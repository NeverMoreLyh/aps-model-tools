"""Read-only local web workbench over the APSGraph SQLite index.

Serves a single-page query UI from the standard library ``http.server`` and a
small JSON API.  Index databases are always opened read-only and the server
binds to 127.0.0.1 only; the workbench never writes to any index, workspace,
or business repository.  The only things it may write are two user-level
registries under ``~/.apsgraph``: the workspace registry (``lastUsedAt``
refresh, see ``apsgraph.registry``) and the running-instance registry
(``workbench-registry.json``, see below).

Without ``--db`` the server is multi-workspace: every API request resolves a
``ws`` token (registered name or workspace path) against the global workspace
registry, so all registered workspaces are browsable and switchable in one
place without a server restart.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
import sys
import threading
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

from .ddlgen import DdlGenConfig, generate_all_ddl, _resolve_type_ref
from .registry import (
    WorkspaceRef, default_selection, resolve_workspace, touch_workspace,
    workspace_overview,
)
from .store import (
    NODE_SELECT, _row_to_dict, connect, find_nodes, get_stats, references,
)

# 历史默认端口；CLI 默认改为随机空闲端口，需要固定端口时用 --port 8321 显式指定
WORKBENCH_PORT = 8321
PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

# 覆盖实例注册表路径的环境变量；默认在用户主目录下（跨工作区共享，
# 供 workbench list / close 发现其他文件夹下启动的实例）。
REGISTRY_ENV = "APSGRAPH_WORKBENCH_REGISTRY"

STATIC_DIR = Path(__file__).resolve().parent / "workbench_static"

# Query pages over index node kinds.  ``suffix`` narrows a kind group to model
# files with that suffix (data dictionaries and error codes are both
# DICTIONARY nodes and only differ by source file suffix).
KIND_GROUPS: Dict[str, Dict[str, Any]] = {
    "enum": {"kinds": {"ENUM_VALUE"}},
    "dictionary": {"kinds": {"DICTIONARY"}, "suffix": ".d_schema.xml"},
    # Real-world .error.xml files are errorConf roots (kind ERRORCONF), not
    # dictionaries; their detail carries errors>error message definitions.
    "error_code": {"kinds": {"ERRORCONF"}, "suffix": ".error.xml"},
    # 错误码数据项：粒度为 GnError.Genl.E0001 这类 error 明细节点
    "error_item": {"kinds": {"ERROR"}, "suffix": ".error.xml"},
    # 基础类型：.u_schema.xml 中定义的 restrictionType（如 ApBaseType.U_ADDR）
    "base_type": {"kinds": {"RESTRICTION_TYPE"}, "suffix": ".u_schema.xml"},
    # 文件批量：file_batch_transaction 根（结构同批量交易：文件模板字段 + 输入）
    "file_batch": {"kinds": {"FILE_BATCH_TRANSACTION"}, "suffix": ".file_batch_tran.xml"},
    # 命名SQL文件：sqls 根，语句为 select/update/... 等 NAMED_SQL 节点
    "nsql": {"kinds": {"SQL_GROUP"}, "suffix": ".nsql.xml"},
    # 命名SQL：数据项粒度 NAMED_SQL（如 ApBatchFileSqls.upd_tb_file_tran_req）
    "nsql_item": {"kinds": {"NAMED_SQL"}, "suffix": ".nsql.xml"},
    # 分片：ShardingStrategy 根，策略为 strategy 节点
    "sharding": {"kinds": {"SHARDINGSTRATEGY"}, "suffix": ".sharding.xml"},
    # 常量数据项：.constant.xml 中 constantConf>constants>constant（message/description）
    "constant": {"kinds": {"CONSTANT"}, "suffix": ".constant.xml"},
    "complex_type": {"kinds": {"COMPLEX_TYPE"}},
    # 字典数据项只收字典文件（.d_schema.xml，父节点为 DICTIONARY）下的 element，
    # 粒度为 full_id 形如 BpDict.B.btch_grp_num；复合类型的 element 不在此页。
    "dict_element": {"kinds": {"ELEMENT"}, "dict_only": True},
    "table": {"kinds": {"TABLE"}},
    "service": {"kinds": {"SERVICE_TYPE"}},
    "service_operation": {"kinds": {"SERVICE_OPERATION"}},
    "transaction": {"kinds": {"TRANSACTION"}},
    "batch": {"kinds": {"BATCH_TRANSACTION"},
              "extra_kinds": {"FILE_BATCH_TRANSACTION", "BATCH_STEP", "BATCH_GROUP"}},
}

# Substring (LIKE) search dimensions.  Unlike FTS5 token matching, LIKE gives
# true fuzzy semantics for partial English identifiers ("openAcc" matches
# "DemoSvc.openAccount") and for short Chinese substrings alike.  ``message``
# is included so error-code pages can be searched by their message text.
LIKE_COLUMNS: Dict[str, List[str]] = {
    "id": ["n.raw_id"],
    "fullid": ["n.full_id"],
    "longname": ["json_extract(n.properties_json,'$.longname')",
                 "json_extract(n.properties_json,'$.name')"],
    "desc": ["json_extract(n.properties_json,'$.description')",
             "json_extract(n.properties_json,'$.desc')",
             "json_extract(n.properties_json,'$.remark')",
             "json_extract(n.properties_json,'$.message')"],
}
LIKE_COLUMNS[""] = sorted({column for columns in LIKE_COLUMNS.values() for column in columns})

def _like_clause(query: str, dimension: str) -> Tuple[str, List[Any]]:
    columns = LIKE_COLUMNS.get(dimension)
    if columns is None:
        raise ValueError(f"unknown search dimension: {dimension}")
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    # fullid 维度支持按 "." 分段的层级筛选：GnError.GnError.E0001 会命中
    # GnError.GnError.Genl.E0001（中间分组段被省略时仍可定位）。
    if dimension == "fullid" and "." in query:
        pattern = "%" + "%".join(_escape(segment) for segment in query.split(".")) + "%"
        return "(n.full_id like ? escape '\\')", [pattern]
    escaped = _escape(query)
    clause = " or ".join(f"{column} like ? escape '\\'" for column in columns)
    return f"({clause})", [f"%{escaped}%"] * len(columns)


def _group_kinds(group: str, requested_kinds: Optional[List[str]]) -> List[str]:
    config = KIND_GROUPS[group]
    if requested_kinds and config.get("extra_kinds"):
        merged = sorted(config["kinds"] | config["extra_kinds"])
        allowed = [kind for kind in requested_kinds if kind in merged]
        if allowed:
            return allowed
    return sorted(config["kinds"])


def _group_filters(group: str, kinds: List[str], root_kind: str = "") -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if group == "top":
        clauses.append("n.owner_node_id is null")
        if root_kind:
            clauses.append("n.kind=?")
            params.append(root_kind)
    else:
        marks = ",".join("?" for _ in kinds)
        clauses.append(f"n.kind in ({marks})")
        params.extend(kinds)
        suffix = KIND_GROUPS[group].get("suffix")
        if suffix:
            clauses.append("f.path like ?")
            params.append("%" + suffix)
        if KIND_GROUPS[group].get("dict_only"):
            clauses.append("exists(select 1 from nodes p where p.id=n.owner_node_id and p.kind='DICTIONARY')")
            clauses.append("f.path like '%.d_schema.xml'")
    return (" and " + " and ".join(clauses), params) if clauses else ("", params)


def _node_summaries(conn: sqlite3.Connection, rows: Any) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for row in rows:
        node = _row_to_dict(row)
        props = node.get("properties") or {}
        node["chinese_name"] = str(props.get("longname") or props.get("name") or "")
        node["description"] = str(props.get("description") or props.get("desc")
                                  or props.get("remark") or "")
        keep = ("id", "stable_id", "kind", "raw_id", "full_id", "owner_id", "owner_node_id",
                "owner_full_id", "owner_name", "file_path", "xml_tag",
                "chinese_name", "description", "properties")
        results.append({key: node.get(key) for key in keep})
    return results


def search_group(conn: sqlite3.Connection, group: str, query: str = "", dimension: str = "",
                 page: int = 1, page_size: int = PAGE_SIZE,
                 kinds: Optional[List[str]] = None, root_kind: str = "") -> Dict[str, Any]:
    """Search (or browse when query is empty) one workbench page group."""
    if group not in KIND_GROUPS and group != "top":
        raise ValueError(f"unknown query group: {group}")
    if dimension and dimension not in LIKE_COLUMNS:
        raise ValueError(f"unknown search dimension: {dimension}")
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    page = max(1, int(page))

    offset = (page - 1) * page_size
    selected = _group_kinds(group, kinds) if group in KIND_GROUPS else []
    where, params = _group_filters(group, selected, root_kind)
    if query:
        like_sql, like_params = _like_clause(query, dimension)
        conjuncts = [like_sql]
        if group == "error_code" and dimension in ("", "desc"):
            # errorConf 本身没有 message；允许命中其下 error 明细的 message
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conjuncts.append(
                "exists(select 1 from nodes c1 join nodes c2 on c2.owner_node_id=c1.id "
                "where (c1.owner_node_id=n.id or c2.owner_node_id=n.id) "
                "and json_extract(c2.properties_json,'$.message') like ? escape '\\')")
            params = [*params, f"%{escaped}%"]
        where = where + " and (" + " or ".join(conjuncts) + ")"
        params = [*params, *like_params]

    base = ("from nodes n join model_files f on f.id=n.file_id "
            "left join nodes owner on owner.id=n.owner_node_id where 1=1" + where)
    total = conn.execute(f"select count(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"""select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,
        owner.stable_id as owner_id,n.owner_node_id,owner.full_id as owner_full_id,
        json_extract(owner.properties_json,'$.longname') as owner_name,
        f.path as file_path,n.file_id,n.xml_tag,n.properties_json {base}
        order by n.kind, n.full_id, f.path limit ? offset ?""",
        [*params, page_size, offset]).fetchall()
    return {"group": group, "query": query, "total": total, "page": page,
            "page_size": page_size, "results": _node_summaries(conn, rows)}


def enum_groups(conn: sqlite3.Connection, query: str = "", page: int = 1,
                page_size: int = PAGE_SIZE) -> Dict[str, Any]:
    """List the enums (owners of ENUM_VALUE nodes) for the enum master page."""
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    page = max(1, int(page))
    offset = (page - 1) * page_size
    base = """from nodes ev join nodes own on own.id=ev.owner_node_id
              where ev.kind='ENUM_VALUE'"""
    params: List[Any] = []
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        needle = f"%{escaped}%"
        base += (" and (own.full_id like ? escape '\\' or own.raw_id like ? escape '\\' or "
                 "json_extract(own.properties_json,'$.longname') like ? escape '\\')")
        params = [needle, needle, needle]
    total = conn.execute(f"select count(distinct own.id) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"""select own.id,own.stable_id,own.kind,own.raw_id,own.full_id,
        json_extract(own.properties_json,'$.longname') as chinese_name,
        count(ev.id) as value_count {base}
        group by own.id order by own.full_id limit ? offset ?""",
        [*params, page_size, offset]).fetchall()
    return {"query": query, "total": total, "page": page, "page_size": page_size,
            "results": [dict(row) for row in rows]}


def top_groups(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Group all top-level models (owner_node_id is null) by node kind."""
    rows = conn.execute(
        """select n.kind, count(*) as total
           from nodes n where n.owner_node_id is null
           group by n.kind order by n.kind""").fetchall()
    return [{"kind": row["kind"], "count": row["total"]} for row in rows]


