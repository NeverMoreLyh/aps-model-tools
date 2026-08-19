"""文档导出模块：从 APS 元数据 SQLite 索引生成 Markdown 格式文档。

迁移自 aps-model-util 的 docz FreeMarker 模板系列（table.ftl / dict.ftl / trans.ftl /
nsql.ftl / schema.ftl 等），用 Python 标准库重新实现，不依赖 FreeMarker 或 Java。

支持的文档类型：
  - table:    表定义文档（字段、类型、主键、索引）
  - dict:     字典/限制类型文档（枚举值、基类型）
  - schema:   Schema 文档（复杂类型、限制类型列表）
  - trans:    交易流程文档（基本信息、接口）
  - nsql:     命名 SQL 文档（SQL 定义列表）
  - service:  服务类型文档
  - all:      全量文档（以上所有类型的合集）
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import connect, find_nodes


@dataclass
class DocExportReport:
    doc_type: str
    tables_exported: int = 0
    dicts_exported: int = 0
    schemas_exported: int = 0
    trans_exported: int = 0
    nsqls_exported: int = 0
    services_exported: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def export_document(
    conn: sqlite3.Connection,
    doc_type: str,
    queries: Optional[List[str]] = None,
) -> tuple[str, DocExportReport]:
    """生成 Markdown 格式的模型文档。

    Args:
        conn: SQLite 索引连接
        doc_type: 文档类型 (table/dict/schema/trans/nsql/service/all)
        queries: 可选的模型查询列表，为空则导出全部

    Returns:
        (markdown_text, report)
    """
    report = DocExportReport(doc_type=doc_type)
    sections: List[str] = []

    if doc_type in ("table", "all"):
        sections.append(_export_tables(conn, queries, report))
    if doc_type in ("dict", "schema", "all"):
        sections.append(_export_dicts(conn, queries, report))
    if doc_type in ("trans", "all"):
        sections.append(_export_transactions(conn, queries, report))
    if doc_type in ("nsql", "all"):
        sections.append(_export_named_sqls(conn, queries, report))
    if doc_type in ("service", "all"):
        sections.append(_export_services(conn, queries, report))

    return "\n\n".join(s for s in sections if s.strip()), report


def _export_tables(conn: sqlite3.Connection, queries: Optional[List[str]], report: DocExportReport) -> str:
    tables = _resolve_nodes(conn, "TABLE", queries)
    if not tables:
        return ""

    lines = ["# 表定义文档", ""]
    lines.append(f"> 共 {len(tables)} 张表")
    lines.append("")

    for table in tables:
        props = table.get("properties", {})
        tid = props.get("id") or table.get("full_id", "")
        longname = props.get("longname", "")
        desc = props.get("description", "")
        name = props.get("name", tid)
        abstract = props.get("abstract", "false")
        virtual = props.get("virtual", "false")
        table_type = props.get("tableType", "")
        extension = props.get("extension", "")

        lines.append(f"## {tid} {longname}")
        lines.append("")
        lines.append("| 属性 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| ID | {tid} |")
        lines.append(f"| 物理表名 | {name} |")
        lines.append(f"| 中文名 | {longname} |")
        lines.append(f"| 描述 | {desc} |")
        lines.append(f"| 表类型 | {table_type} |")
        lines.append(f"| 抽象表 | {_bool_str(abstract)} |")
        lines.append(f"| 虚拟表 | {_bool_str(virtual)} |")
        if extension:
            lines.append(f"| 父表 | {extension} |")
        lines.append("")

        # 字段列表
        fields = _get_children_by_kind(conn, table["stable_id"], "FIELD")
        if fields:
            lines.append("### 字段定义")
            lines.append("")
            lines.append("| 序号 | ID | 中文名 | 类型 | 长度 | 精度 | 可空 | 主键 | 默认值 | 固定值 | 描述 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for i, f in enumerate(fields, 1):
                fp = f.get("properties", {})
                lines.append(
                    f"| {i} | {fp.get('id', '')} | {fp.get('longname', '')} | "
                    f"{fp.get('type', '')} | {fp.get('maxLength', '')} | "
                    f"{fp.get('fractionDigits', '')} | {_bool_str(fp.get('nullable', 'true'))} | "
                    f"{_bool_str(fp.get('primarykey', 'false'))} | "
                    f"{fp.get('defaultValue', '')} | {fp.get('fixedValue', '')} | "
                    f"{fp.get('description', '')} |"
                )
            lines.append("")

        # 索引列表
        indexes = _get_children_by_kind(conn, table["stable_id"], "INDEX")
        if indexes:
            lines.append("### 索引定义")
            lines.append("")
            lines.append("| ID | 名称 | 类型 | 索引字段 |")
            lines.append("|---|---|---|---|")
            for idx in indexes:
                ip = idx.get("properties", {})
                lines.append(
                    f"| {ip.get('id', '')} | {ip.get('longname', '')} | "
                    f"{ip.get('type', '')} | {ip.get('fields', '')} |"
                )
            lines.append("")

        report.tables_exported += 1

    return "\n".join(lines)


def _export_dicts(conn: sqlite3.Connection, queries: Optional[List[str]], report: DocExportReport) -> str:
    # 字典/限制类型
    rtypes = _resolve_nodes(conn, "RESTRICTION_TYPE", queries)
    ctypes = _resolve_nodes(conn, "DICTIONARY", queries)

    if not rtypes and not ctypes:
        return ""

    lines = ["# 字典与限制类型文档", ""]

    if rtypes:
        lines.append(f"> 共 {len(rtypes)} 个限制类型")
        lines.append("")
        for rt in rtypes:
            props = rt.get("properties", {})
            rid = props.get("id", "")
            longname = props.get("longname", "")
            desc = props.get("description", "")
            base = props.get("base", "")
            max_len = props.get("maxLength", "")
            by_char = props.get("byCharacter", "")

            lines.append(f"## {rid} {longname}")
            lines.append("")
            lines.append("| 属性 | 值 |")
            lines.append("|---|---|")
            lines.append(f"| ID | {rid} |")
            lines.append(f"| 中文名 | {longname} |")
            lines.append(f"| 描述 | {desc} |")
            lines.append(f"| 基类型 | {base} |")
            lines.append(f"| 最大长度 | {max_len} |")
            lines.append(f"| 按字符 | {_bool_str(by_char)} |")
            lines.append("")

            # 枚举值
            enums = _get_children_by_kind(conn, rt["stable_id"], "ENUM_VALUE")
            if enums:
                lines.append("### 枚举值")
                lines.append("")
                lines.append("| ID | 名称 | 值 | 描述 |")
                lines.append("|---|---|---|---|")
                for ev in enums:
                    ep = ev.get("properties", {})
                    lines.append(
                        f"| {ep.get('id', '')} | {ep.get('longname', '')} | "
                        f"{ep.get('value', '')} | {ep.get('description', '')} |"
                    )
                lines.append("")

            report.dicts_exported += 1

    if ctypes:
        lines.append("---")
        lines.append("")
        lines.append(f"> 共 {len(ctypes)} 个复杂类型/字典")
        lines.append("")
        for ct in ctypes:
            props = ct.get("properties", {})
            cid = props.get("id", "")
            longname = props.get("longname", "")
            desc = props.get("description", "")
            clazz = props.get("clazz", "")
            pkg = props.get("package", "")

            lines.append(f"## {cid} {longname}")
            lines.append("")
            lines.append("| 属性 | 值 |")
            lines.append("|---|---|")
            lines.append(f"| ID | {cid} |")
            lines.append(f"| 中文名 | {longname} |")
            lines.append(f"| 描述 | {desc} |")
            lines.append(f"| Java 包 | {pkg} |")
            lines.append(f"| Java 类 | {clazz} |")
            lines.append("")

            report.schemas_exported += 1

    return "\n".join(lines)


def _export_transactions(conn: sqlite3.Connection, queries: Optional[List[str]], report: DocExportReport) -> str:
    trans = _resolve_nodes(conn, "TRANSACTION", queries)
    if not trans:
        return ""

    lines = ["# 交易流程文档", ""]
    lines.append(f"> 共 {len(trans)} 个交易")
    lines.append("")

    for tr in trans:
        props = tr.get("properties", {})
        tid = props.get("id", "")
        longname = props.get("longname", "")
        kind = props.get("kind", "")
        desc = props.get("description", "")
        pkg = props.get("package", "")
        before = props.get("beforeProcedure", "")
        after = props.get("afterProcedure", "")
        error_proc = props.get("errorProcedure", "")

        lines.append(f"## {tid} {longname}")
        lines.append("")
        lines.append("| 属性 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 交易码 | {tid} |")
        lines.append(f"| 中文名 | {longname} |")
        lines.append(f"| 业务类别 | {kind} |")
        lines.append(f"| 描述 | {desc} |")
        lines.append(f"| Java 包 | {pkg} |")
        lines.append(f"| 前处理 | {before} |")
        lines.append(f"| 后处理 | {after} |")
        lines.append(f"| 异常处理 | {error_proc} |")
        lines.append("")

        # 接口字段
        for iface_kind, iface_name in [("SERVICE_OPERATION", "服务操作")]:
            ops = _get_children_by_kind(conn, tr["stable_id"], iface_kind)
            if ops:
                lines.append(f"### {iface_name}")
                lines.append("")
                lines.append("| ID | 名称 | 描述 |")
                lines.append("|---|---|---|")
                for op in ops:
                    op_props = op.get("properties", {})
                    lines.append(
                        f"| {op_props.get('id', '')} | {op_props.get('longname', '')} | "
                        f"{op_props.get('description', '')} |"
                    )
                lines.append("")

        report.trans_exported += 1

    return "\n".join(lines)


def _export_named_sqls(conn: sqlite3.Connection, queries: Optional[List[str]], report: DocExportReport) -> str:
    nsqls = _resolve_nodes(conn, "SQL_GROUP", queries)
    if not nsqls:
        return ""

    lines = ["# 命名 SQL 文档", ""]
    lines.append(f"> 共 {len(nsqls)} 个 SQL 组")
    lines.append("")

    for ns in nsqls:
        props = ns.get("properties", {})
        nid = props.get("id", "")
        longname = props.get("longname", "")
        desc = props.get("description", "")
        datasource = props.get("datasource", "")
        clazz = props.get("clazz", "")

        lines.append(f"## {nid} {longname}")
        lines.append("")
        lines.append("| 属性 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| ID | {nid} |")
        lines.append(f"| 中文名 | {longname} |")
        lines.append(f"| 描述 | {desc} |")
        lines.append(f"| 数据源 | {datasource} |")
        lines.append(f"| Java 类 | {clazz} |")
        lines.append("")

        # SQL 明细
        sqls = _get_children_by_kind(conn, ns["stable_id"], "NAMED_SQL")
        if sqls:
            lines.append("### SQL 明细")
            lines.append("")
            lines.append("| ID | 名称 | 方法 | 描述 |")
            lines.append("|---|---|---|---|")
            for sql in sqls:
                sp = sql.get("properties", {})
                lines.append(
                    f"| {sp.get('id', '')} | {sp.get('longname', '')} | "
                    f"{sp.get('method', '')} | {sp.get('description', '')} |"
                )
            lines.append("")

        report.nsqls_exported += 1

    return "\n".join(lines)


def _export_services(conn: sqlite3.Connection, queries: Optional[List[str]], report: DocExportReport) -> str:
    services = _resolve_nodes(conn, "SERVICE_TYPE", queries)
    if not services:
        return ""

    lines = ["# 服务类型文档", ""]
    lines.append(f"> 共 {len(services)} 个服务类型")
    lines.append("")

    for sv in services:
        props = sv.get("properties", {})
        sid = props.get("id", "")
        longname = props.get("longname", "")
        kind = props.get("kind", "")
        desc = props.get("description", "")
        pkg = props.get("package", "")
        category = props.get("category", "")

        lines.append(f"## {sid} {longname}")
        lines.append("")
        lines.append("| 属性 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 服务ID | {sid} |")
        lines.append(f"| 中文名 | {longname} |")
        lines.append(f"| 类别 | {category} |")
        lines.append(f"| 业务类型 | {kind} |")
        lines.append(f"| 描述 | {desc} |")
        lines.append(f"| Java 包 | {pkg} |")
        lines.append("")

        # 服务操作
        ops = _get_children_by_kind(conn, sv["stable_id"], "SERVICE_OPERATION")
        if ops:
            lines.append("### 服务操作")
            lines.append("")
            lines.append("| ID | 名称 | 描述 |")
            lines.append("|---|---|---|")
            for op in ops:
                op_props = op.get("properties", {})
                lines.append(
                    f"| {op_props.get('id', '')} | {op_props.get('longname', '')} | "
                    f"{op_props.get('description', '')} |"
                )
            lines.append("")

        report.services_exported += 1

    return "\n".join(lines)


# ─── 辅助方法 ───

def _resolve_nodes(conn: sqlite3.Connection, kind: str, queries: Optional[List[str]]) -> List[Dict[str, Any]]:
    """按 kind 和可选查询条件解析节点列表。"""
    if not queries:
        rows = conn.execute(
            "SELECT stable_id, kind, full_id, raw_id, properties_json FROM nodes WHERE kind=? ORDER BY full_id",
            (kind,),
        ).fetchall()
        result = []
        for row in rows:
            props = {}
            try:
                props = json.loads(row["properties_json"]) if row["properties_json"] else {}
            except (json.JSONDecodeError, TypeError):
                pass
            result.append({
                "stable_id": row["stable_id"],
                "kind": row["kind"],
                "full_id": row["full_id"],
                "raw_id": row["raw_id"],
                "properties": props,
            })
        return result

    # 有查询条件时使用 find_nodes（返回已解析的 properties dict）
    result = []
    seen = set()
    for q in queries:
        found = find_nodes(conn, q)
        for n in found:
            if n.get("kind") != kind:
                continue
            sid = n.get("stable_id", "")
            if sid in seen:
                continue
            seen.add(sid)
            result.append({
                "stable_id": sid,
                "kind": n.get("kind", kind),
                "full_id": n.get("full_id", ""),
                "raw_id": n.get("raw_id", ""),
                "properties": n.get("properties", {}),
            })
    return result


def _get_children_by_kind(conn: sqlite3.Connection, parent_stable_id: str, child_kind: str) -> List[Dict[str, Any]]:
    """获取指定父节点的某种子节点列表（递归查找多层子节点）。"""
    result: List[Dict[str, Any]] = []
    seen = set()

    def _walk(parent_sid: str, depth: int = 0) -> None:
        if depth > 5:
            return
        rows = conn.execute(
            """SELECT n.stable_id, n.kind, n.full_id, n.raw_id, n.properties_json
               FROM nodes n
               JOIN nodes parent ON n.owner_node_id = parent.id
               WHERE parent.stable_id = ?
               ORDER BY n.id""",
            (parent_sid,),
        ).fetchall()
        for row in rows:
            if row["kind"] == child_kind:
                if row["stable_id"] in seen:
                    continue
                seen.add(row["stable_id"])
                props = {}
                try:
                    props = json.loads(row["properties_json"]) if row["properties_json"] else {}
                except (json.JSONDecodeError, TypeError):
                    pass
                result.append({
                    "stable_id": row["stable_id"],
                    "kind": row["kind"],
                    "full_id": row["full_id"],
                    "raw_id": row["raw_id"],
                    "properties": props,
                })
            else:
                # 递归进入容器节点（如 FIELDS、INDEXES、SQL_GROUP 等）
                _walk(row["stable_id"], depth + 1)

    _walk(parent_stable_id)
    return result


def _bool_str(value: Any) -> str:
    """将布尔值转为中文是/否。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    s = str(value).strip().lower()
    if s in ("true", "1", "yes"):
        return "是"
    if s in ("false", "0", "no"):
        return "否"
    return s
