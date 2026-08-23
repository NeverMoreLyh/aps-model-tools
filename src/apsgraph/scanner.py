from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
import xml.etree.ElementTree as ET

from .store import SCHEMA_VERSION, connect, get_stats, initialize_schema, is_aps_index

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
EXCLUDED_DIRS = {".git", "target", ".idea", ".codegraph", ".apsgraph", ".hermes", "node_modules"}
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
    unresolved_models: List[str]


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
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        conn.execute("delete from model_files where path=?", (relative,))
        conn.execute(
            "insert into model_files(path,suffix,content_hash,parse_status,error_message) values(?,?,?,?,?)",
            (relative, suffix, content_hash, "PARSE_FAILED", str(exc)),
        )
        return False
    return _register_parsed(conn, relative, suffix, content_hash, root)


def _register_parsed(conn: sqlite3.Connection, logical_path: str, suffix: str,
                     content_hash: bytes, root: ET.Element) -> bool:
    """Register an already-parsed model document under a logical path.

    Shared by workspace file scanning and Maven dependency jar imports.
    """
    root_tag = _local(root.tag)
    root_id = root.attrib.get("id", "")
    package = root.attrib.get("package", "")
    conn.execute("delete from model_files where path=?", (logical_path,))
    cursor = conn.execute(
        """insert into model_files(path,suffix,content_hash,parse_status,root_name,model_id,package_name)
        values(?,?,?,?,?,?,?)""",
        (logical_path, suffix, content_hash, "PARSED", root_tag, root_id, package),
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
            stable = _stable(logical_path, full_id, kind, ordinal)
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


def jar_logical_path(jar: Path, entry: str) -> str:
    return f"jar:{jar}!/{entry}"


def import_jar_models(db_path: Path | str, jars: List[Path | str],
                      reresolve: bool = True) -> Dict[str, object]:
    """Import APS model XML documents found inside Maven dependency jars.

    Framework base models (KBaseType, SPType, GeneralFileService*, ...) live in
    dependency jars, not in the business workspace; the native MavenModelLoader
    traverses dependency jars for exactly this reason. Entries are registered
    under logical paths ``jar:<jar>!/<entry>`` so sync never treats them as
    workspace files.
    """
    target = Path(db_path).resolve()
    conn = connect(target)
    imported = 0
    failed = 0
    skipped = 0
    per_jar: Dict[str, int] = {}
    try:
        with conn:
            for jar in jars:
                jar_path = Path(jar).resolve()
                if not jar_path.is_file():
                    raise FileNotFoundError(f"jar not found: {jar_path}")
                count = 0
                with zipfile.ZipFile(jar_path) as archive:
                    for info in archive.infolist():
                        if info.is_dir():
                            continue
                        suffix = recognized_suffix(Path(info.filename))
                        if not suffix:
                            continue
                        logical = jar_logical_path(jar_path, info.filename)
                        data = archive.read(info.filename)
                        content_hash = hashlib.sha256(data).digest()
                        try:
                            root = ET.fromstring(data)
                        except ET.ParseError as exc:
                            conn.execute("delete from model_files where path=?", (logical,))
                            conn.execute(
                                "insert into model_files(path,suffix,content_hash,parse_status,error_message) values(?,?,?,?,?)",
                                (logical, suffix, content_hash, "PARSE_FAILED", str(exc)),
                            )
                            failed += 1
                            continue
                        _register_parsed(conn, logical, suffix, content_hash, root)
                        imported += 1
                        count += 1
                per_jar[str(jar_path)] = count
                if count == 0:
                    skipped += 1
            if reresolve:
                _resolve_edges(conn)
        stats = get_stats(conn)
    finally:
        conn.close()
    return {
        "jars": len(jars),
        "jars_without_models": skipped,
        "imported_files": imported,
        "failed_files": failed,
        "per_jar": per_jar,
        "nodes": stats["nodes"],
        "edges": stats["edges"],
        "unresolved": stats["unresolved"],
    }


def _external_logical_path(source_db: Path, model_path: str) -> str:
    return f"external-db:{source_db}!/{model_path}"


def import_external_indexes(
    db_path: Path | str,
    external_indexes: List[Path | str],
    reresolve: bool = True,
) -> Dict[str, object]:
    """Merge model nodes from dependency APS indexes into a workspace index.

    Workspace definitions win when both indexes expose the same ``full_id``.
    Imported files use ``external-db:<db>!<model>`` logical paths and therefore
    remain stable during workspace sync.  Reference edges are re-resolved after
    the merge, allowing local workspace XML to reference dependency models.
    """
    target = Path(db_path).resolve()
    conn = connect(target)
    imported_files = 0
    imported_nodes = 0
    imported_edges = 0
    duplicated_nodes = 0
    per_index: Dict[str, Dict[str, int]] = {}
    try:
        with conn:
            for raw_index in external_indexes:
                source_path = Path(raw_index).resolve()
                if source_path == target:
                    raise ValueError(f"external index cannot be the target index: {source_path}")
                if not source_path.is_file():
                    raise FileNotFoundError(f"external index does not exist: {source_path}")
                source = connect(source_path, read_only=True)
                counts = {"files": 0, "nodes": 0, "edges": 0, "duplicated_nodes": 0}
                try:
                    file_map: Dict[int, int] = {}
                    for row in source.execute(
                        "select id,path,suffix,content_hash,parse_status,root_name,model_id,package_name,error_message from model_files order by id"
                    ):
                        logical = _external_logical_path(source_path, row["path"])
                        cursor = conn.execute(
                            """delete from model_files where path=?""",
                            (logical,),
                        )
                        cursor = conn.execute(
                            """insert into model_files
                            (path,suffix,content_hash,parse_status,root_name,model_id,package_name,error_message)
                            values(?,?,?,?,?,?,?,?)""",
                            (logical, row["suffix"], row["content_hash"], row["parse_status"],
                             row["root_name"], row["model_id"], row["package_name"], row["error_message"]),
                        )
                        file_map[row["id"]] = cursor.lastrowid
                        imported_files += 1
                        counts["files"] += 1

                    node_map: Dict[int, int] = {}
                    full_id_map: Dict[str, int] = {
                        row[0]: row[1] for row in conn.execute(
                            "select full_id,id from nodes where full_id is not null and full_id!=''"
                        )
                    }
                    for row in source.execute(
                        "select id,stable_id,kind,raw_id,full_id,owner_node_id,file_id,xml_tag,properties_json from nodes order by id"
                    ):
                        existing = full_id_map.get(row["full_id"]) if row["full_id"] else None
                        if existing is not None:
                            node_map[row["id"]] = existing
                            duplicated_nodes += 1
                            counts["duplicated_nodes"] += 1
                            continue
                        cursor = conn.execute(
                            """insert into nodes
                            (stable_id,kind,raw_id,full_id,owner_node_id,file_id,xml_tag,properties_json)
                            values(?,?,?,?,?,?,?,?)""",
                            (f"{row['stable_id']}@{source_path}", row["kind"], row["raw_id"],
                             row["full_id"], node_map.get(row["owner_node_id"]),
                             file_map.get(row["file_id"]), row["xml_tag"], row["properties_json"]),
                        )
                        node_map[row["id"]] = cursor.lastrowid
                        if row["full_id"]:
                            full_id_map[row["full_id"]] = cursor.lastrowid
                        imported_nodes += 1
                        counts["nodes"] += 1

                    source_nodes = {
                        row[0]: row for row in source.execute(
                            "select id,full_id from nodes order by id"
                        )
                    }
                    for row in source.execute(
                        "select id,from_node_id,to_node_id,raw_target,relation_kind,evidence_file_id,evidence_value,confidence from edges order by id"
                    ):
                        from_id = node_map.get(row["from_node_id"])
                        if from_id is None:
                            continue
                        raw_target = row["raw_target"]
                        if raw_target is None and row["to_node_id"] is not None:
                            raw_target = source_nodes[row["to_node_id"]]["full_id"] or None
                        conn.execute(
                            """insert into edges
                            (from_node_id,to_node_id,raw_target,relation_kind,evidence_file_id,evidence_value,confidence)
                            values(?,?,?,?,?,?,?)""",
                            (from_id, node_map.get(row["to_node_id"]), raw_target,
                             row["relation_kind"], file_map.get(row["evidence_file_id"]),
                             row["evidence_value"], row["confidence"]),
                        )
                        imported_edges += 1
                        counts["edges"] += 1
                finally:
                    source.close()
                per_index[str(source_path)] = counts
            if reresolve:
                _resolve_edges(conn)
        stats = get_stats(conn)
    finally:
        conn.close()
    return {
        "external_indexes": len(external_indexes),
        "imported_files": imported_files,
        "imported_nodes": imported_nodes,
        "imported_edges": imported_edges,
        "duplicated_nodes": duplicated_nodes,
        "per_index": per_index,
        "nodes": stats["nodes"],
        "edges": stats["edges"],
        "unresolved": stats["unresolved"],
    }


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
    unresolved_models = [
        row[0] for row in conn.execute(
            """select distinct raw_target from edges
               where to_node_id is null and raw_target is not null
               order by raw_target"""
        )
    ]
    return ScanSummary(
        discovered, stats["parsed"], stats["parse_failed"], stats["nodes"],
        stats["edges"], stats["unresolved"], unresolved_models,
    )


def scan_workspace(workspace: Path | str, db_path: Path | str, fail_on_parse_error: bool = False) -> ScanSummary:
    root = Path(workspace).resolve()
    _validate_workspace(root)
    files = sorted(discover_model_files(root), key=lambda item: item[0].as_posix())
    if not files:
        raise ValueError(f"workspace contains no recognized APS model files: {root}")
    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0:
        check = connect(target, initialize=False)
        try:
            if not is_aps_index(check):
                raise ValueError("refusing to rebuild database without a valid APS index identity")
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
        if fail_on_parse_error and summary.failed_files:
            raise ValueError(f"{summary.failed_files} model file(s) failed to parse")
        conn.close()
        conn = None
        os.replace(temp_path, target)
    finally:
        if conn is not None:
            conn.close()
        if temp_path.exists():
            temp_path.unlink()
    return summary


def scan_workspace_with_external_indexes(
    workspace: Path | str,
    db_path: Path | str,
    external_indexes: List[Path | str],
    fail_on_parse_error: bool = False,
) -> Tuple[ScanSummary, Dict[str, object]]:
    """Scan workspace XML, merge dependency indexes, then publish atomically."""
    root = Path(workspace).resolve()
    _validate_workspace(root)
    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".external.tmp", dir=str(target.parent)
    )
    os.close(fd)
    staging = Path(temp_name)
    staging.unlink()
    try:
        summary = scan_workspace(root, staging, fail_on_parse_error=fail_on_parse_error)
        result = import_external_indexes(staging, external_indexes)
        os.replace(staging, target)
        return summary, result
    finally:
        if staging.exists():
            staging.unlink()


def _change_set(root: Path, conn: sqlite3.Connection):
    discovered = {path.relative_to(root).as_posix(): (path, suffix, _hash(path))
                  for path, suffix in discover_model_files(root)}
    # jar-imported models (logical path prefix "jar:") are dependency artifacts,
    # never workspace files; they must not participate in workspace diffing.
    existing = {row["path"]: bytes(row["content_hash"])
                for row in conn.execute("select path,content_hash from model_files")
                if not row["path"].startswith(("jar:", "external-db:"))}
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