DDL_DIALECTS = {"mysql": "mysql", "oracle": "oracle", "postgresql": "postgres",
                "tdsql": "mysql", "goldendb": "mysql"}

# 分布式方言的表尾分布子句（shardkey=... / DISTRIBUTED BY ...）不是标准 MySQL 语法，
# sqlglot 按 mysql 方言校验前先剥离；PARTITION BY 是标准语法，无需剥离
_DISTRIBUTION_CLAUSE_RE = re.compile(
    r"shardkey\s*=\s*\S+"
    r"|DISTRIBUTED\s+BY\s+(?:HASH|DUPLICATE)\s*\([^)]*\)(?:\s*\([^)]*\))?",
    re.IGNORECASE)
# sqlglot 的 mysql 方言不识别 RANGE COLUMNS 关键字；校验前降级为 RANGE 做语法近似
#（表达式与列类型的匹配由数据库在执行侧保证）
_RANGE_COLUMNS_RE = re.compile(r"RANGE\s+COLUMNS", re.IGNORECASE)


def validate_ddl_sql(sql: str, dialect: str) -> Dict[str, Any]:
    """Validate generated DDL with sqlglot (optional dependency).

    Mirrors the db-diff pattern: sqlglot is imported lazily so the core CLI
    keeps zero mandatory third-party dependencies.  When it is not installed
    the validation is reported as skipped instead of failing.
    """
    if dialect not in DDL_DIALECTS:
        raise ValueError(f"unsupported dialect: {dialect}")
    if not sql or not sql.strip():
        return {"available": False, "valid": None, "errors": [], "statements": 0,
                "hint": "未生成 DDL，跳过校验"}
    try:
        import sqlglot
        from sqlglot.errors import ParseError
    except ImportError:
        return {"available": False, "valid": None, "errors": [], "statements": 0,
                "hint": "可选依赖 sqlglot 未安装，跳过 SQL 校验（pip install sqlglot）"}
    text = sql
    if dialect in ("tdsql", "goldendb"):
        text = _DISTRIBUTION_CLAUSE_RE.sub("", text)
    text = _RANGE_COLUMNS_RE.sub("RANGE", text)
    try:
        statements = [stmt for stmt in sqlglot.parse(text, read=DDL_DIALECTS[dialect])
                      if stmt is not None]
    except ParseError as exc:
        return {"available": True, "valid": False, "errors": [str(exc)], "statements": 0,
                "hint": ""}
    # ddl-gen legitimately emits create/alter/index plus sequence seed
    # insert/delete statements; any successful dialect parse is valid SQL.
    create_tables = sum(1 for stmt in statements if getattr(stmt, "key", "") == "create")
    if create_tables == 0:
        return {"available": True, "valid": False,
                "errors": ["no CREATE TABLE statement found in generated DDL"],
                "statements": 0, "hint": ""}
    return {"available": True, "valid": True, "errors": [],
            "statements": create_tables, "hint": ""}


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _named_sql_texts(conn: sqlite3.Connection, node: Dict[str, Any],
                     source_root: Optional[Path]) -> List[Dict[str, str]]:
    """Extract the per-database SQL texts of a NAMED_SQL from its source XML.

    The statement's <sql type="..."> children carry the SQL as CDATA, which is
    not copied into the index; re-read the source file on demand.  ``type``
    defaults to NONE when absent.
    """
    if source_root is None or not node.get("file_path"):
        return []
    path = Path(source_root) / node["file_path"]
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    tag, raw_id = node.get("xml_tag"), node.get("raw_id")
    for element in root.iter():
        if (_local_tag(element.tag) == tag and element.attrib.get("id") == raw_id):
            # 静态语句：多个 <sql type="..."> 子元素按数据库类型区分；
            # 动态语句（dynamicSelect/dynamicSql）：SQL 原文本是元素自身的 CDATA。
            sql_children = [child for child in element if _local_tag(child.tag) == "sql"]
            if sql_children:
                return [{"type": str(child.attrib.get("type") or "NONE"),
                         "text": (child.text or "").strip()}
                        for child in sql_children]
            dyn_children = [child for child in element if _local_tag(child.tag) == "dynamicSql"]
            if dyn_children:
                # 动态SQL：按 MyBatis mapper 机制，原样展示每个 <dynamicSql type="..."> XML 节点
                result = []
                for dyn in dyn_children:
                    raw = ET.tostring(dyn, encoding="unicode").strip()
                    result.append({"type": str(dyn.attrib.get("type") or "NONE"),
                                   "text": raw})
                return result
            own_text = (element.text or "").strip()
            if own_text:
                return [{"type": "NONE", "text": own_text}]
            # 无 CDATA 文本时兜底展示整个语句节点的原始 XML
            return [{"type": "NONE", "text": ET.tostring(element, encoding="unicode").strip()}]
    return []


