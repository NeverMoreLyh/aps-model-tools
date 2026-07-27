from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

from .store import connect, get_stats


SUFFIXES = (
    ".flowtrans.xml", ".nsql.xml", ".batchStep.xml", ".batchgroup.xml",
    ".batch_tran.xml", ".file_batch_tran.xml", ".tables.xml", ".u_schema.xml",
    ".e_schema.xml", ".d_schema.xml", ".c_schema.xml", ".parms.xml",
    ".serviceType.xml", ".serviceImpl.xml", ".apsServiceType.xml",
    ".apsServiceImpl.xml", ".dmsServiceType.xml", ".dmsServiceImpl.xml",
    ".error.xml", ".constant.xml", ".plugin.xml", ".plugin2.xml",
    ".componentSchema.xml", ".report.xml", ".sharding.xml", ".webtran.xml",
    ".workflow.xml", ".transtest.xml",
)
EXCLUDED_DIRS = {".git", "target", ".idea", ".codegraph", ".hermes", "node_modules"}


TAG_KIND = {
    "schema": "SCHEMA", "restrictionType": "RESTRICTION_TYPE", "enumeration": "ENUM_VALUE",
    "complexType": "DICTIONARY" , "element": "ELEMENT", "table": "TABLE", "field": "FIELD",
    "sqls": "SQL_GROUP", "select": "NAMED_SQL", "dynamicSelect": "NAMED_SQL",
    "insert": "NAMED_SQL", "update": "NAMED_SQL", "delete": "NAMED_SQL",
    "procedure": "NAMED_SQL", "ddl": "NAMED_SQL", "serviceType": "SERVICE_TYPE",
    "serviceImpl": "SERVICE_IMPLEMENTATION", "service": "SERVICE_OPERATION",
    "flowtran": "TRANSACTION", "batch_transaction": "BATCH_TRANSACTION",
    "file_batch_transaction": "FILE_BATCH_TRANSACTION", "batchStep": "BATCH_STEP",
    "batchStepGroup": "BATCH_GROUP", "index": "INDEX", "dbSequence": "SEQUENCE",
    "parameter": "SQL_PARAMETER", "result": "SQL_RESULT", "fields": "FIELDS",
}
CONTAINER_TAGS = {"fields", "indexes", "odbindexes", "parameterMap", "resultMap", "interface", "input", "output", "property", "printer", "flow"}
REFERENCE_ATTRS = {
    "type": "TYPE_REF", "javaType": "TYPE_REF", "base": "TYPE_REF", "ref": "DICT_REF",
    "extension": "EXTENDS", "serviceType": "IMPLEMENTS", "serviceName": "CALLS_SERVICE",
    "transactionId": "CALLS_TRANSACTION", "transaction": "CALLS_TRANSACTION",
    "dataItem": "TYPE_REF", "jobDataItem": "TYPE_REF", "groupInfo": "TYPE_REF",
    "class": "TYPE_REF", "clazz": "TYPE_REF", "resultClass": "TYPE_REF",
}


@dataclass(frozen=True)
class ScanSummary:
    discovered_files: int
    parsed_files: int
    failed_files: int
    nodes: int
    edges: int
    unresolved: int


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def recognized_suffix(path: Path) -> Optional[str]:
    name = path.name
    matches = [suffix for suffix in SUFFIXES if name.endswith(suffix)]
    return max(matches, key=len) if matches else None


def discover_model_files(workspace: Path) -> Iterator[Tuple[Path, str]]:
    for path in workspace.rglob("*.xml"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(workspace).parts[:-1]):
            continue
        suffix = recognized_suffix(path)
        if suffix:
            yield path, suffix


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable(file_path: str, full_id: str, kind: str, ordinal: int) -> str:
    # Full IDs are not globally unique for every nested XML node (for example,
    # ODB and DB indexes can intentionally reuse an ID). Preserve both nodes
    # instead of aborting the scan; `full_id` remains separately queryable.
    identity = f"{file_path}#{ordinal}:{full_id}" if full_id else f"{file_path}#{ordinal}"
    return f"model:{kind}:{identity}"


def _child_full_id(parent_full: str, tag: str, raw_id: str, root_id: str) -> str:
    if not raw_id:
        return ""
    if tag in {"schema", "sqls", "serviceType", "serviceImpl", "flowtran", "batch_transaction", "file_batch_transaction", "batchStep", "batchStepGroup"}:
        return raw_id
    if tag == "table":
        return f"{root_id}.{raw_id}" if root_id else raw_id
    if parent_full:
        return f"{parent_full}.{raw_id}"
    return f"{root_id}.{raw_id}" if root_id else raw_id


