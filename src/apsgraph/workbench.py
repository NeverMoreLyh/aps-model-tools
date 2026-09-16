"""Read-only local web workbench over the APSGraph SQLite index.

Serves a single-page query UI from the standard library ``http.server`` and a
small JSON API.  The index database is always opened read-only and the server
binds to 127.0.0.1 only; the workbench never writes to the index, the
workspace, or any business repository.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .store import (
    NODE_SELECT, _row_to_dict, connect, find_nodes, get_stats, references,
)

WORKBENCH_PORT = 8321
PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

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
    "complex_type": {"kinds": {"COMPLEX_TYPE"}},
    "dict_element": {"kinds": {"ELEMENT"}},
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

# APS SimpleType built-in base types with their database column mappings
# (docs/aps-type-database-mapping.md, APS 6.51.173 baseline).
BASIC_TYPES: List[Dict[str, str]] = [
    {"name": "string", "java": "可变长字符串", "mysql": "varchar", "oracle": "varchar2", "postgresql": "varchar"},
    {"name": "fixString", "java": "定长字符串", "mysql": "char", "oracle": "char", "postgresql": "char"},
    {"name": "eString", "java": "数据库加密字符串", "mysql": "varchar", "oracle": "varchar2", "postgresql": "varchar"},
    {"name": "encString", "java": "加密字符串", "mysql": "varchar", "oracle": "varchar2", "postgresql": "varchar"},
    {"name": "cString", "java": "中文字符串", "mysql": "varchar", "oracle": "varchar2", "postgresql": "varchar"},
    {"name": "dateString", "java": "日期字符串", "mysql": "varchar", "oracle": "varchar2", "postgresql": "varchar"},
    {"name": "dateString8", "java": "8位日期字符串", "mysql": "date", "oracle": "date", "postgresql": "date"},
    {"name": "timeString17", "java": "时间字符串", "mysql": "datetime", "oracle": "timestamp", "postgresql": "datetime"},
    {"name": "boolean", "java": "布尔值", "mysql": "char", "oracle": "char", "postgresql": "boolean"},
    {"name": "int", "java": "整型", "mysql": "int", "oracle": "number", "postgresql": "integer"},
    {"name": "integer", "java": "整型（兼容int）", "mysql": "int", "oracle": "number", "postgresql": "integer"},
    {"name": "long", "java": "长整型", "mysql": "bigint", "oracle": "number", "postgresql": "bigint"},
    {"name": "double", "java": "浮点/数值", "mysql": "decimal", "oracle": "number", "postgresql": "decimal"},
    {"name": "decimal", "java": "可带小数数值", "mysql": "decimal", "oracle": "number", "postgresql": "decimal"},
    {"name": "amount", "java": "金额", "mysql": "decimal", "oracle": "number", "postgresql": "decimal"},
    {"name": "date", "java": "日期", "mysql": "date", "oracle": "date", "postgresql": "date"},
    {"name": "time", "java": "时间", "mysql": "time", "oracle": "date", "postgresql": "time"},
    {"name": "dateTime", "java": "时间戳", "mysql": "dateTime", "oracle": "date", "postgresql": "timestamp"},
    {"name": "timestamp", "java": "时间戳", "mysql": "timestamp", "oracle": "timestamp", "postgresql": "timestamp"},
    {"name": "clob", "java": "大字符串", "mysql": "text", "oracle": "clob", "postgresql": "text"},
    {"name": "blob", "java": "二进制大对象", "mysql": "blob", "oracle": "blob", "postgresql": "bytea"},
    {"name": "schema", "java": "Schema字符串", "mysql": "varchar", "oracle": "varchar2", "postgresql": "varchar"},
    {"name": "byte", "java": "字节", "mysql": "", "oracle": "", "postgresql": ""},
    {"name": "expr", "java": "表达式字符串", "mysql": "", "oracle": "", "postgresql": ""},
    {"name": "cursor", "java": "ResultSet游标", "mysql": "", "oracle": "", "postgresql": ""},
    {"name": "resultSet", "java": "ResultSet游标", "mysql": "", "oracle": "", "postgresql": ""},
    {"name": "map", "java": "Map", "mysql": "", "oracle": "", "postgresql": ""},
    {"name": "object", "java": "Object", "mysql": "", "oracle": "", "postgresql": ""},
    {"name": "class", "java": "Java Class", "mysql": "", "oracle": "", "postgresql": ""},
]


def _like_clause(query: str, dimension: str) -> Tuple[str, List[Any]]:
    columns = LIKE_COLUMNS.get(dimension)
    if columns is None:
        raise ValueError(f"unknown search dimension: {dimension}")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
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
    if group not in KIND_GROUPS and group not in {"top", "basetype"}:
        raise ValueError(f"unknown query group: {group}")
    if dimension and dimension not in LIKE_COLUMNS:
        raise ValueError(f"unknown search dimension: {dimension}")
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    page = max(1, int(page))

    if group == "basetype":
        needle = query.casefold()
        items = [entry for entry in BASIC_TYPES
                 if not needle or needle in entry["name"].casefold()
                 or needle in entry["java"].casefold()]
        return {"group": group, "query": query, "total": len(items), "page": 1,
                "page_size": max(len(items), 1), "results": items}

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


def _table_detail(conn: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
    return {
        "fields": _descendants_of_kind(conn, node_id, "FIELD"),
        "indexes": [node for node in _descendants_of_kind(conn, node_id, "INDEX")
                    if node.get("xml_tag") == "index"],
        "odbindexes": _descendants_of_kind(conn, node_id, "ODBINDEX"),
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
            """select n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
            from nodes n where n.owner_node_id=? order by n.id""", (group["id"],)).fetchall()
        groups.append({"node": group, "errors": [_row_to_dict(row) for row in rows]})
    orphans = conn.execute(
        """select n.stable_id,n.kind,n.raw_id,n.full_id,n.xml_tag,n.properties_json
        from nodes n where n.owner_node_id=? and n.kind='ERROR' order by n.id""",
        (node_id,)).fetchall()
    if orphans:
        groups.append({"node": None, "errors": [_row_to_dict(row) for row in orphans]})
    return {"groups": groups}


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
    if kind == "BATCH_TRANSACTION":
        return _batch_detail(conn, node_id)
    if kind == "ERRORCONF":
        return _error_code_detail(conn, node_id)
    if kind == "ENUM_VALUE":
        return {"enum_values": []}
    if kind in {"RESTRICTION_TYPE", "DICTIONARY", "COMPLEX_TYPE"}:
        return {"elements": _descendants_of_kind(conn, node_id, "ELEMENT"),
                "enum_values": _descendants_of_kind(conn, node_id, "ENUM_VALUE")}
    return {}


def node_detail(conn: sqlite3.Connection, stable_id: str) -> Dict[str, Any]:
    """Full detail payload: node, direct children, structured view, and edges.

    ``stable_id`` also accepts exact full_id or raw_id values so UI links such
    as flow ``serviceName`` targets resolve to a single node.
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
    node["properties"] = node.get("properties") or {}
    return {"node": node, "children": children, "detail": detail,
            "out_edges": out_edges, "in_edges": in_edges}


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "apsgraph-workbench"

    @property
    def db_path(self) -> Path:
        return self.server.db_path  # type: ignore[attr-defined]

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

    def _api_search(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        conn = connect(self.db_path, read_only=True)
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

    def _api_node(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        stable_id = params.get("id", [""])[0]
        if not stable_id:
            raise ValueError("missing node id")
        conn = connect(self.db_path, read_only=True)
        try:
            return node_detail(conn, stable_id)
        finally:
            conn.close()

    def _api_children(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        stable_id = params.get("id", [""])[0]
        if not stable_id:
            raise ValueError("missing node id")
        conn = connect(self.db_path, read_only=True)
        try:
            return {"id": stable_id, "children": child_nodes(conn, stable_id)}
        finally:
            conn.close()

    def _handle_api(self, path: str, params: Dict[str, List[str]]) -> None:
        if path == "/api/stats":
            conn = connect(self.db_path, read_only=True)
            try:
                self._send_json({"stats": get_stats(conn),
                                 "top_groups": top_groups(conn)})
            finally:
                conn.close()
        elif path == "/api/search":
            self._send_json(self._api_search(params))
        elif path == "/api/enums":
            conn = connect(self.db_path, read_only=True)
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
            self._send_json(self._api_node(params))
        elif path == "/api/children":
            self._send_json(self._api_children(params))
        elif path == "/api/basetypes":
            self._send_json({"group": "basetype", "results": BASIC_TYPES})
        elif path == "/api/top-groups":
            conn = connect(self.db_path, read_only=True)
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


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db_path: Path, port: int = WORKBENCH_PORT):
        super().__init__(("127.0.0.1", port), WorkbenchHandler)
        self.db_path = Path(db_path)


def serve_workbench(db_path: Path, port: int = WORKBENCH_PORT,
                    open_browser: bool = True, progress=None) -> int:
    """Start the local workbench server; blocks until interrupted."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(
            f"index database does not exist: {db_path}; run 'apsgraph scan' first")
    connect(db_path, read_only=True).close()
    server = WorkbenchServer(db_path, port)
    url = f"http://127.0.0.1:{server.server_port}/"
    if progress:
        progress(f"workbench serving {db_path} at {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if progress:
            progress("workbench stopped")
    finally:
        server.server_close()
    return 0
