from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List
import sqlite3

from .store import EDGE_SELECT, find_nodes, references


IMPACT_RELATIONS = {
    "TYPE_REF", "DICT_REF", "EXTENDS", "IMPLEMENTS", "CALLS_SERVICE",
    "CALLS_TRANSACTION", "USES_NAMED_SQL", "READS_TABLE", "WRITES_TABLE",
    "GENERATES", "IMPLEMENTED_BY", "REGISTERED_AS", "RESOLVES_TO",
}


def _resolve_one(conn: sqlite3.Connection, query: str) -> Dict[str, Any]:
    nodes = find_nodes(conn, query)
    if not nodes:
        raise ValueError(f"model not found: {query}")
    if len(nodes) > 1:
        candidates = ", ".join(f"{n['kind']}:{n['full_id']}@{n['file_path']}" for n in nodes)
        raise ValueError(f"ambiguous model '{query}': {candidates}")
    return nodes[0]


def build_impact_report(conn: sqlite3.Connection, query: str, depth: int = 3) -> Dict[str, Any]:
    target = _resolve_one(conn, query)
    graph = references(conn, target["stable_id"], "in", depth)
    edges = [e for e in graph["edges"] if e["relation_kind"] in IMPACT_RELATIONS]
    affected_node_ids = {e["from_node_id"] for e in edges if e["from_id"] != target["stable_id"]}
    affected_stable_ids = {e["from_id"] for e in edges if e["from_id"] != target["stable_id"]}
    affected = [n for n in graph["nodes"] if n["stable_id"] in affected_stable_ids]
    summary = Counter(e["relation_kind"] for e in edges)
    questions = [
        e for e in conn.execute(
            EDGE_SELECT + " where e.from_node_id in ({}) and e.to_node_id is null and e.raw_target is not null".format(
                ",".join("?" for _ in affected_node_ids) if affected_node_ids else "''"
            ),
            list(affected_node_ids),
        ).fetchall()
    ] if affected_node_ids else []
    return {
        "target": target,
        "summary": dict(summary),
        "affected_nodes": affected,
        "paths": edges,
        "questions": [dict(row) for row in questions],
        "recommendations": [
            "Regenerate owning top-level model files for changed nested models.",
            "Compile handwritten Java consumers after generation.",
            "Review database/API compatibility before applying the change.",
        ],
    }