def _kind(tag: str, attrs: Dict[str, str]) -> str:
    if tag == "complexType" and attrs.get("dict", "false").lower() != "true":
        return "COMPLEX_TYPE"
    return TAG_KIND.get(tag, tag.upper())


def _insert_edge(conn: sqlite3.Connection, from_id: str, relation: str, evidence_path: str,
                 evidence_value: str, unresolved_target: Optional[str] = None,
                 to_id: Optional[str] = None, confidence: str = "CERTAIN") -> None:
    conn.execute(
        """insert or ignore into edges
           (from_id,to_id,unresolved_target,relation_kind,evidence_path,evidence_value,confidence)
           values (?,?,?,?,?,?,?)""",
        (from_id, to_id, unresolved_target, relation, evidence_path, evidence_value, confidence),
    )


def _parse_file(conn: sqlite3.Connection, workspace: Path, path: Path, suffix: str) -> bool:
    relative = path.relative_to(workspace).as_posix()
    content_hash = _hash(path)
    conn.execute("delete from model_files where path=?", (relative,))
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except (ET.ParseError, OSError) as exc:
        conn.execute(
            "insert into model_files(path,suffix,content_hash,parse_status,error_message) values(?,?,?,?,?)",
            (relative, suffix, content_hash, "PARSE_FAILED", str(exc)),
        )
        return False

    root_tag = _local(root.tag)
    root_id = root.attrib.get("id", "")
    package = root.attrib.get("package", "")
    conn.execute(
        """insert into model_files(path,suffix,content_hash,parse_status,root_name,model_id,package_name)
           values(?,?,?,?,?,?,?)""",
        (relative, suffix, content_hash, "PARSED", root_tag, root_id, package),
    )

    ordinal = 0

    def visit(element: ET.Element, owner_stable: Optional[str], parent_full: str) -> None:
        nonlocal ordinal
        tag = _local(element.tag)
        attrs = dict(element.attrib)
        raw_id = attrs.get("id", "")
        kind = _kind(tag, attrs)
        creates_node = bool(raw_id) or element is root or tag in CONTAINER_TAGS
        current_stable = owner_stable
        current_full = parent_full
        if creates_node:
            ordinal += 1
            full_id = ("" if tag in CONTAINER_TAGS and not raw_id
                       else _child_full_id(parent_full, tag, raw_id or root_id, root_id))
            current_stable = _stable(relative, full_id, kind, ordinal)
            current_full = full_id
            conn.execute(
                """insert into nodes(stable_id,kind,raw_id,full_id,owner_id,file_path,xml_tag,properties_json)
                   values(?,?,?,?,?,?,?,?)""",
                (current_stable, kind, raw_id or root_id, full_id, owner_stable, relative, tag,
                 json.dumps(attrs, ensure_ascii=False, sort_keys=True)),
            )
            if owner_stable:
                _insert_edge(conn, owner_stable, "CONTAINS", relative, full_id, to_id=current_stable)
            for attr, relation in REFERENCE_ATTRS.items():
                value = attrs.get(attr)
                if value:
                    _insert_edge(conn, current_stable, relation, relative, value, unresolved_target=value)
        child_parent_full = (parent_full if tag in CONTAINER_TAGS and not raw_id else current_full)
        for child in list(element):
            visit(child, current_stable, child_parent_full if creates_node else parent_full)

    visit(root, None, "")
    return True


def _resolve_edges(conn: sqlite3.Connection) -> None:
    rows = conn.execute("select id,unresolved_target from edges where to_id is null and unresolved_target is not null").fetchall()
    for row in rows:
        target = row["unresolved_target"]
        matches = conn.execute(
            "select stable_id from nodes where full_id=? order by stable_id limit 2", (target,)
        ).fetchall()
        if len(matches) == 1:
            conn.execute("update edges set to_id=?, unresolved_target=null where id=?", (matches[0][0], row["id"]))


def scan_workspace(workspace: Path | str, db_path: Path | str, fail_on_parse_error: bool = False) -> ScanSummary:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"workspace directory does not exist: {root}")
    conn = connect(db_path)
    discovered = parsed = failed = 0
    with conn:
        conn.execute("delete from edges")
        conn.execute("delete from nodes")
        conn.execute("delete from model_files")
        for path, suffix in discover_model_files(root):
            discovered += 1
            if _parse_file(conn, root, path, suffix):
                parsed += 1
            else:
                failed += 1
        if discovered == 0:
            raise ValueError(f"workspace contains no recognized APS model files: {root}")
        _resolve_edges(conn)
    stats = get_stats(conn)
    summary = ScanSummary(discovered, parsed, failed, stats["nodes"], stats["edges"], stats["unresolved"])
    conn.close()
    if fail_on_parse_error and failed:
        raise ValueError(f"{failed} model file(s) failed to parse")
    return summary
