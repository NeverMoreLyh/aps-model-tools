from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS model_files (
    path TEXT PRIMARY KEY,
    suffix TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    root_name TEXT,
    model_id TEXT,
    package_name TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    stable_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    raw_id TEXT,
    full_id TEXT,
    owner_id TEXT,
    file_path TEXT NOT NULL,
    xml_tag TEXT,
    properties_json TEXT NOT NULL,
    FOREIGN KEY(file_path) REFERENCES model_files(path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nodes_full_id ON nodes(full_id);
CREATE INDEX IF NOT EXISTS idx_nodes_raw_id ON nodes(raw_id);
CREATE INDEX IF NOT EXISTS idx_nodes_owner_id ON nodes(owner_id);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id TEXT NOT NULL,
    to_id TEXT,
    unresolved_target TEXT,
    relation_kind TEXT NOT NULL,
    evidence_path TEXT NOT NULL,
    evidence_value TEXT,
    confidence TEXT NOT NULL,
    UNIQUE(from_id, relation_kind, evidence_path, evidence_value, to_id, unresolved_target),
    FOREIGN KEY(from_id) REFERENCES nodes(stable_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    result = {
        "files": conn.execute("select count(*) from model_files").fetchone()[0],
        "parsed": conn.execute("select count(*) from model_files where parse_status='PARSED'").fetchone()[0],
        "parse_failed": conn.execute("select count(*) from model_files where parse_status='PARSE_FAILED'").fetchone()[0],
        "nodes": conn.execute("select count(*) from nodes").fetchone()[0],
        "edges": conn.execute("select count(*) from edges").fetchone()[0],
        "unresolved": conn.execute("select count(*) from edges where to_id is null and unresolved_target is not null").fetchone()[0],
    }
    return result


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    if "properties_json" in data:
        data["properties"] = json.loads(data.pop("properties_json"))
    return data


def find_nodes(conn: sqlite3.Connection, query: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """select * from nodes
           where stable_id=? or full_id=? or raw_id=?
           order by case when stable_id=? then 0 when full_id=? then 1 else 2 end,
                    kind, file_path""",
        (query, query, query, query, query),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def references(conn: sqlite3.Connection, stable_id: str, direction: str = "both", depth: int = 1) -> Dict[str, Any]:
    if direction not in {"in", "out", "both"}:
        raise ValueError("direction must be in, out, or both")
    seen_nodes = {stable_id}
    frontier = {stable_id}
    result_edges: Dict[int, Dict[str, Any]] = {}
    for level in range(1, depth + 1):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        clauses = []
        params: List[str] = []
        if direction in {"out", "both"}:
            clauses.append(f"from_id in ({placeholders})")
            params.extend(frontier)
        if direction in {"in", "both"}:
            clauses.append(f"to_id in ({placeholders})")
            params.extend(frontier)
        rows = conn.execute(
            "select * from edges where " + " or ".join(clauses) + " order by relation_kind,evidence_path,id",
            params,
        ).fetchall()
        next_frontier = set()
        for row in rows:
            edge = dict(row)
            edge["depth"] = level
            result_edges[row["id"]] = edge
            for candidate in (row["from_id"], row["to_id"]):
                if candidate and candidate not in seen_nodes:
                    seen_nodes.add(candidate)
                    next_frontier.add(candidate)
        frontier = next_frontier
    nodes = []
    if seen_nodes:
        placeholders = ",".join("?" for _ in seen_nodes)
        nodes = [_row_to_dict(row) for row in conn.execute(
            f"select * from nodes where stable_id in ({placeholders}) order by kind,full_id,file_path",
            list(seen_nodes),
        ).fetchall()]
    return {"root": stable_id, "nodes": nodes, "edges": list(result_edges.values())}
