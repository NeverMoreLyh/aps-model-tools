from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


SCHEMA_VERSION = 2
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS model_files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    suffix TEXT NOT NULL,
    content_hash BLOB NOT NULL,
    parse_status TEXT NOT NULL,
    root_name TEXT,
    model_id TEXT,
    package_name TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    stable_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    raw_id TEXT,
    full_id TEXT,
    owner_node_id INTEGER,
    file_id INTEGER NOT NULL,
    xml_tag TEXT,
    properties_json TEXT NOT NULL,
    FOREIGN KEY(owner_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(file_id) REFERENCES model_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nodes_full_id ON nodes(full_id);
CREATE INDEX IF NOT EXISTS idx_nodes_raw_id ON nodes(raw_id);
CREATE INDEX IF NOT EXISTS idx_nodes_owner ON nodes(owner_node_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_id);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    from_node_id INTEGER NOT NULL,
    to_node_id INTEGER,
    raw_target TEXT,
    relation_kind TEXT NOT NULL,
    evidence_file_id INTEGER NOT NULL,
    evidence_value TEXT,
    confidence TEXT NOT NULL,
    UNIQUE(from_node_id, relation_kind, evidence_file_id, evidence_value, raw_target, to_node_id),
    FOREIGN KEY(from_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(to_node_id) REFERENCES nodes(id) ON DELETE SET NULL,
    FOREIGN KEY(evidence_file_id) REFERENCES model_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_raw_target ON edges(raw_target) WHERE raw_target IS NOT NULL;
CREATE TABLE IF NOT EXISTS scan_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    workspace TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scanner_version TEXT NOT NULL
);
"""


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("select count(*) from sqlite_schema where type='table' and name not like 'sqlite_%'").fetchone()[0] > 0


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("pragma user_version").fetchone()[0])


def initialize_schema(conn: sqlite3.Connection, reset: bool = False) -> None:
    if reset:
        conn.executescript("""
        PRAGMA foreign_keys=OFF;
        DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS nodes;
        DROP TABLE IF EXISTS model_files;
        DROP TABLE IF EXISTS scan_state;
        PRAGMA foreign_keys=ON;
        """)
    elif _has_user_tables(conn) and _schema_version(conn) != SCHEMA_VERSION:
        raise ValueError("legacy APS index schema; run a full scan to rebuild it as schema v2")
    conn.executescript(SCHEMA)
    conn.execute(f"pragma user_version={SCHEMA_VERSION}")


def connect(db_path: Path | str, read_only: bool = False, initialize: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(f"index database does not exist: {path}")
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        if _schema_version(conn) != SCHEMA_VERSION:
            conn.close()
            raise ValueError("unsupported APS index schema; rebuild with scan")
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys=on")
    if initialize:
        initialize_schema(conn)
    return conn


def get_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        "files": conn.execute("select count(*) from model_files").fetchone()[0],
        "parsed": conn.execute("select count(*) from model_files where parse_status='PARSED'").fetchone()[0],
        "parse_failed": conn.execute("select count(*) from model_files where parse_status='PARSE_FAILED'").fetchone()[0],
        "nodes": conn.execute("select count(*) from nodes").fetchone()[0],
        "edges": conn.execute("select count(*) from edges").fetchone()[0],
        "unresolved": conn.execute("select count(*) from edges where to_node_id is null and raw_target is not null").fetchone()[0],
    }


NODE_SELECT = """select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,
       owner.stable_id as owner_id,n.owner_node_id,
       f.path as file_path,n.file_id,n.xml_tag,n.properties_json
from nodes n join model_files f on f.id=n.file_id
left join nodes owner on owner.id=n.owner_node_id"""

EDGE_SELECT = """select e.id,src.stable_id as from_id,dst.stable_id as to_id,
       e.from_node_id,e.to_node_id,e.raw_target as unresolved_target,e.raw_target,
       e.relation_kind,f.path as evidence_path,e.evidence_file_id,
       e.evidence_value,e.confidence
from edges e join nodes src on src.id=e.from_node_id
left join nodes dst on dst.id=e.to_node_id
join model_files f on f.id=e.evidence_file_id"""


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    if "properties_json" in data:
        data["properties"] = json.loads(data.pop("properties_json"))
    return data


def find_nodes(conn: sqlite3.Connection, query: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        NODE_SELECT + """ where n.stable_id=? or n.full_id=? or n.raw_id=?
        order by case when n.stable_id=? then 0 when n.full_id=? then 1 else 2 end,n.kind,f.path""",
        (query, query, query, query, query),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def references(conn: sqlite3.Connection, stable_id: str, direction: str = "both", depth: int = 1) -> Dict[str, Any]:
    if direction not in {"in", "out", "both"}:
        raise ValueError("direction must be in, out, or both")
    row = conn.execute("select id from nodes where stable_id=?", (stable_id,)).fetchone()
    if not row:
        raise ValueError(f"model not found: {stable_id}")
    root_id = row[0]
    seen = {root_id}
    frontier = {root_id}
    result_edges: Dict[int, Dict[str, Any]] = {}
    for level in range(1, depth + 1):
        if not frontier:
            break
        marks = ",".join("?" for _ in frontier)
        clauses, params = [], []
        if direction in {"out", "both"}:
            clauses.append(f"e.from_node_id in ({marks})")
            params.extend(frontier)
        if direction in {"in", "both"}:
            clauses.append(f"e.to_node_id in ({marks})")
            params.extend(frontier)
        rows = conn.execute(EDGE_SELECT + " where " + " or ".join(clauses) + " order by e.relation_kind,e.id", params).fetchall()
        next_frontier = set()
        for edge_row in rows:
            edge = dict(edge_row)
            edge["depth"] = level
            result_edges[edge["id"]] = edge
            for candidate in (edge["from_node_id"], edge["to_node_id"]):
                if candidate is not None and candidate not in seen:
                    seen.add(candidate)
                    next_frontier.add(candidate)
        frontier = next_frontier
    marks = ",".join("?" for _ in seen)
    nodes = [_row_to_dict(row) for row in conn.execute(NODE_SELECT + f" where n.id in ({marks}) order by n.kind,n.full_id,f.path", list(seen)).fetchall()]
    return {"root": stable_id, "nodes": nodes, "edges": list(result_edges.values())}