def _find_element_by_id(element: ET.Element, seg: str) -> Optional[ET.Element]:
    for child in element.iter():
        if child is element:
            continue
        if child.attrib.get("id") == seg:
            return child
    return None


def _find_element_by_tag(element: ET.Element, tag: str) -> Optional[ET.Element]:
    for child in element.iter():
        if child is element:
            continue
        if _local_tag(child.tag) == tag:
            return child
    return None


def _source_xml_fragment(conn: sqlite3.Connection, node: Dict[str, Any],
                         source_root: Optional[Path]) -> Optional[Dict[str, str]]:
    """Original XML fragment of a node, read on demand from its source file.

    The fragment is never stored in SQLite.  The element is located by walking
    the full_id id-chain inside the source document; nodes without their own
    full_id (id-less containers) are located from the nearest ancestor's
    full_id followed by the container tag chain.
    """
    if source_root is None or not node.get("file_path"):
        return None
    path = Path(source_root) / node["file_path"]
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    full_id = node.get("full_id") or ""
    container_tags: List[str] = []
    if full_id:
        segments = full_id.split(".")
    else:
        segments, parent_id = [], node.get("owner_node_id")
        while parent_id:
            row = conn.execute(
                "select owner_node_id, full_id, xml_tag from nodes where id=?",
                (parent_id,)).fetchone()
            if row is None:
                return None
            if row["full_id"]:
                segments = row["full_id"].split(".")
                break
            container_tags.insert(0, str(row["xml_tag"]))
            parent_id = row["owner_node_id"]
        if not segments:
            return None
        container_tags.append(str(node.get("xml_tag")))
    current = root
    for seg in segments[1:]:
        nxt = _find_element_by_id(current, seg)
        if nxt is None:
            return None
        current = nxt
    if not full_id:
        for tag in container_tags:
            nxt = _find_element_by_tag(current, tag)
            if nxt is None:
                return None
            current = nxt
    fragment = ET.tostring(current, encoding="unicode").strip()
    return {"path": node["file_path"], "xml": fragment}


# 文件级节点的 XML 片段与整份源文件几乎相同，跳过展示
FRAGMENT_SKIP_KINDS = {"SQL_GROUP", "DICTIONARY", "ERRORCONF"}


def _resolve_node_row(conn: sqlite3.Connection, node_ref: str) -> sqlite3.Row:
    """Resolve a node by stable_id, or by exact full_id/raw_id as fallback."""
    row = conn.execute(NODE_SELECT + " where n.stable_id=?", (node_ref,)).fetchone()
    if row is not None:
        return row
    matches = [item for item in find_nodes(conn, node_ref)
               if node_ref in {item["stable_id"], item["full_id"], item["raw_id"]}]
    if not matches:
        raise ValueError(f"model not found: {node_ref}")
    row = conn.execute(NODE_SELECT + " where n.stable_id=?",
                       (matches[0]["stable_id"],)).fetchone()
    if row is None:
        raise ValueError(f"model not found: {node_ref}")
    return row


