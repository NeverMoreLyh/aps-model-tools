from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple
import xml.etree.ElementTree as ET

from .store import SCHEMA_VERSION, connect, get_stats, initialize_schema

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
SCANNER_VERSION = "2"

TAG_KIND = {
    "schema": "SCHEMA", "restrictionType": "RESTRICTION_TYPE", "enumeration": "ENUM_VALUE",
    "complexType": "DICTIONARY", "element": "ELEMENT", "table": "TABLE", "field": "FIELD",
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


@dataclass(frozen=True)
class WorkspaceStatus:
    added: int
    modified: int
    deleted: int
    unchanged: int
    up_to_date: bool


@dataclass(frozen=True)
class SyncSummary:
    added: int
    modified: int
    deleted: int
    unchanged: int
    parsed_files: int
    failed_files: int
    nodes: int
    edges: int
    unresolved: int


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def recognized_suffix(path: Path) -> Optional[str]:
    matches = [suffix for suffix in SUFFIXES if path.name.endswith(suffix)]
    return max(matches, key=len) if matches else None


def discover_model_files(workspace: Path) -> Iterator[Tuple[Path, str]]:
    for path in workspace.rglob("*.xml"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(workspace).parts[:-1]):
            continue
        suffix = recognized_suffix(path)
        if suffix:
            yield path, suffix


def _hash(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _stable(file_path: str, full_id: str, kind: str, ordinal: int) -> str:
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


def _insert_edge(conn: sqlite3.Connection, from_node_id: int, relation: str, file_id: int,
                 evidence_value: str, raw_target: Optional[str] = None,
                 to_node_id: Optional[int] = None, confidence: str = "CERTAIN") -> None:
    conn.execute(
        """insert into edges
        (from_node_id,to_node_id,raw_target,relation_kind,evidence_file_id,evidence_value,confidence)
        values(?,?,?,?,?,?,?)""",
        (from_node_id, to_node_id, raw_target, relation, file_id, evidence_value, confidence),
    )


def _parse_file(conn: sqlite3.Connection, workspace: Path, path: Path, suffix: str) -> bool:
    relative = path.relative_to(workspace).as_posix()
    content_hash = _hash(path)
    conn.execute("delete from model_files where path=?", (relative,))
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        conn.execute(
            "insert into model_files(path,suffix,content_hash,parse_status,error_message) values(?,?,?,?,?)",
            (relative, suffix, content_hash, "PARSE_FAILED", str(exc)),
        )
        return False
    root_tag = _local(root.tag)
    root_id = root.attrib.get("id", "")
    package = root.attrib.get("package", "")
    cursor = conn.execute(
        """insert into model_files(path,suffix,content_hash,parse_status,root_name,model_id,package_name)
        values(?,?,?,?,?,?,?)""",
        (relative, suffix, content_hash, "PARSED", root_tag, root_id, package),
    )
    file_id = cursor.lastrowid
    ordinal = 0

    def visit(element: ET.Element, owner_node_id: Optional[int], parent_full: str) -> None:
        nonlocal ordinal
        tag = _local(element.tag)
        attrs = dict(element.attrib)
        raw_id = attrs.get("id", "")
        kind = _kind(tag, attrs)
        creates_node = bool(raw_id) or element is root or tag in CONTAINER_TAGS
        current_node_id = owner_node_id
        current_full = parent_full
        if creates_node:
            ordinal += 1
            full_id = "" if tag in CONTAINER_TAGS and not raw_id else _child_full_id(parent_full, tag, raw_id or root_id, root_id)
            stable = _stable(relative, full_id, kind, ordinal)
            current_node_id = conn.execute(
                """insert into nodes(stable_id,kind,raw_id,full_id,owner_node_id,file_id,xml_tag,properties_json)
                values(?,?,?,?,?,?,?,?)""",
                (stable, kind, raw_id or root_id, full_id, owner_node_id, file_id, tag,
                 json.dumps(attrs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            ).lastrowid
            current_full = full_id
            if owner_node_id is not None:
                _insert_edge(conn, owner_node_id, "CONTAINS", file_id, full_id, to_node_id=current_node_id)
            for attr, relation in REFERENCE_ATTRS.items():
                value = attrs.get(attr)
                if value:
                    _insert_edge(conn, current_node_id, relation, file_id, value, raw_target=value)
        child_parent_full = parent_full if tag in CONTAINER_TAGS and not raw_id else current_full
        for child in list(element):
            visit(child, current_node_id, child_parent_full if creates_node else parent_full)

    visit(root, None, "")
    return True


def _resolve_edges(conn: sqlite3.Connection) -> None:
    conn.execute("update edges set to_node_id=null where raw_target is not null")
    conn.execute("""update edges set to_node_id=(
        select min(n.id) from nodes n where n.full_id=edges.raw_target
        having count(*)=1
    ) where raw_target is not null""")


def _validate_workspace(root: Path) -> None:
    if not root.is_dir():
        raise FileNotFoundError(f"workspace directory does not exist: {root}")


def _summary(conn: sqlite3.Connection, discovered: int) -> ScanSummary:
    stats = get_stats(conn)
    return ScanSummary(discovered, stats["parsed"], stats["parse_failed"], stats["nodes"], stats["edges"], stats["unresolved"])


def scan_workspace(workspace: Path | str, db_path: Path | str, fail_on_parse_error: bool = False) -> ScanSummary:
    root = Path(workspace).resolve()
    _validate_workspace(root)
    files = sorted(discover_model_files(root), key=lambda item: item[0].as_posix())
    if not files:
        raise ValueError(f"workspace contains no recognized APS model files: {root}")
    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        check = connect(target, initialize=False)
        try:
            existing = {row[0] for row in check.execute("select name from sqlite_schema where type='table' and name not like 'sqlite_%'")}
            unknown = existing - {"edges", "nodes", "model_files", "scan_state"}
            if unknown:
                raise ValueError(f"refusing to rebuild database with non-APS tables: {', '.join(sorted(unknown))}")
        finally:
            check.close()
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    conn = connect(temp_path, initialize=False)
    try:
        with conn:
            initialize_schema(conn, reset=True)
            for model_path, suffix in files:
                _parse_file(conn, root, model_path, suffix)
            _resolve_edges(conn)
            conn.execute("insert or replace into scan_state(id,workspace,scanner_version) values(1,?,?)", (str(root), SCANNER_VERSION))
        summary = _summary(conn, len(files))
        conn.close()
        conn = None
        os.replace(temp_path, target)
    finally:
        if conn is not None:
            conn.close()
        if temp_path.exists():
            temp_path.unlink()
    if fail_on_parse_error and summary.failed_files:
        raise ValueError(f"{summary.failed_files} model file(s) failed to parse")
    return summary


def _change_set(root: Path, conn: sqlite3.Connection):
    discovered = {path.relative_to(root).as_posix(): (path, suffix, _hash(path))
                  for path, suffix in discover_model_files(root)}
    existing = {row["path"]: bytes(row["content_hash"]) for row in conn.execute("select path,content_hash from model_files")}
    if not discovered and not existing:
        raise ValueError(f"workspace contains no recognized APS model files: {root}")
    added = sorted(set(discovered) - set(existing))
    deleted = sorted(set(existing) - set(discovered))
    modified = sorted(path for path in set(existing) & set(discovered) if existing[path] != discovered[path][2])
    unchanged = len(discovered) - len(added) - len(modified)
    return discovered, added, modified, deleted, unchanged


def workspace_status(workspace: Path | str, db_path: Path | str) -> WorkspaceStatus:
    root = Path(workspace).resolve()
    _validate_workspace(root)
    conn = connect(db_path, read_only=True)
    try:
        state = conn.execute("select workspace from scan_state where id=1").fetchone()
        if state and Path(state[0]).resolve() != root:
            raise ValueError(f"index belongs to another workspace: {state[0]}")
        _, added, modified, deleted, unchanged = _change_set(root, conn)
        return WorkspaceStatus(len(added), len(modified), len(deleted), unchanged, not (added or modified or deleted))
    finally:
        conn.close()


def sync_workspace(workspace: Path | str, db_path: Path | str, fail_on_parse_error: bool = False) -> SyncSummary:
    root = Path(workspace).resolve()
    _validate_workspace(root)
    conn = connect(db_path)
    try:
        state = conn.execute("select workspace from scan_state where id=1").fetchone()
        if state and Path(state[0]).resolve() != root:
            raise ValueError(f"index belongs to another workspace: {state[0]}")
        discovered, added, modified, deleted, unchanged = _change_set(root, conn)
        with conn:
            for path in deleted:
                conn.execute("delete from model_files where path=?", (path,))
            for path in modified:
                conn.execute("delete from model_files where path=?", (path,))
            for path in added + modified:
                model_path, suffix, _ = discovered[path]
                _parse_file(conn, root, model_path, suffix)
            _resolve_edges(conn)
            conn.execute("insert or replace into scan_state(id,workspace,scanner_version) values(1,?,?)", (str(root), SCANNER_VERSION))
        stats = get_stats(conn)
        result = SyncSummary(len(added), len(modified), len(deleted), unchanged,
                             stats["parsed"], stats["parse_failed"], stats["nodes"], stats["edges"], stats["unresolved"])
    finally:
        conn.close()
    if fail_on_parse_error and result.failed_files:
        raise ValueError(f"{result.failed_files} model file(s) failed to parse")
    return result
