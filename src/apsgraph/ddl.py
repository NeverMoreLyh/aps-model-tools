from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .store import NODE_SELECT, find_nodes


def _positive_length(p: Dict[str, Any], default: int, label: str, maximum: int) -> int:
    length = int(p.get("maxLength") or p.get("dbLength") or default)
    if length < 1 or length > maximum:
        raise ValueError(f"invalid {label} length: {length}; expected 1..{maximum}")
    return length


def _varchar(p: Dict[str, Any], default: int = 255) -> str:
    return f"VARCHAR({_positive_length(p, default, 'VARCHAR', 65535)})"


def _char(p: Dict[str, Any], default: int = 1) -> str:
    return f"CHAR({_positive_length(p, default, 'CHAR', 255)})"


PRIMITIVES = {
    "string": lambda p: _varchar(p),
    "encString": lambda p: _varchar(p),
    "fixString": lambda p: _char(p),
    "cString": lambda p: _varchar(p),
    "eString": lambda p: _varchar(p),
    "dateString": lambda p: _varchar(p, 32),
    "byte": lambda p: "TINYINT",
    "boolean": lambda p: "TINYINT(1)",
    "int": lambda p: "INT",
    "integer": lambda p: "INT",
    "long": lambda p: "BIGINT",
    "decimal": lambda p: _decimal(p),
    "amount": lambda p: _decimal(p),
    "double": lambda p: "DOUBLE",
    "date": lambda p: "DATE",
    "time": lambda p: "TIME",
    "dateTime": lambda p: "DATETIME",
    "timestamp": lambda p: "TIMESTAMP",
    "blob": lambda p: "LONGBLOB",
    "clob": lambda p: "LONGTEXT",
}


@dataclass(frozen=True)
class DdlResult:
    sql: str
    warnings: List[str]
    errors: List[str]
    evidence: List[str]


def _decimal(p: Dict[str, Any]) -> str:
    precision = int(p.get("dbLength") or p.get("maxLength") or 38)
    scale = int(p.get("dbFractionDigits") or p.get("fractionDigits") or 0)
    if precision < 1 or precision > 65 or scale < 0 or scale > 30 or scale > precision:
        raise ValueError(f"invalid MySQL DECIMAL facets: precision={precision}, scale={scale}")
    return f"DECIMAL({precision},{scale})"


def _props(node: Dict[str, Any]) -> Dict[str, Any]:
    return node.get("properties") or json.loads(node.get("properties_json", "{}"))


def _resolve_one(conn: sqlite3.Connection, query: str, expected_kind: Optional[str] = None) -> Dict[str, Any]:
    nodes = find_nodes(conn, query)
    if expected_kind:
        nodes = [n for n in nodes if n["kind"] == expected_kind]
    if not nodes:
        raise ValueError(f"model not found: {query}")
    if len(nodes) != 1:
        raise ValueError(f"ambiguous model: {query}")
    return nodes[0]