DASHBOARD_GROUPS = [
    "top", "table", "service", "service_operation", "transaction", "batch",
    "file_batch", "nsql", "nsql_item", "sharding", "complex_type", "dictionary",
    "dict_element", "base_type", "error_code", "error_item", "constant",
]


def dashboard(conn: sqlite3.Connection, db_path: Path) -> Dict[str, Any]:
    """Overview numbers: index stats, sqlite file size, per-page record counts."""
    counts = {}
    for group in DASHBOARD_GROUPS:
        counts[group] = search_group(conn, group, page=1, page_size=1)["total"]
    # 枚举类型页按枚举（ENUM_VALUE 的 owner）计数
    counts["enum"] = enum_groups(conn, page=1, page_size=1)["total"]
    return {
        "stats": get_stats(conn),
        "db_size": Path(db_path).stat().st_size,
        "counts": counts,
    }


def child_nodes(conn: sqlite3.Connection, stable_id: str) -> List[Dict[str, Any]]:
    """Direct children of a node, for the lazy-loaded containment tree."""
    row = conn.execute("select id from nodes where stable_id=?", (stable_id,)).fetchone()
    node_id = row[0] if row else _resolve_node_row(conn, stable_id)["id"]
    rows = conn.execute(
        """select n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json,
                  exists(select 1 from nodes c where c.owner_node_id=n.id) as has_children
           from nodes n where n.owner_node_id=? order by n.id""", (node_id,)).fetchall()
    results = []
    for record in rows:
        node = _row_to_dict(record)
        props = node.pop("properties", {}) or {}
        node["chinese_name"] = str(props.get("longname") or props.get("name") or "")
        results.append({key: node.get(key) for key in
                        ("stable_id", "kind", "raw_id", "full_id", "xml_tag",
                         "chinese_name", "has_children")})
    return results


_DESCENDANT_CTE = """
with recursive sub(id) as (
    select ? union all select n.id from nodes n join sub s on n.owner_node_id=s.id
)
"""


def _descendants_of_kind(conn: sqlite3.Connection, node_id: int, kind: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        _DESCENDANT_CTE + """select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
        from nodes n where n.id in (select id from sub) and n.kind=? and n.id != ?
        order by n.id""", (node_id, kind, node_id)).fetchall()
    return [_row_to_dict(row) for row in rows]


def _container_under(conn: sqlite3.Connection, node_id: int, container_tag: str) -> Optional[int]:
    """Locate a passthrough container (input/output/flow) inside a subtree.

    Containers such as ``interface``/``fields`` are id-less nodes, and
    ``input`` hangs under ``interface`` rather than directly under the
    operation, so the lookup must descend the owner chain.
    """
    rows = conn.execute(
        _DESCENDANT_CTE + """select n.id from nodes n
        where n.id in (select id from sub) and n.id != ? and n.xml_tag=? order by n.id""",
        (node_id, node_id, container_tag)).fetchall()
    return rows[0][0] if rows else None


def _fields_under(conn: sqlite3.Connection, node_id: int, container_tag: str) -> List[Dict[str, Any]]:
    """Fields below an input/output container (container nodes have no ids)."""
    container = _container_under(conn, node_id, container_tag)
    if container is None:
        return []
    rows = conn.execute(
        _DESCENDANT_CTE + """select n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
        from nodes n where n.id in (select id from sub) and n.kind='FIELD' and n.id != ?
        order by n.id""", (container, container)).fetchall()
    return [_row_to_dict(row) for row in rows]


def _table_extensions(conn: sqlite3.Connection, node_id: int) -> List[Dict[str, Any]]:
    """Common-field tables referenced via EXTENDS, in document order.

    A table may extend several common tables; each entry carries the target
    identity and its own fields so the UI can render them sequentially.
    """
    rows = conn.execute(
        """select e.raw_target, t.id as tid, t.stable_id, t.full_id
        from edges e left join nodes t on t.id=e.to_node_id
        where e.from_node_id=? and e.relation_kind='EXTENDS' order by e.id""",
        (node_id,)).fetchall()
    extensions: List[Dict[str, Any]] = []
    for row in rows:
        resolved = row["tid"] is not None
        extensions.append({
            "raw_target": row["raw_target"],
            "full_id": row["full_id"] if resolved else row["raw_target"],
            "stable_id": row["stable_id"] if resolved else None,
            "resolved": resolved,
            "fields": [_with_primitive(conn, f)
                       for f in _descendants_of_kind(conn, row["tid"], "FIELD")] if resolved else [],
        })
    return extensions


