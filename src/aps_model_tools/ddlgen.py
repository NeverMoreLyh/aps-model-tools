"""统一 DDL 生成器：从 aps-model-tools SQLite 索引生成 MySQL/Oracle/PostgreSQL 建表脚本。

规则基准见 docs/ddl-generation-rules.md（逆向自 aps-model-util 的 DdlGenerator/TableDdlUtil
与 mysql.ftl/oracle.ftl/postgresql.ftl）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .store import find_nodes

DIALECTS = ("mysql", "oracle", "postgresql")

# ---------------------------------------------------------------------------
# 类型映射表（与 TableDdlUtil 静态映射一致）
# ---------------------------------------------------------------------------

# 基础类型 -> 各数据库类型名
TYPE_BASE = {
    "mysql": {
        "eString": "varchar", "encString": "varchar", "cString": "varchar",
        "dateString": "varchar", "string": "varchar", "schema": "varchar",
        "fixString": "char", "boolean": "char",
        "int": "int", "long": "bigint",
        "dateTime": "dateTime", "date": "date", "dateString8": "date",
        "time": "time",
        "double": "decimal", "decimal": "decimal", "amount": "decimal",
        "clob": "text", "blob": "blob", "timestamp": "timestamp",
    },
    "oracle": {
        "eString": "varchar2", "encString": "varchar2", "cString": "varchar2",
        "dateString": "varchar2", "string": "varchar2", "schema": "varchar2",
        "fixString": "char", "boolean": "char",
        "int": "number", "integer": "number", "long": "number",
        "double": "number", "decimal": "number", "amount": "number",
        "dateTime": "date", "date": "date", "dateString8": "date",
        "time": "date",
        "timeString17": "timestamp",
        "clob": "clob", "blob": "blob", "timestamp": "timestamp",
    },
    "postgresql": {
        "eString": "varchar", "encString": "varchar", "cString": "varchar",
        "dateString": "varchar", "string": "varchar", "schema": "varchar",
        "fixString": "char", "boolean": "boolean",
        "int": "integer", "long": "bigint",
        "dateTime": "timestamp", "date": "date", "dateString8": "date",
        "time": "time",
        "double": "decimal", "decimal": "decimal", "amount": "decimal",
        "clob": "text", "blob": "bytea", "timestamp": "timestamp",
    },
}

# 默认长度（TableDdlUtil 的 mysqlLength/oracleLength/postgresqlLength）
DEFAULT_FACETS = {
    "mysql": {"string": {"maxLength": 255}, "schema": {"maxLength": 255},
              "boolean": {"maxLength": 1}, "int": {"maxLength": 10},
              "long": {"maxLength": 16}, "amount": {"maxLength": 20, "fractionDigits": 2},
              "dateString": {"maxLength": 8}},
    "oracle": {"string": {"maxLength": 255}, "schema": {"maxLength": 255},
               "boolean": {"maxLength": 1}, "int": {"maxLength": 10},
               "long": {"maxLength": 16}, "amount": {"maxLength": 20, "fractionDigits": 2},
               "dateString": {"maxLength": 8}},
    "postgresql": {"string": {"maxLength": 255}, "schema": {"maxLength": 255},
                   "decimal": {"maxLength": 20, "fractionDigits": 2},
                   "double": {"maxLength": 20, "fractionDigits": 2},
                   "amount": {"maxLength": 20, "fractionDigits": 2},
                   "dateString": {"maxLength": 8}},
}

# 需要长度后缀的类型
LENGTH_TYPES = {
    "mysql": {"varchar", "char", "int", "bigint", "decimal"},
    "oracle": {"varchar2", "char", "number"},
    "postgresql": {"varchar", "char", "decimal"},
}

TEXT_THRESHOLD = 1000  # varchar(n) n>=1000 时 mysql/postgresql 转 text，oracle 转 clob

NUMERIC_PRIMITIVES = {"int", "integer", "long", "double", "decimal", "amount"}

FUNCTION_DEFAULTS = {
    "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "SYSDATE", "SYSTIMESTAMP",
    "NOW()", "LOCALTIMESTAMP", "NULL", "TRUE", "FALSE",
}


@dataclass
class DdlGenConfig:
    dialect: str = "mysql"
    text_threshold: int = TEXT_THRESHOLD
    db_ratio: float = 1.0          # byCharacter=true 时的长度系数（原生为 DBRATIO 首选项）
    auto_increment: bool = False   # 原生 auto_increment 选项：额外 id 列（MySQL）
    username: str = ""             # Oracle: public synonym 属主
    charset: str = "utf8mb4"       # MySQL 表选项
    table_space: str = ""          # Oracle tablespace
    index_space: str = ""          # Oracle index tablespace


@dataclass
class GenReport:
    dialect: str
    tables_generated: int = 0
    tables_skipped_abstract: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    sql: str = ""


# ---------------------------------------------------------------------------
# SQLite 取数
# ---------------------------------------------------------------------------

def _props(node: Dict[str, Any]) -> Dict[str, Any]:
    p = node.get("properties")
    if p is None:
        p = json.loads(node.get("properties_json", "{}"))
    return p


def _bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() == "true"


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _children(conn: sqlite3.Connection, node_id: int, kind: Optional[str] = None,
              xml_tag: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = ("select n.id, n.stable_id, n.kind, n.raw_id, n.full_id, n.owner_node_id, "
           "n.xml_tag, n.properties_json, f.path as file_path "
           "from nodes n join model_files f on f.id=n.file_id "
           "where n.owner_node_id=?")
    params: List[Any] = [node_id]
    if kind:
        sql += " and n.kind=?"
        params.append(kind)
    if xml_tag:
        sql += " and n.xml_tag=?"
        params.append(xml_tag)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _by_owner_kind(conn: sqlite3.Connection, table: Dict[str, Any], xml_tag: str) -> Dict[str, Any]:
    for child in _children(conn, table["id"], xml_tag=xml_tag):
        return child
    return {}


def _resolve_type_ref(conn: sqlite3.Connection, type_id: str, visited: Optional[Set[str]] = None
                      ) -> Tuple[Optional[str], Dict[str, Any], List[str]]:
    """解析类型链：返回 (primitive, merged_facets, errors)。merged_facets 子层优先。"""
    visited = visited or set()
    if not type_id:
        return None, {}, ["empty type reference"]
    if type_id in visited:
        return None, {}, [f"type cycle: {type_id}"]
    visited.add(type_id)
    # primitive 直接命中
    if type_id in TYPE_BASE["mysql"] or type_id in TYPE_BASE["oracle"] or type_id in TYPE_BASE["postgresql"]:
        return type_id, {}, []
    # 按 ref 查找 RESTRICTION_TYPE（优先 full_id 精确，其次 raw_id）
    nodes = [n for n in find_nodes(conn, type_id) if n["kind"] in {"RESTRICTION_TYPE", "SUBENUM"}]
    if not nodes:
        return None, {}, [f"unresolved type: {type_id}"]
    if len(nodes) > 1:
        # 精确 full_id 匹配优先
        exact = [n for n in nodes if n["full_id"] == type_id]
        if len(exact) == 1:
            nodes = exact
        else:
            return None, {}, [f"ambiguous type: {type_id} ({len(nodes)} candidates)"]
    node = nodes[0]
    if node["kind"] == "SUBENUM":
        # 子枚举无 base，回溯到所属 RESTRICTION_TYPE 解析
        owner = conn.execute("select full_id from nodes where id=?", (node.get("owner_node_id"),)).fetchone()
        if not owner or not owner[0]:
            return None, {}, [f"subenum has no owner: {type_id}"]
        return _resolve_type_ref(conn, owner[0], visited)
    local = dict(_props(node))
    base = local.pop("base", None)
    if not base:
        return None, {}, [f"type has no base: {type_id}"]
    primitive, inherited, errors = _resolve_type_ref(conn, base, visited)
    merged = dict(inherited)
    merged.update({k: v for k, v in local.items() if v != ""})
    merged["_chain"] = inherited.get("_chain", []) + [node["full_id"]]
    return primitive, merged, errors


def _field_length(facets: Dict[str, Any], primitive: str, dialect: str, cfg: DdlGenConfig) -> Optional[int]:
    """dbLength → maxLength → 默认长度表；byCharacter=true 时乘 db_ratio。"""
    length = _int(facets.get("dbLength")) or _int(facets.get("maxLength"))
    if not length:
        length = _int(DEFAULT_FACETS[dialect].get(primitive, {}).get("maxLength"))
    if length and _bool(facets.get("byCharacter")) and cfg.db_ratio != 1.0:
        length = int(length * cfg.db_ratio)
    return length


def _field_fraction(facets: Dict[str, Any], primitive: str, dialect: str) -> Optional[int]:
    fraction = _int(facets.get("dbFractionDigits")) or _int(facets.get("fractionDigits"))
    if fraction is None:
        fraction = DEFAULT_FACETS[dialect].get(primitive, {}).get("fractionDigits")
    return fraction


# ---------------------------------------------------------------------------
# SQL 类型渲染
# ---------------------------------------------------------------------------

def render_sql_type(primitive: str, facets: Dict[str, Any], cfg: DdlGenConfig) -> str:
    dialect = cfg.dialect
    base = TYPE_BASE[dialect].get(primitive)
    if base is None:
        raise ValueError(f"no {dialect} mapping for primitive type: {primitive}")
    if base not in LENGTH_TYPES[dialect]:
        return base
    if base == "decimal":
        precision = _field_length(facets, primitive, dialect, cfg) or 20
        scale = _field_fraction(facets, primitive, dialect)
        scale = scale if scale is not None else 0
        return f"decimal({precision},{scale})"
    length = _field_length(facets, primitive, dialect, cfg)
    if length is None:
        return base
    sql_type = f"{base}({length})"
    # 二次特殊转换
    if dialect == "mysql" and base == "varchar" and length >= cfg.text_threshold:
        return "text"
    if dialect == "oracle" and base == "varchar2" and length > 4000:
        return "clob"
    if dialect == "postgresql" and base == "varchar" and length >= cfg.text_threshold:
        return "text"
    return sql_type


def _add_remain(facets: Dict[str, Any], sql_type: str) -> str:
    if _bool(facets.get("isUnsigned")):
        sql_type += " unsigned"
    if _bool(facets.get("isZerofill")):
        sql_type += " zerofill"
    return sql_type


def _format_default(raw: str, primitive: str, conn: sqlite3.Connection, field_ref: Dict[str, Any]) -> str:
    value = raw.strip()
    if value.startswith("#"):
        # 枚举值引用：#枚举值id -> 取 value 属性
        enum_id = value[1:]
        nodes = [n for n in find_nodes(conn, enum_id) if n["kind"] == "ENUM_VALUE"]
        if len(nodes) == 1:
            value = str(_props(nodes[0]).get("value", enum_id))
        else:
            value = enum_id
    if primitive in NUMERIC_PRIMITIVES:
        if re.fullmatch(r"[+-]?\d+(\.\d+)?", value):
            return value
        return f"'{value}'"
    upper = value.upper()
    if upper in FUNCTION_DEFAULTS:
        return upper
    return "'" + value.replace("'", "''") + "'"


def _comment_text(longname: str, enum_values: List[Dict[str, Any]], max_enums: int = 5) -> str:
    text = longname or ""
    if enum_values:
        shown = enum_values[:max_enums]
        parts = [f"{_props(e).get('value', e.get('raw_id', ''))}-{_props(e).get('longname', '')}" for e in shown]
        suffix = ",..." if len(enum_values) > max_enums else ""
        text += "(" + ",".join(parts) + suffix + ")"
    return text.replace("'", "''")


# ---------------------------------------------------------------------------
# 表结构提取
# ---------------------------------------------------------------------------

@dataclass
class Column:
    logical_id: str
    field_id: str
    sql_type: str
    nullable: bool
    default: Optional[str]
    primarykey: bool
    identity: bool
    comment: str
    file_path: str


@dataclass
class TableDef:
    node: Dict[str, Any]
    name: str
    longname: str
    virtual: bool
    sharding: int
    columns: List[Column]
    indexes: List[Dict[str, Any]]
    sequences: List[Dict[str, Any]]
    pk_columns: List[str]
    ddl_frags: List[str]


def _expanded_table_fields(conn: sqlite3.Connection, table: Dict[str, Any],
                           visited: Optional[Set[str]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """展开 extension 继承：父表字段在前，同名被本表覆盖。"""
    visited = visited or set()
    if table["stable_id"] in visited:
        return [], [f"table inheritance cycle: {table['full_id']}"]
    visited.add(table["stable_id"])
    fields: List[Dict[str, Any]] = []
    errors: List[str] = []
    extension = _props(table).get("extension")
    if extension:
        # APS extension supports multiple space-separated parent types
        for ext_ref in str(extension).split():
            parents = [n for n in find_nodes(conn, ext_ref) if n["kind"] in {"TABLE", "COMPLEX_TYPE"}]
            if len(parents) != 1:
                errors.append(f"{table['full_id']}: unresolved extension {ext_ref}")
                continue
            if parents[0]["kind"] == "TABLE":
                pf, pe = _expanded_table_fields(conn, parents[0], visited)
                fields.extend(pf)
                errors.extend(pe)
            else:
                fields.extend(_children(conn, parents[0]["id"], kind="ELEMENT"))
    fields_container = _by_owner_kind(conn, table, "fields")
    if fields_container:
        fields.extend(_children(conn, fields_container["id"], kind="FIELD"))
    merged: Dict[str, Dict[str, Any]] = {}
    for f in fields:
        merged[f["raw_id"]] = f
    return list(merged.values()), errors


def _enum_values_for(conn: sqlite3.Connection, facets: Dict[str, Any]) -> List[Dict[str, Any]]:
    chain = facets.get("_chain", [])
    for type_full_id in reversed(chain):
        nodes = [n for n in find_nodes(conn, type_full_id) if n["kind"] == "RESTRICTION_TYPE"]
        if len(nodes) != 1:
            continue
        enums = _children(conn, nodes[0]["id"], kind="ENUM_VALUE")
        if enums:
            return enums
    return []


def _extract_table(conn: sqlite3.Connection, table: Dict[str, Any], cfg: DdlGenConfig,
                   report: GenReport) -> Optional[TableDef]:
    props = _props(table)
    if _bool(props.get("abstract")):
        report.tables_skipped_abstract += 1
        return None
    table_name = props.get("name") or table["raw_id"]
    longname = props.get("longname", "")
    virtual = _bool(props.get("virtual"))
    sharding = _int(props.get("sharding")) or 0

    fields, errors = _expanded_table_fields(conn, table)
    for e in errors:
        report.errors.append(e)

    columns: List[Column] = []
    for f in fields:
        fp = _props(f)
        type_id = fp.get("type")
        if not type_id:
            report.errors.append(f"{f['full_id']}: field has no type")
            continue
        primitive, facets, terr = _resolve_type_ref(conn, type_id)
        for e in terr:
            report.errors.append(f"{f['full_id']}: {e}")
        if not primitive:
            continue
        try:
            sql_type = render_sql_type(primitive, facets, cfg)
        except ValueError as exc:
            report.errors.append(f"{f['full_id']}: {exc}")
            continue
        sql_type = _add_remain(facets, sql_type)
        logical_id = fp.get("dbname") or f["raw_id"]
        default_raw = fp.get("default")
        default = None
        if default_raw not in (None, ""):
            default = _format_default(str(default_raw), primitive, conn, f)
        enums = _enum_values_for(conn, facets)
        comment = _comment_text(fp.get("longname") or fp.get("desc") or "", enums)
        columns.append(Column(
            logical_id=logical_id,
            field_id=f["raw_id"],
            sql_type=sql_type,
            nullable=_bool(fp.get("nullable"), True),
            default=default,
            primarykey=_bool(fp.get("primarykey")),
            identity=_bool(fp.get("identity")),
            comment=comment,
            file_path=f.get("file_path", ""),
        ))

    # 物理索引：<indexes> 容器（<odbindexes> 不产生物理索引）
    indexes: List[Dict[str, Any]] = []
    idx_container = _by_owner_kind(conn, table, "indexes")
    if idx_container:
        for idx in _children(conn, idx_container["id"], kind="INDEX"):
            ip = _props(idx)
            raw_fields = str(ip.get("fields", "")).replace(",", " ").split()
            indexes.append({"node": idx, "props": ip, "fields": raw_fields})

    # 主键：field 级 primarykey；无则取第一个 primarykey/unique 索引字段兜底（原生 getKeys 逻辑）
    pk_columns = [c.logical_id for c in columns if c.primarykey]
    partition = props.get("partition")
    if not pk_columns:
        for idx in indexes:
            itype = str(idx["props"].get("type", "")).lower()
            if itype in ("primarykey", "unique") and idx["fields"]:
                pk_columns = [_logical_id_of(columns, x) for x in idx["fields"]]
                pk_columns = [x for x in pk_columns if x]
                break
    if partition:
        pk_columns.append(str(partition))

    sequences: List[Dict[str, Any]] = []
    for child in _children(conn, table["id"], kind="SEQUENCE"):
        if child.get("xml_tag") == "dbSequence":
            sequences.append(child)

    ddl_frags = []
    frags_container = _by_owner_kind(conn, table, "ddls")
    if frags_container:
        for frag in _children(conn, frags_container["id"]):
            fp = _props(frag)
            if fp.get("dbType", cfg.dialect) == cfg.dialect or not fp.get("dbType"):
                ddl_frags.append(str(fp.get("sql", "")))

    return TableDef(node=table, name=table_name, longname=longname, virtual=virtual,
                    sharding=sharding, columns=columns, indexes=indexes,
                    sequences=sequences, pk_columns=pk_columns, ddl_frags=ddl_frags)


def _logical_id_of(columns: List[Column], field_id: str) -> str:
    for c in columns:
        if c.field_id == field_id:
            return c.logical_id
    return field_id


# ---------------------------------------------------------------------------
# 各数据库 DDL 渲染
# ---------------------------------------------------------------------------

def _ident(name: str, dialect: str) -> str:
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return name


def _column_line(c: Column, cfg: DdlGenConfig) -> str:
    dialect = cfg.dialect
    parts = [_ident(c.logical_id, dialect), c.sql_type]
    if c.identity:
        if dialect == "mysql":
            parts.append("AUTO_INCREMENT")
        else:
            parts.append("GENERATED BY DEFAULT AS IDENTITY")
    if c.default is not None:
        parts.append(f"DEFAULT {c.default}")
    if not c.nullable:
        parts.append("NOT NULL")
    if dialect == "mysql" and c.comment:
        # MySQL 原生模板将注释内联到列定义中
        parts.append(f"COMMENT '{c.comment.replace(chr(39), chr(39)*2)}'")
    return "  " + " ".join(parts)


def _emit_table(td: TableDef, cfg: DdlGenConfig, report: GenReport) -> List[str]:
    dialect = cfg.dialect
    names = [td.name]
    if td.sharding > 1:
        names = [f"{td.name}_{i}" for i in range(td.sharding)]
    out: List[str] = []
    pk_used_in_first_index: Set[str] = set()

    for tname in names:
        lines: List[str] = []
        header = f"create table {_ident(tname, dialect)} ("
        if dialect == "oracle" and td.virtual:
            header = f"create global temporary table {_ident(tname, dialect)} ("
        if cfg.auto_increment and dialect == "mysql":
            lines.append(f"  id bigint(20) PRIMARY KEY AUTO_INCREMENT NOT NULL COMMENT '无业务含义主键',")
        col_lines = [_column_line(c, cfg) for c in td.columns]
        body = ",\n".join(col_lines)
        lines_sql = header + "\n" + body + "\n)"
        if dialect == "mysql":
            lines_sql += f" ENGINE=InnoDB DEFAULT CHARSET={cfg.charset} ROW_FORMAT=DYNAMIC collate={cfg.charset}_unicode_ci"
            if td.longname:
                lines_sql += f" COMMENT='{td.longname.replace(chr(39), chr(39)*2)}'"
        elif dialect == "oracle":
            if cfg.table_space:
                lines_sql += f" tablespace {cfg.table_space}"
            if td.virtual:
                lines_sql += " on commit delete rows"
        out.append(lines_sql + ";")

        # 主键（ALTER 追加，与原生模板一致；Oracle keyProcess 内联模式此处统一走 ALTER）
        if td.pk_columns:
            pk_cols = ", ".join(_ident(x, dialect) for x in td.pk_columns)
            if dialect == "oracle":
                out.append(f"alter table {_ident(tname, dialect)} add constraint pk_{tname} primary key ({pk_cols}) using index;")
            else:
                out.append(f"alter table {_ident(tname, dialect)} add constraint pk_{tname} primary key ({pk_cols});")

        # 索引：已作为主键第一个索引的不再重复生成普通索引（原生 getTableIndex 过滤）
        pk_set = set(td.pk_columns)
        first_pk_index_consumed = False
        for idx in td.indexes:
            itype = str(idx["props"].get("type", "")).lower()
            iname = idx["node"]["raw_id"]
            cols = [_logical_id_of(td.columns, x) for x in idx["fields"]]
            missing = [x for x in cols if x not in {c.logical_id for c in td.columns}]
            if missing:
                report.errors.append(f"{td.name}: index {iname} references missing fields: {', '.join(missing)}")
                continue
            if itype == "primarykey":
                if not first_pk_index_consumed and not any(c.primarykey for c in td.columns):
                    first_pk_index_consumed = True
                continue  # primarykey 类型索引仅用于兜底主键，不单独建索引
            if not first_pk_index_consumed and set(cols) == pk_set and itype in ("unique", "index"):
                first_pk_index_consumed = True
                continue
            unique = "unique " if itype == "unique" else ""
            col_sql = ", ".join(_ident(x, dialect) for x in cols)
            suffix = ""
            if dialect == "oracle":
                if cfg.index_space:
                    suffix += f" tablespace {cfg.index_space}"
                if str(idx["props"].get("reverse", "")).lower() == "true":
                    suffix += " reverse"
                if str(idx["props"].get("local", "")).lower() == "true":
                    suffix += " local"
            out.append(f"create {unique}index {iname} on {_ident(tname, dialect)} ({col_sql}){suffix};")

        # 注释
        if td.longname or any(c.comment for c in td.columns):
            if dialect == "mysql":
                pass  # MySQL 注释已内联在列定义与表选项中（列级 comment 此处简化：追加 ALTER）
            else:
                if td.longname:
                    out.append(f"comment on table {_ident(tname, dialect)} is '{td.longname.replace(chr(39), chr(39)*2)}';")
                for c in td.columns:
                    if c.comment:
                        out.append(f"comment on column {_ident(tname, dialect)}.{_ident(c.logical_id, dialect)} is '{c.comment}';")
        if dialect == "oracle" and cfg.username:
            out.append(f"create or replace public synonym {td.name} for {cfg.username}.{td.name};")

        # 自定义 DDL 片段
        for frag in td.ddl_frags:
            if frag.strip():
                report.warnings.append(f"{td.name}: custom <ddls> fragment emitted verbatim")
                out.append(frag.strip().rstrip(";") + ";")

    # 序列
    for seq in td.sequences:
        out.extend(_emit_sequence(seq, cfg, report))
    return out


def _emit_sequence(seq: Dict[str, Any], cfg: DdlGenConfig, report: GenReport) -> List[str]:
    sp = _props(seq)
    name = sp.get("id") or seq["raw_id"]
    start = sp.get("startWith", "1")
    inc = sp.get("incrementBy", "1")
    minv = sp.get("minValue")
    maxv = sp.get("maxValue")
    cache = sp.get("cache")
    cycle = _bool(sp.get("cycle"))
    dialect = cfg.dialect
    if dialect == "mysql":
        # APS 约定：序列表登记
        return [
            f"delete from ksys_liusdy where liusdyid='{name}';",
            f"insert into ksys_liusdy(liusdyid, startnum, step, minnum, maxnum, cachennum, cyclenum) "
            f"values('{name}', {start}, {inc}, {minv or 1}, {maxv or 9999999999999999}, {cache or 20}, {1 if cycle else 0});",
        ]
    if dialect == "oracle":
        parts = [f"create sequence {name} start with {start} increment by {inc}"]
        if minv:
            parts.append(f"minvalue {minv}")
        if maxv:
            parts.append(f"maxvalue {maxv}")
        if cache:
            parts.append(f"cache {cache}")
        parts.append("cycle" if cycle else "nocycle")
        parts.append("order")
        return [" ".join(parts) + ";"]
    # postgresql
    parts = [f"create sequence {name} start {start} increment {inc}"]
    if minv:
        parts.append(f"minvalue {minv}")
    if maxv:
        parts.append(f"maxvalue {maxv}")
    if cache:
        parts.append(f"cache {cache}")
    parts.append("cycle" if cycle else "no cycle")
    return [" ".join(parts) + ";"]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def generate_all_ddl(conn: sqlite3.Connection, cfg: DdlGenConfig,
                      tables_filter: Optional[List[str]] = None) -> GenReport:
    if cfg.dialect not in DIALECTS:
        raise ValueError(f"unsupported dialect: {cfg.dialect}; expected one of {DIALECTS}")
    report = GenReport(dialect=cfg.dialect)

    if tables_filter:
        tables: List[Dict[str, Any]] = []
        for q in tables_filter:
            exact = [dict(r) for r in conn.execute(
                "select n.id, n.stable_id, n.kind, n.raw_id, n.full_id, n.owner_node_id, n.xml_tag, "
                "n.properties_json, f.path as file_path "
                "from nodes n join model_files f on f.id=n.file_id "
                "where n.kind='TABLE' and n.full_id=?", (q,)).fetchall()]
            if exact:
                tables.extend(exact)
                continue
            found = [n for n in find_nodes(conn, q) if n["kind"] == "TABLE"]
            if len(found) != 1:
                report.errors.append(f"table query '{q}': expected exactly 1 TABLE node, got {len(found)}")
                continue
            tables.append(found[0])
    else:
        tables = [dict(r) for r in conn.execute(
            "select n.id, n.stable_id, n.kind, n.raw_id, n.full_id, n.owner_node_id, n.xml_tag, "
            "n.properties_json, f.path as file_path "
            "from nodes n join model_files f on f.id=n.file_id "
            "where n.kind='TABLE' order by n.full_id").fetchall()]

    blocks: List[str] = []
    seen_table_names: Dict[str, str] = {}
    for table in tables:
        td = _extract_table(conn, table, cfg, report)
        if td is None:
            continue
        if not td.columns:
            report.warnings.append(f"{td.name}: no columns after expansion, skipped")
            continue
        dup_key = td.name.lower()
        if dup_key in seen_table_names:
            report.warnings.append(
                f"{td.name}: duplicate table definition (also in {seen_table_names[dup_key]}), "
                f"second definition skipped")
            continue
        seen_table_names[dup_key] = table.get("file_path", td.node.get("full_id", "?"))
        try:
            blocks.extend(_emit_table(td, cfg, report))
            report.tables_generated += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{td.name}: generation failed: {exc}")

    report.sql = "\n\n".join(blocks) + ("\n" if blocks else "")
    return report