def _children(conn: sqlite3.Connection, owner_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    owner = conn.execute("select id from nodes where stable_id=?", (owner_id,)).fetchone()
    if not owner:
        return []
    params: List[Any] = [owner[0]]
    sql = NODE_SELECT + " where n.owner_node_id=?"
    if kind:
        sql += " and n.kind=?"
        params.append(kind)
    sql += " order by n.full_id"
    result = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["properties"] = json.loads(item.pop("properties_json"))
        result.append(item)
    return result


def _descendants(conn: sqlite3.Connection, owner_id: str, kind: str) -> List[Dict[str, Any]]:
    result = []
    queue = [owner_id]
    while queue:
        parent = queue.pop(0)
        for child in _children(conn, parent):
            queue.append(child["stable_id"])
            if child["kind"] == kind:
                result.append(child)
    return result


def _expanded_fields(conn: sqlite3.Connection, table: Dict[str, Any], visited: Optional[set] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    visited = visited or set()
    if table["stable_id"] in visited:
        return [], [f"table inheritance cycle: {table['full_id']}"]
    visited.add(table["stable_id"])
    fields: List[Dict[str, Any]] = []
    errors: List[str] = []
    extension = _props(table).get("extension")
    if extension:
        for extension_ref in str(extension).split():
            parents = [n for n in find_nodes(conn, extension_ref) if n["kind"] in {"TABLE", "COMPLEX_TYPE"}]
            if len(parents) != 1:
                errors.append(f"unresolved or ambiguous table extension: {extension_ref}")
            elif parents[0]["kind"] == "TABLE":
                parent_fields, parent_errors = _expanded_fields(conn, parents[0], visited)
                fields.extend(parent_fields)
                errors.extend(parent_errors)
            else:
                fields.extend(_descendants(conn, parents[0]["stable_id"], "ELEMENT"))
    local = _descendants(conn, table["stable_id"], "FIELD")
    by_name = {field["raw_id"]: field for field in fields}
    for field in local:
        by_name[field["raw_id"]] = field
    return list(by_name.values()), errors


def _table_indexes(conn: sqlite3.Connection, table: Dict[str, Any]) -> List[Dict[str, Any]]:
    # `<odbindexes>` drives generated DAO operations, not physical initialization DDL.
    rows = conn.execute(
        NODE_SELECT + """ join nodes container on container.id=n.owner_node_id
           where n.kind='INDEX'
             and container.xml_tag='indexes'
             and (container.owner_node_id=? or container.owner_node_id in
                  (select id from nodes where owner_node_id=?))
           order by n.full_id,n.stable_id""",
        (table["id"], table["id"]),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["properties"] = json.loads(item.pop("properties_json"))
        result.append(item)
    return result


def _resolve_type_chain(conn: sqlite3.Connection, type_id: str, visited: Optional[set] = None) -> Tuple[Optional[str], Dict[str, Any], List[str], List[str]]:
    visited = visited or set()
    if type_id in visited:
        return None, {}, [], [f"type cycle: {type_id}"]
    visited.add(type_id)
    if type_id in PRIMITIVES:
        return type_id, {}, [f"primitive:{type_id}"], []
    nodes = [n for n in find_nodes(conn, type_id) if n["kind"] in {"RESTRICTION_TYPE", "SUBENUM"}]
    if len(nodes) != 1:
        return None, {}, [], [f"unresolved or ambiguous type: {type_id}"]
    node = nodes[0]
    if node["kind"] == "SUBENUM":
        # 枚举子集（<subenum>，如 ApBaseEnumType.E_MATU_UNIT.E_MATU_UNIT_CZZQ）
        # 无自身 base：回溯所属枚举类型（RESTRICTION_TYPE）按其基础类型解析
        owner = conn.execute("select full_id from nodes where id=?", (node["owner_node_id"],)).fetchone()
        if not owner or not owner[0]:
            return None, {}, [node["file_path"]], [f"subenum has no owner: {type_id}"]
        primitive, inherited, evidence, errors = _resolve_type_chain(conn, owner[0], visited)
        return primitive, inherited, [node["file_path"]] + evidence, errors
    local = _props(node)
    base = local.get("base")
    if not base:
        return None, {}, [node["file_path"]], [f"type has no base: {type_id}"]
    primitive, inherited, evidence, errors = _resolve_type_chain(conn, base, visited)
    merged = dict(inherited)
    merged.update({key: value for key, value in local.items() if key != "base" and value != ""})
    return primitive, merged, [node["file_path"]] + evidence, errors


def _resolve_type(conn: sqlite3.Connection, type_id: str, visited: Optional[set] = None) -> Tuple[Optional[str], List[str], List[str]]:
    primitive, facets, evidence, errors = _resolve_type_chain(conn, type_id, visited)
    if errors or not primitive or primitive not in PRIMITIVES:
        return None, evidence, errors or [f"unsupported primitive type: {primitive}"]
    try:
        return PRIMITIVES[primitive](facets), evidence, []
    except (TypeError, ValueError) as exc:
        return None, evidence, [f"invalid type facets for {type_id}: {exc}"]


def _primitive_base(conn: sqlite3.Connection, type_id: str, visited: set) -> Optional[str]:
    if type_id in PRIMITIVES:
        return type_id
    if type_id in visited:
        return None
    visited.add(type_id)
    nodes = [n for n in find_nodes(conn, type_id) if n["kind"] == "RESTRICTION_TYPE"]
    if len(nodes) != 1:
        return None
    return _primitive_base(conn, _props(nodes[0]).get("base", ""), visited)


def _quote(identifier: str, dialect: str) -> str:
    if dialect != "mysql":
        raise ValueError(f"unsupported dialect: {dialect}")
    return "`" + identifier.replace("`", "``") + "`"


def _format_default(value: Any, sql_type: str) -> str:
    raw = str(value).strip()
    if not raw:
        return "''"
    upper = raw.upper()
    if upper in {"NULL", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"}:
        return upper
    if sql_type.startswith(("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT", "DECIMAL", "DOUBLE", "FLOAT")):
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw):
            raise ValueError(f"invalid numeric default {raw!r} for {sql_type}")
        return raw
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        raw = raw[1:-1]
    return "'" + raw.replace("'", "''") + "'"


def generate_table_ddl(conn: sqlite3.Connection, query: str, dialect: str = "mysql") -> DdlResult:
    try:
        table = _resolve_one(conn, query, "TABLE")
    except ValueError as exc:
        return DdlResult("", [], [str(exc)], [])
    if dialect != "mysql":
        return DdlResult("", [], [f"unsupported dialect: {dialect}"], [table["file_path"]])
    props = _props(table)
    table_name = props.get("name") or table["raw_id"]
    fields, inheritance_errors = _expanded_fields(conn, table)
    if not fields:
        inheritance_errors.append(f"table has no fields after inheritance expansion: {table['full_id']}")
    indexes = _table_indexes(conn, table)
    columns: List[str] = []
    primary: List[str] = []
    warnings: List[str] = []
    errors: List[str] = list(inheritance_errors)
    evidence = [table["file_path"]]
    field_names = {field["raw_id"] for field in fields}
    identity_fields: List[str] = []
    for field in fields:
        fp = _props(field)
        type_id = fp.get("type")
        if not type_id:
            errors.append(f"field has no type: {field['full_id']}")
            continue
        sql_type, type_evidence, type_errors = _resolve_type(conn, type_id)
        evidence.extend(type_evidence)
        errors.extend(type_errors)
        if not sql_type:
            continue
        part = f"  {_quote(field['raw_id'], dialect)} {sql_type}"
        if fp.get("nullable", "true").lower() == "false":
            part += " NOT NULL"
        default_present = "default" in fp
        default_value = fp.get("default")
        if not default_present and fp.get("ref"):
            refs = find_nodes(conn, fp["ref"])
            refs = [node for node in refs if node["kind"] == "ELEMENT"]
            if len(refs) == 1:
                dict_props = _props(refs[0])
                if "default" in dict_props:
                    default_present = True
                    default_value = dict_props["default"]
        if default_present and default_value != "":
            try:
                part += " DEFAULT " + _format_default(default_value, sql_type)
            except ValueError as exc:
                errors.append(f"invalid default for {field['full_id']}: {exc}")
        if fp.get("identity", "false").lower() == "true":
            if not sql_type.startswith(("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT")):
                errors.append(f"AUTO_INCREMENT requires integer type: {field['full_id']} is {sql_type}")
            part += " AUTO_INCREMENT"
            identity_fields.append(field["raw_id"])
        columns.append(part)
        if fp.get("primarykey", "false").lower() == "true":
            primary.append(field["raw_id"])
    if len(identity_fields) > 1:
        errors.append("MySQL allows at most one AUTO_INCREMENT column")
    for identity in identity_fields:
        # Physical indexes are emitted after CREATE TABLE. They cannot make an
        # AUTO_INCREMENT declaration valid at table-creation time.
        if identity not in primary:
            errors.append(f"AUTO_INCREMENT column must be a primary key in preview DDL: {identity}")
    if errors:
        return DdlResult("", sorted(set(warnings)), sorted(set(errors)), sorted(set(evidence)))
    if primary:
        columns.append("  PRIMARY KEY (" + ", ".join(_quote(x, dialect) for x in primary) + ")")
    lines = ["-- APS model DDL preview only; not executed.", f"-- source: {table['file_path']}",
             f"CREATE TABLE {_quote(table_name, dialect)} (", ",\n".join(columns), ");"]
    index_names: set = set()
    for index in indexes:
        if index["raw_id"] in index_names:
            errors.append(f"duplicate physical index name: {index['raw_id']}")
            continue
        index_names.add(index["raw_id"])
        ip = _props(index)
        raw_fields = ip.get("fields", "")
        index_fields = [x for x in raw_fields.replace(",", " ").split() if x]
        if not index_fields:
            errors.append(f"index {index['raw_id']} has no fields")
            continue
        missing = [x for x in index_fields if x not in field_names]
        if missing:
            errors.append(f"index {index['raw_id']} references missing fields: {', '.join(missing)}")
            continue
        unique = "UNIQUE " if ip.get("type", "").lower() == "unique" else ""
        lines.append(
            f"CREATE {unique}INDEX {_quote(index['raw_id'], dialect)} ON {_quote(table_name, dialect)} "
            f"({', '.join(_quote(x, dialect) for x in index_fields)});"
        )
    if errors:
        return DdlResult("", sorted(set(warnings)), sorted(set(errors)), sorted(set(evidence)))
    return DdlResult("\n".join(lines), sorted(set(warnings)), [], sorted(set(evidence)))