def _with_primitive(conn: sqlite3.Connection, node: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the resolved primitive type name (empty when unresolvable);
    the DDL dialog uses it to hint the partition conversion function."""
    data = dict(node)
    type_ref = (data.get("properties") or {}).get("type")
    primitive = ""
    if type_ref:
        resolved, _, _ = _resolve_type_ref(conn, str(type_ref))
        primitive = resolved or ""
    data["primitive"] = primitive
    return data


def _table_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    # Real tables carry both index flavors as kind INDEX; distinguish them by
    # their parent container (<indexes> vs <odbindexes>), not by tag.
    odb_container = _container_under(conn, node_id, "odbindexes")
    odbindexes = _descendants_of_kind(conn, odb_container, "INDEX") if odb_container is not None else []
    odb_ids = {node["id"] for node in odbindexes}
    indexes = [node for node in _descendants_of_kind(conn, node_id, "INDEX")
               if node["id"] not in odb_ids]
    return {
        "fields": [_with_primitive(conn, f) for f in _descendants_of_kind(conn, node_id, "FIELD")],
        "extensions": _table_extensions(conn, node_id),
        "indexes": indexes,
        "odbindexes": odbindexes,
        "sequences": _descendants_of_kind(conn, node_id, "SEQUENCE"),
    }


def _interface_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    return {
        "input": _fields_under(conn, node_id, "input"),
        "output": _fields_under(conn, node_id, "output"),
    }


def _service_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    operations = []
    for op in _descendants_of_kind(conn, node_id, "SERVICE_OPERATION"):
        operations.append({
            "node": op,
            "input": _fields_under(conn, op["id"], "input"),
            "output": _fields_under(conn, op["id"], "output"),
        })
    return {"operations": operations}


def _flow_tree(conn: sqlite3.Connection, node_id: int, depth: int = 0) -> List[Dict[str, Any]]:
    """Build the flow orchestration tree under a flow node.

    Real FlowTran flows mix direct ``service``/``method`` steps with
    ``case``/``when`` branches (when nodes carry a ``test`` expression and
    contain their own service steps), so this must recurse.
    """
    rows = conn.execute(
        """select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
        from nodes n where n.owner_node_id=? order by n.id""", (node_id,)).fetchall()
    steps: List[Dict[str, Any]] = []
    for row in rows:
        props = json.loads(row["properties_json"] or "{}")
        tag = row["xml_tag"]
        step: Dict[str, Any] = {
            "stable_id": row["stable_id"], "kind": row["kind"], "raw_id": row["raw_id"],
            "xml_tag": tag, "longname": str(props.get("longname") or ""),
            "test": str(props.get("test") or ""), "label": "", "resolved_target": None,
            "children": [],
        }
        if tag == "service":
            target = str(props.get("serviceName") or props.get("transactionId")
                         or props.get("transaction") or "")
            step["label"] = target
            if target:
                matches = [item for item in find_nodes(conn, target)
                           if target in {item["stable_id"], item["full_id"], item["raw_id"]}]
                if matches:
                    step["resolved_target"] = matches[0]["stable_id"]
        elif tag == "method":
            step["label"] = str(props.get("method") or row["raw_id"])
        else:
            step["label"] = str(props.get("longname") or row["raw_id"])
        if depth < 8 and tag in {"case", "when"}:
            step["children"] = _flow_tree(conn, row["id"], depth + 1)
        steps.append(step)
    return steps


def _transaction_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    interface = _interface_detail(conn, node_id)
    flow_steps: List[Dict[str, Any]] = []
    flow = _container_under(conn, node_id, "flow")
    if flow is not None:
        flow_steps = _flow_tree(conn, flow)
    return {**interface, "flow_steps": flow_steps}


def _error_code_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    """errorConf detail: errors groups each carrying their error definitions."""
    groups = []
    for group in _descendants_of_kind(conn, node_id, "ERRORS"):
        rows = conn.execute(
            """select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
            from nodes n where n.owner_node_id=? order by n.id""", (group["id"],)).fetchall()
        groups.append({"node": group, "errors": [_error_row(conn, row) for row in rows]})
    orphans = conn.execute(
        """select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
        from nodes n where n.owner_node_id=? and n.kind='ERROR' order by n.id""",
        (node_id,)).fetchall()
    if orphans:
        groups.append({"node": None, "errors": [_error_row(conn, row) for row in orphans]})
    return {"groups": groups}


def _error_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    """One error definition plus its parameter ids for the detail tables."""
    item = _row_to_dict(row)
    params = conn.execute(
        "select raw_id, full_id, properties_json from nodes where owner_node_id=? order by id",
        (row["id"],)).fetchall()
    item["parameters"] = ", ".join(
        str(json.loads(p["properties_json"] or "{}").get("id") or p["raw_id"])
        for p in params)
    return item


def _dictionary_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    return {
        "elements": _descendants_of_kind(conn, node_id, "ELEMENT"),
        "enum_values": _descendants_of_kind(conn, node_id, "ENUM_VALUE"),
    }


def _batch_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    # split/execute/merge are id-less passthrough tags, so their inner steps
    # hang directly below the batch transaction; surface them by kind.
    return {
        "input": _descendants_of_kind(conn, node_id, "FIELD"),
        "steps": _descendants_of_kind(conn, node_id, "BATCH_STEP"),
        "groups": _descendants_of_kind(conn, node_id, "BATCH_GROUP"),
    }


def _detail_for_kind(conn: sqlite3.Connection, kind: str, node_id: int) -> Dict[str, Any]:
    if kind == "TABLE":
        return _table_detail(conn, node_id)
    if kind == "SERVICE_TYPE":
        return _service_detail(conn, node_id)
    if kind == "SERVICE_OPERATION":
        return _interface_detail(conn, node_id)
    if kind == "TRANSACTION":
        return _transaction_detail(conn, node_id)
    if kind in {"BATCH_TRANSACTION", "FILE_BATCH_TRANSACTION"}:
        return _batch_detail(conn, node_id)
    if kind == "SQL_GROUP":
        return {"statements": _descendants_of_kind(conn, node_id, "NAMED_SQL")}
    if kind == "NAMED_SQL":
        return {"parameters": _descendants_of_kind(conn, node_id, "SQL_PARAMETER")}
    if kind == "SHARDINGSTRATEGY":
        return {"strategies": _descendants_of_kind(conn, node_id, "STRATEGY")}
    if kind == "ERRORCONF":
        return _error_code_detail(conn, node_id)
    if kind == "ERROR":
        return {"parameters": _descendants_of_kind(conn, node_id, "SQL_PARAMETER")}
    if kind == "ENUM_VALUE":
        return {"enum_values": []}
    if kind in {"RESTRICTION_TYPE", "DICTIONARY", "COMPLEX_TYPE"}:
        return {"elements": _descendants_of_kind(conn, node_id, "ELEMENT"),
                "enum_values": _descendants_of_kind(conn, node_id, "ENUM_VALUE")}
    return {}


def node_detail(conn: sqlite3.Connection, stable_id: str,
                source_root: Optional[Path] = None) -> Dict[str, Any]:
    """Full detail payload: node, direct children, structured view, and edges.

    ``stable_id`` also accepts exact full_id or raw_id values so UI links such
    as flow ``serviceName`` targets resolve to a single node.  ``source_root``
    enables on-demand source extraction (named SQL texts) when provided.
    """
    row = _resolve_node_row(conn, stable_id)
    node = _row_to_dict(row)
    resolved_id = node["stable_id"]
    children = child_nodes(conn, resolved_id)
    graph = references(conn, resolved_id, "both", 1)
    out_edges = []
    in_edges = []
    root = node["stable_id"]
    for edge in graph["edges"]:
        target = ({"stable_id": edge["to_id"], "full_id": edge["to_id"],
                   "raw_target": edge["raw_target"], "resolved": bool(edge["to_id"])}
                  if edge["from_id"] == root else
                  {"stable_id": edge["from_id"], "full_id": edge["from_id"],
                   "raw_target": edge["raw_target"], "resolved": True})
        item = {"relation_kind": edge["relation_kind"], "target": target}
        (out_edges if edge["from_id"] == root else in_edges).append(item)
    detail = _detail_for_kind(conn, node["kind"], node["id"])
    if node["kind"] == "NAMED_SQL":
        detail["sqls"] = _named_sql_texts(conn, node, source_root)
    node["properties"] = node.get("properties") or {}
    # 文件级节点（命名SQL文件/数据字典文件/错误码文件）的片段等于大半份源文件，无展示意义
    fragment = (None if node["kind"] in FRAGMENT_SKIP_KINDS
                else _source_xml_fragment(conn, node, source_root))
    return {"node": node, "children": children, "detail": detail,
            "xml_fragment": fragment,
            "out_edges": out_edges, "in_edges": in_edges}


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "apsgraph-workbench"

    def _resolve(self, params: Dict[str, List[str]]) -> WorkspaceRef:
        """Workspace for this request: the ``ws`` token wins, then the server
        default (startup ``--workspace`` or the bare-run default selection)."""
        return self.server.router.resolve(params.get("ws", [None])[0])  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[apsgraph] workbench: {self.address_string()} {format % args}",
              file=sys.stderr, flush=True)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename: str) -> None:
        target = (STATIC_DIR / filename).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self._send_json({"error": f"static asset not found: {filename}"}, 404)
            return
        content_type = {"html": "text/html; charset=utf-8",
                        "js": "application/javascript; charset=utf-8",
                        "css": "text/css; charset=utf-8"}.get(target.suffix.lstrip("."),
                                                              "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_search(self, ref: WorkspaceRef, params: Dict[str, List[str]]) -> Dict[str, Any]:
        conn = connect(ref.db_path, read_only=True)
        try:
            return search_group(
                conn,
                group=params.get("group", ["table"])[0],
                query=params.get("q", [""])[0],
                dimension=params.get("field", [""])[0],
                page=int(params.get("page", ["1"])[0]),
                page_size=int(params.get("page_size", [str(PAGE_SIZE)])[0]),
                kinds=params.get("kinds") or None,
                root_kind=params.get("root_kind", [""])[0],
            )
        finally:
            conn.close()

    def _api_node(self, ref: WorkspaceRef, params: Dict[str, List[str]]) -> Dict[str, Any]:
        stable_id = params.get("id", [""])[0]
        if not stable_id:
            raise ValueError("missing node id")
        conn = connect(ref.db_path, read_only=True)
        try:
            return node_detail(conn, stable_id, source_root=ref.source_root)
        finally:
            conn.close()

    def _api_parse_failures(self, conn: sqlite3.Connection,
                            params: Dict[str, List[str]]) -> Dict[str, Any]:
        """Files that failed to parse, with their error messages."""
        query = params.get("q", [""])[0]
        sql = """select path, suffix, error_message
                 from model_files where parse_status='PARSE_FAILED'"""
        params_list: List[Any] = []
        if query:
            sql += " and (path like ? escape '\\' or error_message like ? escape '\\')"
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params_list = [f"%{escaped}%", f"%{escaped}%"]
        rows = conn.execute(sql + " order by path", params_list).fetchall()
        return {"total": len(rows),
                "results": [dict(row) for row in rows]}

    def _api_children(self, ref: WorkspaceRef, params: Dict[str, List[str]]) -> Dict[str, Any]:
        stable_id = params.get("id", [""])[0]
        if not stable_id:
            raise ValueError("missing node id")
        conn = connect(ref.db_path, read_only=True)
        try:
            return {"id": stable_id, "children": child_nodes(conn, stable_id)}
        finally:
            conn.close()

    def _api_ddl(self, ref: WorkspaceRef, params: Dict[str, List[str]]) -> Dict[str, Any]:
        """Preview DDL for one table; generation only, never executed."""
        node_ref = params.get("id", [""])[0]
        dialect = params.get("dialect", ["mysql"])[0]
        if not node_ref:
            raise ValueError("missing node id")
        if dialect not in DDL_DIALECTS:
            raise ValueError(f"unsupported dialect: {dialect}")
        cfg = DdlGenConfig(
            dialect=dialect,
            shard_type=params.get("shard_type", ["normal"])[0] or "normal",
            shard_key=params.get("shard_key", [""])[0],
            node_groups=params.get("node_groups", [""])[0],
            create_partition=params.get("create_partition", [""])[0].strip().lower() == "true",
            partition_key=params.get("partition_key", [""])[0],
            partition_type=params.get("partition_type", ["range"])[0] or "range",
            partition_start=params.get("partition_start", [""])[0],
            partition_end=params.get("partition_end", [""])[0],
        )
        conn = connect(ref.db_path, read_only=True)
        try:
            row = conn.execute("select kind from nodes where stable_id=?",
                               (_resolve_node_row(conn, node_ref)["stable_id"],)).fetchone()
            if row is None or row[0] != "TABLE":
                raise ValueError(f"DDL preview is only available for TABLE nodes: {node_ref}")
            report = generate_all_ddl(conn, cfg, [node_ref])
            return {"dialect": dialect, "sql": report.sql,
                    "warnings": report.warnings, "errors": report.errors,
                    "validation": validate_ddl_sql(report.sql, dialect)}
        finally:
            conn.close()

    def _api_workspaces(self) -> Dict[str, Any]:
        """Workspace selector payload; ``default`` mirrors the bare-run
        selection so the UI opens on the same workspace the server chose."""
        router = self.server.router  # type: ignore[attr-defined]
        if isinstance(router, _SingleDbRouter):
            return {"mode": "single"}
        default_path: Optional[str] = None
        try:
            ref = router.resolve(None)
            default_path = str(ref.workspace_path or ref.source_root)
        except (ValueError, OSError):
            default_path = None
        return {"mode": "multi", "workspaces": workspace_overview(router.registry),
                "default": default_path}

    def _touch(self, ref: WorkspaceRef) -> None:
        """Refresh lastUsedAt; a broken registry must not break browsing."""
        try:
            self.server.router.touch(ref)  # type: ignore[attr-defined]
        except (ValueError, OSError) as exc:
            print(f"[apsgraph] workbench: registry lastUsedAt update failed: {exc}",
                  file=sys.stderr, flush=True)

    def _handle_api(self, path: str, params: Dict[str, List[str]]) -> None:
        if path == "/api/workspaces":
            self._send_json(self._api_workspaces())
            return
        ref = self._resolve(params)
        if path == "/api/stats":
            conn = connect(ref.db_path, read_only=True)
            try:
                self._send_json({"stats": get_stats(conn),
                                 "top_groups": top_groups(conn)})
            finally:
                conn.close()
        elif path == "/api/search":
            self._send_json(self._api_search(ref, params))
        elif path == "/api/parse-failures":
            conn = connect(ref.db_path, read_only=True)
            try:
                self._send_json(self._api_parse_failures(conn, params))
            finally:
                conn.close()
        elif path == "/api/dashboard":
            conn = connect(ref.db_path, read_only=True)
            try:
                payload = dashboard(conn, ref.db_path)
            finally:
                conn.close()
            if ref.workspace_path:
                self._touch(ref)
            self._send_json(payload)
        elif path == "/api/enums":
            conn = connect(ref.db_path, read_only=True)
            try:
                self._send_json(enum_groups(
                    conn,
                    query=params.get("q", [""])[0],
                    page=int(params.get("page", ["1"])[0]),
                    page_size=int(params.get("page_size", [str(PAGE_SIZE)])[0]),
                ))
            finally:
                conn.close()
        elif path == "/api/node":
            self._send_json(self._api_node(ref, params))
        elif path == "/api/children":
            self._send_json(self._api_children(ref, params))
        elif path == "/api/ddl":
            self._send_json(self._api_ddl(ref, params))
        elif path == "/api/top-groups":
            conn = connect(ref.db_path, read_only=True)
            try:
                self._send_json({"groups": top_groups(conn)})
            finally:
                conn.close()
        else:
            self._send_json({"error": f"unknown api: {path}"}, 404)

    def do_GET(self) -> None:  # noqa: N802
        raw_path = self.path
        try:
            # BaseHTTPRequestHandler decodes the request line as latin-1; undo
            # that so raw (non-percent-encoded) UTF-8 queries still work.
            raw_path = raw_path.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
        parsed = urlparse(raw_path)
        try:
            if parsed.path == "/":
                self._send_static("index.html")
            elif parsed.path in {"/app.js", "/app.css", "/mermaid.min.js"}:
                self._send_static(parsed.path.lstrip("/"))
            elif parsed.path.startswith("/api/"):
                self._handle_api(parsed.path, parse_qs(parsed.query))
            else:
                self._send_json({"error": f"not found: {parsed.path}"}, 404)
        except (ValueError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"error": f"internal error: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        self._send_json({"error": "workbench is read-only; only GET is supported"}, 405)


class _SingleDbRouter:
    """Serve one explicit ``--db`` index; the legacy single-workspace mode."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def resolve(self, token: Optional[str] = None) -> WorkspaceRef:
        source_root = (self.db_path.parent.parent
                       if self.db_path.parent.name == ".apsgraph"
                       else self.db_path.parent)
        return WorkspaceRef(db_path=self.db_path, source_root=source_root)

    def touch(self, ref: WorkspaceRef) -> bool:
        return False


class WorkspaceRouter:
    """Route requests to workspaces from the global registry.

    Request ``ws`` tokens win; then the startup token (``--workspace``);
    then the bare-run default selection.  Resolution happens per request so
    workspaces scanned after the server started are switchable immediately.
    """

    def __init__(self, token: Optional[str] = None, registry: Optional[Path] = None):
        self.token = token
        self.registry = registry

    def resolve(self, token: Optional[str] = None) -> WorkspaceRef:
        if token:
            ref = resolve_workspace(token, self.registry)
        elif self.token:
            ref = resolve_workspace(self.token, self.registry)
        else:
            ref = default_selection(path=self.registry)
        if not ref.db_path.is_file():
            raise FileNotFoundError(
                f"index database does not exist: {ref.db_path}; run 'apsgraph scan' first")
        return ref

    def touch(self, ref: WorkspaceRef) -> bool:
        """Mark a registered workspace as just used; unregistered sources are
        a no-op because there is no entry to refresh."""
        if ref.workspace_path is None:
            return False
        return touch_workspace(ref.workspace_path, self.registry)


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    # http.server 默认 allow_reuse_address=1（SO_REUSEADDR）。Windows 的
    # SO_REUSEADDR 允许重复绑定同一活动端口：两个 workbench 都会"成功"监听
    # 8321 且不报任何冲突，请求被随机分流。POSIX 上该选项只用于快速重启绕过
    # TIME_WAIT，因此仅在非 Windows 平台保留。
    allow_reuse_address = os.name != "nt"

    def __init__(self, db_path: Optional[Path] = None, port: int = WORKBENCH_PORT,
                 workspace: Optional[str] = None, registry: Optional[Path] = None):
        if db_path is not None:
            self.router: Union[_SingleDbRouter, WorkspaceRouter] = _SingleDbRouter(db_path)
        else:
            self.router = WorkspaceRouter(workspace, registry)
        super().__init__(("127.0.0.1", port), WorkbenchHandler)


def registry_path() -> Path:
    """User-level registry file tracking running workbench instances."""
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override)
    return Path.home() / ".apsgraph" / "workbench-registry.json"


