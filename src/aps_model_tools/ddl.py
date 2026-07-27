from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .store import find_nodes


PRIMITIVES = {
    "string": lambda p: f"VARCHAR({int(p.get('maxLength') or p.get('dbLength') or 255)})",
    "encString": lambda p: f"VARCHAR({int(p.get('maxLength') or p.get('dbLength') or 255)})",
    "fixString": lambda p: f"CHAR({int(p.get('maxLength') or p.get('dbLength') or 1)})",
    "cString": lambda p: f"VARCHAR({int(p.get('maxLength') or p.get('dbLength') or 255)})",
    "eString": lambda p: f"VARCHAR({int(p.get('maxLength') or p.get('dbLength') or 255)})",
    "dateString": lambda p: f"VARCHAR({int(p.get('maxLength') or p.get('dbLength') or 32)})",
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


def _decimal(props: Dict[str, Any]) -> str:
    precision = int(props.get("dbLength") or props.get("maxLength") or 38)
    scale = int(props.get("dbFractionDigits") or props.get("fractionDigits") or 0)
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
    params: List[Any] = [owner_id]
    sql = "select * from nodes where owner_id=?"
    if kind:
        sql += " and kind=?"
        params.append(kind)
    sql += " order by full_id"
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
        parents = [n for n in find_nodes(conn, extension) if n["kind"] in {"TABLE", "COMPLEX_TYPE"}]
        if len(parents) != 1:
            errors.append(f"unresolved or ambiguous table extension: {extension}")
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
        """select n.* from nodes n
           join nodes container on container.stable_id=n.owner_id
           where n.kind='INDEX'
             and container.xml_tag='indexes'
             and (container.owner_id=? or container.owner_id in
                  (select stable_id from nodes where owner_id=?))
           order by n.full_id,n.stable_id""",
        (table["stable_id"], table["stable_id"]),
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
    nodes = [n for n in find_nodes(conn, type_id) if n["kind"] == "RESTRICTION_TYPE"]
    if len(nodes) != 1:
        return None, {}, [], [f"unresolved or ambiguous type: {type_id}"]
    node = nodes[0]
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
    return PRIMITIVES[primitive](facets), evidence, []


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
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        return raw
    if sql_type.startswith(("TINYINT", "SMALLINT", "INT", "BIGINT", "DECIMAL", "DOUBLE", "FLOAT")):
        try:
            float(raw)
            return raw
        except ValueError:
            pass
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
            part += " DEFAULT " + _format_default(default_value, sql_type)
        if fp.get("identity", "false").lower() == "true":
            part += " AUTO_INCREMENT"
        columns.append(part)
        if fp.get("primarykey", "false").lower() == "true":
            primary.append(field["raw_id"])
    if errors:
        return DdlResult("", sorted(set(warnings)), sorted(set(errors)), sorted(set(evidence)))
    if primary:
        columns.append("  PRIMARY KEY (" + ", ".join(_quote(x, dialect) for x in primary) + ")")
    lines = ["-- APS model DDL preview only; not executed.", f"-- source: {table['file_path']}",
             f"CREATE TABLE {_quote(table_name, dialect)} (", ",\n".join(columns), ");"]
    for index in indexes:
        ip = _props(index)
        raw_fields = ip.get("fields", "")
        index_fields = [x for x in raw_fields.replace(",", " ").split() if x]
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
