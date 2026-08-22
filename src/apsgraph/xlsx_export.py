"""Excel 文档导出模块：从 APS 元数据 SQLite 索引按项目聚合成 .xlsx 文件。

迁移自 adp-ide 的 AbstractExcelProcessor 系列实现类，用 Python + openpyxl 重新实现。

支持的导出类型：
  - table:     表定义（字段、类型、索引）
  - table_list: 表名清单
  - dict:      数据字典（限制类型、枚举值）
  - dict_ref:  字典基础类型引用总览
  - enum:      枚举类型
  - trans:     交易流程接口
  - nsql:      命名 SQL
  - service:   服务类型
  - service_v2: Service V2
  - params:    参数表
  - error_code: 错误码
  - batch_tran: 批量交易
  - all:       以上全部（按类型分 sheet）

按项目聚合：路径的第一级目录（如 ap-parent、aggr-parent）作为项目名，
每个项目生成一个 .xlsx 文件。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

from .store import connect, find_nodes


# ─── 样式常量 ───
# openpyxl 是可选依赖。不要在导入模块时创建样式对象，否则其他 CLI 命令
# 也会因为 openpyxl 缺失而无法启动。

if HAS_OPENPYXL:
    _TITLE_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    _HEADER_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
    _INFO_FILL = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    _THIN_BORDER = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    _TITLE_FONT = Font(name="Arial", size=12, bold=True, color="FFFFFF")
    _HEADER_FONT = Font(name="Arial", size=10, bold=True)
    _INFO_FONT = Font(name="Arial", size=10, bold=True)
    _DATA_FONT = Font(name="Arial", size=10)
    _WRAP_ALIGN = Alignment(wrap_text=True, vertical="top")
    _CENTER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
else:
    _TITLE_FILL = None
    _HEADER_FILL = None
    _INFO_FILL = None
    _THIN_BORDER = None
    _TITLE_FONT = None
    _HEADER_FONT = None
    _INFO_FONT = None
    _DATA_FONT = None
    _WRAP_ALIGN = None
    _CENTER_ALIGN = None


@dataclass
class ExcelExportReport:
    project: str
    output_file: str
    sheets_generated: int = 0
    tables: int = 0
    dicts: int = 0
    enums: int = 0
    trans: int = 0
    nsqls: int = 0
    services: int = 0
    params: int = 0
    error_codes: int = 0
    batch_trans: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def export_excel(
    conn: sqlite3.Connection,
    output_dir: str,
    doc_types: Optional[List[str]] = None,
    projects: Optional[List[str]] = None,
) -> List[ExcelExportReport]:
    """按项目聚合并导出 Excel 文件。

    Args:
        conn: SQLite 索引连接
        output_dir: 输出目录
        doc_types: 导出类型列表，None=全部
        projects: 项目过滤列表，None=全部项目

    Returns:
        每个项目的导出报告
    """
    if not HAS_OPENPYXL:
        raise ImportError("openpyxl is required for Excel export. Install with: pip install openpyxl")

    if doc_types is None:
        doc_types = ["table", "table_list", "dict", "dict_ref", "enum",
                     "trans", "nsql", "service", "service_v2", "params",
                     "error_code", "batch_tran"]

    # 获取项目列表
    project_map = _get_projects(conn, projects)
    reports = []

    for project, file_ids in project_map.items():
        report = ExcelExportReport(project=project, output_file="")
        wb = Workbook()
        # 删除默认 sheet
        if "Sheet" in wb.sheetnames:
            del wb["Sheet"]

        for doc_type in doc_types:
            _export_by_type(conn, wb, doc_type, file_ids, report)

        if wb.sheetnames:
            out_path = Path(output_dir) / f"{project}.xlsx"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            wb.save(str(out_path))
            report.output_file = str(out_path)
            report.sheets_generated = len(wb.sheetnames)
        else:
            report.warnings.append(f"No data exported for project {project}")

        reports.append(report)

    return reports


# ─── 项目聚合 ───

def _get_projects(conn: sqlite3.Connection, filter_projects: Optional[List[str]]) -> Dict[str, List[int]]:
    """从索引文件路径提取项目分组，返回 {项目名: [file_id列表]}。"""
    rows = conn.execute("SELECT id, path FROM model_files").fetchall()
    result: Dict[str, List[int]] = {}
    for row in rows:
        fid, path = row["id"], row["path"]
        # jar:: 前缀的跳过
        if path.startswith("jar::"):
            project = "framework"
        else:
            parts = path.split("/", 1)
            project = parts[0] if len(parts) > 1 else "unknown"
        if filter_projects and project not in filter_projects:
            continue
        result.setdefault(project, []).append(fid)
    return dict(sorted(result.items()))


# ─── 导出分发 ───

def _export_by_type(
    conn: sqlite3.Connection,
    wb: Workbook,
    doc_type: str,
    file_ids: List[int],
    report: ExcelExportReport,
) -> None:
    """按类型导出到 Workbook。"""
    if doc_type == "table":
        _export_tables(conn, wb, file_ids, report)
    elif doc_type == "table_list":
        _export_table_list(conn, wb, file_ids, report)
    elif doc_type == "dict":
        _export_dicts(conn, wb, file_ids, report)
    elif doc_type == "dict_ref":
        _export_dict_refs(conn, wb, file_ids, report)
    elif doc_type == "enum":
        _export_enums(conn, wb, file_ids, report)
    elif doc_type == "trans":
        _export_trans(conn, wb, file_ids, report)
    elif doc_type == "nsql":
        _export_nsqls(conn, wb, file_ids, report)
    elif doc_type == "service":
        _export_services(conn, wb, file_ids, report, service_v2=False)
    elif doc_type == "service_v2":
        _export_services(conn, wb, file_ids, report, service_v2=True)
    elif doc_type == "params":
        _export_params(conn, wb, file_ids, report)
    elif doc_type == "error_code":
        _export_error_codes(conn, wb, file_ids, report)
    elif doc_type == "batch_tran":
        _export_batch_trans(conn, wb, file_ids, report)


# ─── 辅助方法 ───

def _resolve_nodes_by_file(conn: sqlite3.Connection, kind: str, file_ids: List[int]) -> List[Dict[str, Any]]:
    """按 file_ids 过滤指定 kind 的节点。"""
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT stable_id, kind, full_id, raw_id, properties_json, file_id FROM nodes WHERE kind=? AND file_id IN ({placeholders}) ORDER BY full_id",
        [kind] + file_ids,
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
            "file_id": row["file_id"],
        })
    return result


def _get_children(conn: sqlite3.Connection, parent_stable_id: str, child_kind: str) -> List[Dict[str, Any]]:
    """递归获取子节点。"""
    result: List[Dict[str, Any]] = []
    seen = set()

    def _walk(parent_sid: str, depth: int = 0) -> None:
        if depth > 5:
            return
        rows = conn.execute(
            "SELECT n.stable_id, n.kind, n.full_id, n.raw_id, n.properties_json "
            "FROM nodes n JOIN nodes parent ON n.owner_node_id = parent.id "
            "WHERE parent.stable_id = ? ORDER BY n.id",
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
                _walk(row["stable_id"], depth + 1)

    _walk(parent_stable_id)
    return result


def _safe_sheet_name(name: str, max_len: int = 31) -> str:
    """清理 sheet 名称（Excel 限制 31 字符，禁用特殊字符）。"""
    for ch in '[]:*?/\\':
        name = name.replace(ch, "_")
    if len(name) > max_len:
        name = name[:max_len]
    return name or "sheet"


def _write_title_row(ws, title: str, col_count: int, row_num: int = 1) -> int:
    """写标题行（合并单元格）。"""
    ws.cell(row=row_num, column=1, value=title)
    if col_count > 1:
        ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=col_count)
    cell = ws.cell(row=row_num, column=1)
    cell.font = _TITLE_FONT
    cell.fill = _TITLE_FILL
    cell.alignment = _CENTER_ALIGN
    ws.row_dimensions[row_num].height = 25
    return row_num + 1


def _write_header_row(ws, headers: List[str], row_num: int) -> int:
    """写表头行。"""
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=row_num, column=col, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _CENTER_ALIGN
        cell.border = _THIN_BORDER
    ws.row_dimensions[row_num].height = 20
    return row_num + 1


def _write_info_row(ws, label: str, value: str, col_count: int, row_num: int) -> int:
    """写信息行（标签+值合并）。"""
    cell = ws.cell(row=row_num, column=1, value=label)
    cell.font = _INFO_FONT
    cell.fill = _INFO_FILL
    cell.border = _THIN_BORDER
    cell2 = ws.cell(row=row_num, column=2, value=value or "")
    cell2.font = _DATA_FONT
    cell2.alignment = _WRAP_ALIGN
    cell2.border = _THIN_BORDER
    if col_count > 2:
        ws.merge_cells(start_row=row_num, start_column=2, end_row=row_num, end_column=col_count)
    return row_num + 1


def _write_data_row(ws, values: List[Any], row_num: int) -> int:
    """写数据行。"""
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=row_num, column=col, value=str(val) if val is not None else "")
        cell.font = _DATA_FONT
        cell.alignment = _WRAP_ALIGN
        cell.border = _THIN_BORDER
    return row_num + 1


def _auto_width(ws, max_width: int = 50) -> None:
    """自适应列宽。"""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val_len = len(str(cell.value or ""))
                if val_len > max_len:
                    max_len = val_len
            except Exception:
                pass
        adjusted = min(max_len + 2, max_width)
        ws.column_dimensions[col_letter].width = adjusted


def _bool_cn(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "是" if val else "否"
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return "是"
    if s in ("false", "0", "no"):
        return "否"
    return s


# ─── 各类型导出实现 ───

def _export_tables(conn, wb, file_ids, report):
    tables = _resolve_nodes_by_file(conn, "TABLE", file_ids)
    if not tables:
        return

    # 总览 sheet
    overview_name = _safe_sheet_name("表定义总览")
    ws = wb.create_sheet(overview_name)
    row = _write_title_row(ws, "表定义总览", 4)
    row = _write_header_row(ws, ["类别", "表英文名", "表中文名", "描述"], row)

    # 用于详细 sheet 的唯一名
    used_names = set()
    for table in tables:
        props = table["properties"]
        tid = props.get("id", table.get("full_id", ""))
        longname = props.get("longname", "")
        desc = props.get("description", "")
        row = _write_data_row(ws, ["TABLE", tid, longname, desc], row)
        report.tables += 1

    _auto_width(ws)

    # 详细 sheet — 按表分组
    # 获取同一文件下的 TABLE 节点，按文件生成 sheet
    file_groups: Dict[int, List] = {}
    for table in tables:
        file_groups.setdefault(table["file_id"], []).append(table)

    for fid, file_tables in file_groups.items():
        file_row = conn.execute("SELECT path FROM model_files WHERE id=?", (fid,)).fetchone()
        file_name = Path(file_row["path"]).stem if file_row else f"file_{fid}"
        sheet_name = _safe_sheet_name(file_name)
        # 去重
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        for table in file_tables:
            props = table["properties"]
            tid = props.get("id", "")
            longname = props.get("longname", "")
            name = props.get("name", tid)
            table_type = props.get("tableType", "")
            abstract = props.get("abstract", "false")
            virtual = props.get("virtual", "false")

            r = _write_title_row(ws, f"{tid} {longname}", 9, r)
            r = _write_info_row(ws, "表ID", tid, 9, r)
            r = _write_info_row(ws, "物理表名", name, 9, r)
            r = _write_info_row(ws, "中文名", longname, 9, r)
            r = _write_info_row(ws, "表类型", table_type, 9, r)
            r = _write_info_row(ws, "抽象表", _bool_cn(abstract), 9, r)
            r = _write_info_row(ws, "虚拟表", _bool_cn(virtual), 9, r)
            r += 0  # 空行

            # 字段
            fields = _get_children(conn, table["stable_id"], "FIELD")
            if fields:
                r = _write_header_row(ws,
                    ["字段名", "中文名", "类型", "长度", "精度", "可空", "主键", "默认值", "描述"], r)
                for f in fields:
                    fp = f["properties"]
                    r = _write_data_row(ws, [
                        fp.get("id", ""), fp.get("longname", ""), fp.get("type", ""),
                        fp.get("maxLength", ""), fp.get("fractionDigits", ""),
                        _bool_cn(fp.get("nullable", "true")), _bool_cn(fp.get("primarykey", "false")),
                        fp.get("defaultValue", ""), fp.get("description", ""),
                    ], r)
                r += 0

            # 索引
            indexes = _get_children(conn, table["stable_id"], "INDEX")
            if indexes:
                r = _write_header_row(ws, ["索引ID", "类型", "字段"], r)
                for idx in indexes:
                    ip = idx["properties"]
                    r = _write_data_row(ws, [ip.get("id", ""), ip.get("type", ""), ip.get("fields", "")], r)
                r += 0

        _auto_width(ws)


def _export_table_list(conn, wb, file_ids, report):
    tables = _resolve_nodes_by_file(conn, "TABLE", file_ids)
    if not tables:
        return
    ws = wb.create_sheet(_safe_sheet_name("表名清单"))
    row = _write_title_row(ws, "表名清单", 3)
    row = _write_header_row(ws, ["序号", "表英文名", "表中文名"], row)
    for i, table in enumerate(tables, 1):
        props = table["properties"]
        row = _write_data_row(ws, [i, props.get("id", ""), props.get("longname", "")], row)
    _auto_width(ws)


def _export_dicts(conn, wb, file_ids, report):
    rtypes = _resolve_nodes_by_file(conn, "RESTRICTION_TYPE", file_ids)
    ctypes = _resolve_nodes_by_file(conn, "DICTIONARY", file_ids)
    if not rtypes and not ctypes:
        return

    # 限制类型/枚举 sheet
    if rtypes:
        ws = wb.create_sheet(_safe_sheet_name("数据字典"))
        r = _write_title_row(ws, "限制类型与枚举", 6)
        r = _write_header_row(ws, ["限制类型ID", "中文名称", "基础类型", "最大长度", "按字符", "描述"], r)
        for rt in rtypes:
            props = rt["properties"]
            r = _write_data_row(ws, [
                props.get("id", ""), props.get("longname", ""), props.get("base", ""),
                props.get("maxLength", ""), _bool_cn(props.get("byCharacter", "")),
                props.get("description", ""),
            ], r)

            # 枚举值
            enums = _get_children(conn, rt["stable_id"], "ENUM_VALUE")
            if enums:
                r = _write_header_row(ws, ["  枚举ID", "  枚举名称", "  枚举值", "  描述", "", ""], r)
                for ev in enums:
                    ep = ev["properties"]
                    r = _write_data_row(ws, [
                        ep.get("id", ""), ep.get("longname", ""), ep.get("value", ""),
                        ep.get("description", ""), "", "",
                    ], r)
            r += 0
        report.dicts += len(rtypes)
        _auto_width(ws)

    # 复杂类型 sheet
    if ctypes:
        ws = wb.create_sheet(_safe_sheet_name("复杂类型"))
        r = _write_title_row(ws, "复杂类型/字典", 5)
        r = _write_header_row(ws, ["ID", "中文名称", "Java包", "Java类", "描述"], r)
        for ct in ctypes:
            props = ct["properties"]
            r = _write_data_row(ws, [
                props.get("id", ""), props.get("longname", ""), props.get("package", ""),
                props.get("clazz", ""), props.get("description", ""),
            ], r)
        report.enums += len(ctypes)
        _auto_width(ws)


def _export_dict_refs(conn, wb, file_ids, report):
    # 引用总览
    elements = _resolve_nodes_by_file(conn, "ELEMENT", file_ids)
    if not elements:
        return
    ws = wb.create_sheet(_safe_sheet_name("字典引用总览"))
    r = _write_title_row(ws, "字典基础类型引用总览", 4)
    r = _write_header_row(ws, ["字段ID", "类型", "字典ID", "中文名称"], r)
    for elem in elements:
        props = elem["properties"]
        ref = props.get("ref", "")
        if not ref:
            continue
        r = _write_data_row(ws, [
            props.get("id", ""), props.get("type", ""), ref, props.get("longname", ""),
        ], r)
    _auto_width(ws)


def _export_enums(conn, wb, file_ids, report):
    rtypes = _resolve_nodes_by_file(conn, "RESTRICTION_TYPE", file_ids)
    enums_only = [rt for rt in rtypes if _get_children(conn, rt["stable_id"], "ENUM_VALUE")]
    if not enums_only:
        return
    ws = wb.create_sheet(_safe_sheet_name("枚举类型"))
    r = _write_title_row(ws, "枚举类型列表", 8)
    r = _write_header_row(ws, ["类型名称", "类型ID", "基础类型", "最小长度", "最大长度", "精度", "格式", "描述"], r)
    for rt in enums_only:
        props = rt["properties"]
        r = _write_data_row(ws, [
            props.get("longname", ""), props.get("id", ""), props.get("base", ""),
            props.get("minLength", ""), props.get("maxLength", ""),
            props.get("fractionDigits", ""), props.get("format", ""), props.get("description", ""),
        ], r)
        # 枚举值子表
        enums = _get_children(conn, rt["stable_id"], "ENUM_VALUE")
        if enums:
            r = _write_header_row(ws, ["  字段", "  枚举值", "  中文名", "  描述", "", "", "", ""], r)
            for ev in enums:
                ep = ev["properties"]
                r = _write_data_row(ws, [
                    ep.get("id", ""), ep.get("value", ""), ep.get("longname", ""),
                    ep.get("description", ""), "", "", "", "",
                ], r)
        r += 0
    _auto_width(ws)


def _export_trans(conn, wb, file_ids, report):
    trans = _resolve_nodes_by_file(conn, "TRANSACTION", file_ids)
    if not trans:
        return

    # 总览
    ws = wb.create_sheet(_safe_sheet_name("交易总览"))
    r = _write_title_row(ws, "交易流程总览", 2)
    r = _write_header_row(ws, ["交易名称", "交易码"], r)
    for tr in trans:
        props = tr["properties"]
        r = _write_data_row(ws, [props.get("longname", ""), props.get("id", "")], r)
    _auto_width(ws)

    # 详细
    used_names = set()
    for tr in trans:
        props = tr["properties"]
        tid = props.get("id", "")
        longname = props.get("longname", tid)
        sheet_name = _safe_sheet_name(f"{tid}_{longname}")
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        r = _write_title_row(ws, f"{tid} {longname}", 11, r)
        r = _write_info_row(ws, "交易中文名", longname, 11, r)
        r = _write_info_row(ws, "交易ID", tid, 11, r)
        r = _write_info_row(ws, "业务类别", props.get("kind", ""), 11, r)
        r = _write_info_row(ws, "描述", props.get("description", ""), 11, r)
        r = _write_info_row(ws, "Java包", props.get("package", ""), 11, r)
        r += 0

        # 接口字段（输入/输出/属性）
        for iface_kind, iface_label in [("SERVICE_OPERATION", "服务操作")]:
            ops = _get_children(conn, tr["stable_id"], iface_kind)
            if ops:
                r = _write_header_row(ws, [f"{iface_label}ID", "名称", "描述"], r)
                for op in ops:
                    op_props = op["properties"]
                    r = _write_data_row(ws, [
                        op_props.get("id", ""), op_props.get("longname", ""), op_props.get("description", ""),
                    ], r)
                r += 0

        report.trans += 1
        _auto_width(ws)


def _export_nsqls(conn, wb, file_ids, report):
    nsqls = _resolve_nodes_by_file(conn, "SQL_GROUP", file_ids)
    if not nsqls:
        return

    ws = wb.create_sheet(_safe_sheet_name("命名SQL总览"))
    r = _write_title_row(ws, "命名SQL总览", 3)
    r = _write_header_row(ws, ["命名SQL类别", "命名SQL中文名称", "命名SQLID"], r)
    for ns in nsqls:
        props = ns["properties"]
        r = _write_data_row(ws, [props.get("id", ""), props.get("longname", ""), props.get("id", "")], r)
    _auto_width(ws)

    used_names = set()
    for ns in nsqls:
        props = ns["properties"]
        nid = props.get("id", "")
        longname = props.get("longname", nid)
        sheet_name = _safe_sheet_name(f"{nid}_{longname}")
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        r = _write_title_row(ws, f"{nid} {longname}", 6, r)
        r = _write_info_row(ws, "中文名称", longname, 6, r)
        r = _write_info_row(ws, "命名SQLID", nid, 6, r)
        r = _write_info_row(ws, "数据源", props.get("datasource", ""), 6, r)
        r = _write_info_row(ws, "Java类", props.get("clazz", ""), 6, r)
        r += 0

        # SQL 明细
        sqls = _get_children(conn, ns["stable_id"], "NAMED_SQL")
        if sqls:
            r = _write_header_row(ws, ["SQL ID", "名称", "方法", "描述"], r)
            for sql in sqls:
                sp = sql["properties"]
                r = _write_data_row(ws, [sp.get("id", ""), sp.get("longname", ""), sp.get("method", ""), sp.get("description", "")], r)
            r += 0

        report.nsqls += 1
        _auto_width(ws)


def _export_services(conn, wb, file_ids, report, service_v2=False):
    kind = "SERVICE_TYPE"
    services = _resolve_nodes_by_file(conn, kind, file_ids)
    if not services:
        return

    prefix = "ServiceV2" if service_v2 else "服务类型"
    ws = wb.create_sheet(_safe_sheet_name(f"{prefix}总览"))
    r = _write_title_row(ws, f"{prefix}总览", 3)
    r = _write_header_row(ws, [f"{prefix}名称", "中文名称", "ID"], r)
    for sv in services:
        props = sv["properties"]
        r = _write_data_row(ws, [props.get("longname", ""), props.get("id", "")], r)
    _auto_width(ws)

    used_names = set()
    for sv in services:
        props = sv["properties"]
        sid = props.get("id", "")
        longname = props.get("longname", sid)
        sheet_name = _safe_sheet_name(f"{sid}_{longname}")
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        r = _write_title_row(ws, f"{sid} {longname}", 7, r)
        r = _write_info_row(ws, "服务中文名", longname, 7, r)
        r = _write_info_row(ws, "服务ID", sid, 7, r)
        r = _write_info_row(ws, "服务类别", props.get("category", ""), 7, r)
        r = _write_info_row(ws, "业务类型", props.get("kind", ""), 7, r)
        r = _write_info_row(ws, "描述", props.get("description", ""), 7, r)
        r = _write_info_row(ws, "Java包", props.get("package", ""), 7, r)
        r += 0

        # 服务操作
        ops = _get_children(conn, sv["stable_id"], "SERVICE_OPERATION")
        if ops:
            r = _write_header_row(ws, ["操作ID", "操作名称", "描述"], r)
            for op in ops:
                op_props = op["properties"]
                r = _write_data_row(ws, [op_props.get("id", ""), op_props.get("longname", ""), op_props.get("description", "")], r)
            r += 0

        if service_v2:
            report.services += 1
        else:
            report.services += 1
        _auto_width(ws)


def _export_params(conn, wb, file_ids, report):
    tables = _resolve_nodes_by_file(conn, "TABLE", file_ids)
    # 过滤参数表：文件后缀为 .parms.xml
    param_tables = []
    for t in tables:
        file_row = conn.execute("SELECT path FROM model_files WHERE id=?", (t["file_id"],)).fetchone()
        if file_row and file_row["path"].endswith(".parms.xml"):
            param_tables.append(t)

    if not param_tables:
        return

    ws = wb.create_sheet(_safe_sheet_name("参数表总览"))
    r = _write_title_row(ws, "参数表总览", 3)
    r = _write_header_row(ws, ["参数配置ID", "中文名称", "描述"], r)
    for t in param_tables:
        props = t["properties"]
        r = _write_data_row(ws, [props.get("id", ""), props.get("longname", ""), props.get("description", "")], r)
    _auto_width(ws)

    used_names = set()
    for t in param_tables:
        props = t["properties"]
        tid = props.get("id", "")
        longname = props.get("longname", tid)
        sheet_name = _safe_sheet_name(f"{tid}_{longname}")
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        r = _write_title_row(ws, f"{tid} {longname}", 8, r)
        r = _write_info_row(ws, "参数表ID", tid, 8, r)
        r = _write_info_row(ws, "中文名", longname, 8, r)
        r += 0

        fields = _get_children(conn, t["stable_id"], "FIELD")
        if fields:
            r = _write_header_row(ws, ["字段ID", "中文名", "类型", "长度", "多值", "必输", "默认值", "描述"], r)
            for f in fields:
                fp = f["properties"]
                r = _write_data_row(ws, [
                    fp.get("id", ""), fp.get("longname", ""), fp.get("type", ""),
                    fp.get("maxLength", ""), _bool_cn(fp.get("multi", "")),
                    _bool_cn(fp.get("required", "")), fp.get("defaultValue", ""),
                    fp.get("description", ""),
                ], r)
        r += 0

        # OdbIndex
        odbindexes = _get_children(conn, t["stable_id"], "ODBINDEX")
        if odbindexes:
            r = _write_header_row(ws, ["索引ID", "索引类型", "字段", "操作"], r)
            for idx in odbindexes:
                ip = idx["properties"]
                r = _write_data_row(ws, [ip.get("id", ""), ip.get("type", ""), ip.get("fields", ""), ip.get("operate", "")], r)

        report.params += 1
        _auto_width(ws)


def _export_error_codes(conn, wb, file_ids, report):
    errors = _resolve_nodes_by_file(conn, "DICTIONARY", file_ids)
    # 错误码通常也是 DICTIONARY 或特定 kind，按文件后缀过滤
    error_confs = []
    for e in errors:
        file_row = conn.execute("SELECT path FROM model_files WHERE id=?", (e["file_id"],)).fetchone()
        if file_row and file_row["path"].endswith(".error.xml"):
            error_confs.append(e)

    if not error_confs:
        return

    ws = wb.create_sheet(_safe_sheet_name("错误码总览"))
    r = _write_title_row(ws, "错误码总览", 3)
    r = _write_header_row(ws, ["类别", "错误码类型", "中文名称"], r)
    for ec in error_confs:
        props = ec["properties"]
        r = _write_data_row(ws, ["ERROR", props.get("id", ""), props.get("longname", "")], r)
    _auto_width(ws)

    used_names = set()
    for ec in error_confs:
        props = ec["properties"]
        eid = props.get("id", "")
        longname = props.get("longname", eid)
        sheet_name = _safe_sheet_name(f"{eid}_{longname}")
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        r = _write_title_row(ws, f"{eid} {longname}", 5, r)
        r = _write_info_row(ws, "错误类型名称", longname, 5, r)
        r = _write_info_row(ws, "错误类型ID", eid, 5, r)
        r = _write_info_row(ws, "描述", props.get("description", ""), 5, r)
        r += 0

        # 枚举值作为错误码明细
        enums = _get_children(conn, ec["stable_id"], "ENUM_VALUE")
        if enums:
            r = _write_header_row(ws, ["错误码ID", "错误码值", "中文名", "描述", ""], r)
            for ev in enums:
                ep = ev["properties"]
                r = _write_data_row(ws, [ep.get("id", ""), ep.get("value", ""), ep.get("longname", ""), ep.get("description", ""), ""], r)

        report.error_codes += 1
        _auto_width(ws)


def _export_batch_trans(conn, wb, file_ids, report):
    batches = _resolve_nodes_by_file(conn, "BATCH_TRANSACTION", file_ids)
    if not batches:
        return

    ws = wb.create_sheet(_safe_sheet_name("批量交易总览"))
    r = _write_title_row(ws, "批量交易总览", 2)
    r = _write_header_row(ws, ["批量名称", "批量ID"], r)
    for bt in batches:
        props = bt["properties"]
        r = _write_data_row(ws, [props.get("longname", ""), props.get("id", "")], r)
    _auto_width(ws)

    used_names = set()
    for bt in batches:
        props = bt["properties"]
        bid = props.get("id", "")
        longname = props.get("longname", bid)
        sheet_name = _safe_sheet_name(f"{bid}_{longname}")
        orig = sheet_name
        idx = 1
        while sheet_name in used_names:
            idx += 1
            sheet_name = _safe_sheet_name(f"{orig}_{idx}")
        used_names.add(sheet_name)

        ws = wb.create_sheet(sheet_name)
        r = 1
        r = _write_title_row(ws, f"{bid} {longname}", 7, r)
        r = _write_info_row(ws, "批量中文名", longname, 7, r)
        r = _write_info_row(ws, "批量ID", bid, 7, r)
        r = _write_info_row(ws, "业务类别", props.get("kind", ""), 7, r)
        r = _write_info_row(ws, "描述", props.get("description", ""), 7, r)
        r += 0

        # 输入接口
        fields = _get_children(conn, bt["stable_id"], "FIELD")
        if fields:
            r = _write_header_row(ws, ["字段码", "中文名", "类型", "长度", "列表值", "默认值", "固定值"], r)
            for f in fields:
                fp = f["properties"]
                r = _write_data_row(ws, [
                    fp.get("id", ""), fp.get("longname", ""), fp.get("type", ""),
                    fp.get("maxLength", ""), fp.get("fixedValue", ""),
                    fp.get("defaultValue", ""), fp.get("fixedValue", ""),
                ], r)

        report.batch_trans += 1
        _auto_width(ws)