def _load_registry(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("instances") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _save_registry(path: Path, entries: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"instances": entries}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def register_workbench(entry: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Record one running instance; an entry with the same port is replaced."""
    path = Path(path) if path else registry_path()
    entries = [item for item in _load_registry(path) if item.get("port") != entry["port"]]
    entries.append(entry)
    _save_registry(path, entries)


def unregister_workbench(port: int, path: Optional[Path] = None) -> None:
    path = Path(path) if path else registry_path()
    entries = _load_registry(path)
    kept = [item for item in entries if item.get("port") != port]
    if len(kept) != len(entries):
        _save_registry(path, kept)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill on Windows terminates the target for any signal other than
        # the CTRL events, so liveness must be probed via tasklist instead.
        # tasklist 输出使用本地代码页（如 GBK），按字节匹配 ASCII 的 pid 引号串。
        try:
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, timeout=15, check=False).stdout or b""
        except (OSError, ValueError):
            return True  # 无法判断时按存活处理，由 close 端探测后再决定
        return f'"{pid}"'.encode("ascii") in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe_workbench(port: int, timeout: float = 3.0) -> bool:
    """True when 127.0.0.1:<port>/api/stats answers like an APSGraph workbench.

    close 用它确认注册表条目仍然对应本工具的实例，避免 pid 被复用后误杀无关进程。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/stats", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and "stats" in payload


def _terminate_pid(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def list_workbenches(path: Optional[Path] = None,
                     progress=None) -> Dict[str, Any]:
    """Running instances from the registry; stale entries are pruned in place."""
    path = Path(path) if path else registry_path()
    alive: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    for entry in _load_registry(path):
        try:
            pid = int(entry.get("pid", 0))
            port = int(entry.get("port", 0))
        except (TypeError, ValueError):
            stale.append(entry)
            continue
        if _pid_alive(pid) and _probe_workbench(port):
            alive.append(entry)
        else:
            stale.append(entry)
    if stale:
        try:
            _save_registry(path, alive)
        except OSError as exc:
            if progress:
                progress(f"could not prune stale workbench registry entries: {exc}")
    return {"instances": sorted(alive, key=lambda item: item.get("port", 0)),
            "pruned_stale": len(stale)}


def close_workbenches(port: Optional[int] = None, close_all: bool = False,
                      path: Optional[Path] = None, progress=None) -> Dict[str, Any]:
    """Stop registered instance(s) and remove their registry entries.

    Only processes that currently answer the workbench API are terminated; a
    live pid whose port no longer serves the workbench API is reported as a
    stale entry and pruned instead of being killed.
    """
    path = Path(path) if path else registry_path()
    entries = _load_registry(path)
    if close_all:
        targets = list(enumerate(entries))
    else:
        targets = [(index, entry) for index, entry in enumerate(entries)
                   if entry.get("port") == port]
    result: Dict[str, Any] = {"closed": [], "already_stopped": [], "stale": [],
                              "failed": [], "not_found": []}
    if not targets:
        if not close_all:
            result["not_found"].append(port)
        return result
    dropped: set = set()
    for index, entry in targets:
        try:
            pid = int(entry.get("pid", 0))
            port_value = int(entry.get("port", 0))
        except (TypeError, ValueError):
            pid, port_value = 0, 0
        if _probe_workbench(port_value):
            try:
                _terminate_pid(pid)
                result["closed"].append(entry)
                dropped.add(index)
                if progress:
                    progress(f"workbench stopped: 127.0.0.1:{port_value} (pid {pid})")
            except (OSError, ValueError) as exc:
                failed = dict(entry)
                failed["error"] = str(exc)
                result["failed"].append(failed)
        elif not _pid_alive(pid):
            result["already_stopped"].append(entry)
            dropped.add(index)
        else:
            # pid 复用或端口被其他程序占用：不终止进程，仅清理注册表条目
            result["stale"].append(entry)
            dropped.add(index)
    kept = [entry for index, entry in enumerate(entries) if index not in dropped]
    _save_registry(path, kept)
    return result


def _install_sigterm_handler() -> None:
    """Turn SIGTERM into KeyboardInterrupt so POSIX termination still unregisters."""
    if not hasattr(signal, "SIGTERM"):
        return

    def _handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass  # not the main thread or unsupported platform


def _instance_entry(server: WorkbenchServer, db_path: Optional[Path],
                    url: str) -> Dict[str, Any]:
    if db_path is not None:
        db_path = Path(db_path).resolve()
        workspace = (db_path.parent.parent if db_path.parent.name == ".apsgraph"
                     else db_path.parent)
        db_value: Optional[str] = str(db_path)
        workspace_value = str(Path(workspace).resolve())
    else:
        # Multi-workspace instance: no single index; record the launch dir.
        db_value = None
        workspace_value = str(Path.cwd().resolve())
    return {"port": int(server.server_port), "pid": os.getpid(), "url": url,
            "db": db_value, "workspace": workspace_value,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def _bind_workbench_server(db_path: Path, port: int) -> WorkbenchServer:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(
            f"index database does not exist: {db_path}; run 'apsgraph scan' first")
    connect(db_path, read_only=True).close()
    try:
        return WorkbenchServer(db_path, port)
    except OSError as exc:
        raise ValueError(f"cannot bind 127.0.0.1:{port}: {exc}; "
                         "use --port 0 to pick a random free port") from exc


def _serve_workbench_server(server: WorkbenchServer, db_path: Optional[Path],
                            open_browser: bool = True, progress=None,
                            target: Optional[str] = None) -> int:
    _install_sigterm_handler()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        register_workbench(_instance_entry(server, db_path, url))
    except OSError as exc:
        if progress:
            progress(f"could not record workbench instance in registry: {exc}")
    if progress:
        progress(f"workbench serving {target or db_path} at {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if progress:
            progress("workbench stopped")
    finally:
        try:
            unregister_workbench(server.server_port)
        except OSError:
            pass
        server.server_close()
    return 0


def serve_workbench(db_path: Optional[Path] = None, port: int = 0,
                    open_browser: bool = True, progress=None,
                    workspace: Optional[str] = None) -> int:
    """Start the local workbench server; blocks until interrupted.

    ``port`` defaults to 0 — the OS assigns a random free port so several
    workspaces can be served at the same time; the effective port is reported
    in the URL and recorded in the instance registry.  Pass an explicit port
    (e.g. ``WORKBENCH_PORT``) for a fixed address.

    With an explicit ``db_path`` the server serves that one index (legacy
    ``--db`` mode).  Otherwise it serves every workspace registered in
    ``~/.apsgraph/registry.json``; ``workspace`` preselects one, and the UI
    can switch between all of them without a restart.
    """
    if db_path is not None:
        db_path = Path(db_path)
        server = _bind_workbench_server(db_path, port)
        return _serve_workbench_server(server, db_path, open_browser=open_browser,
                                       progress=progress)
    # Multi-workspace mode: fail fast on an unknown --workspace token or an
    # empty registry before binding the port; per-workspace staleness stays
    # fail-soft (selectable entries explain themselves in the UI).
    probe = WorkspaceRouter(workspace)
    ref = probe.resolve(None)
    connect(ref.db_path, read_only=True).close()
    count = len(workspace_overview())
    try:
        server = WorkbenchServer(db_path=None, port=port, workspace=workspace)
    except OSError as exc:
        raise ValueError(f"cannot bind 127.0.0.1:{port}: {exc}; "
                         "use --port 0 to pick a random free port") from exc
    return _serve_workbench_server(server, None, open_browser=open_browser,
                                   progress=progress,
                                   target=f"{count} registered workspace(s)")
