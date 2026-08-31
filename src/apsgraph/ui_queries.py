"""Read-only APSGraph UI query helpers."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional


def _props(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        return json.loads(row["properties_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _node(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result["properties"] = _props(row)
    result.pop("properties_json", None)
    return result


def list_models(conn: sqlite3.Connection, query: str = "", kind: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    where, params = [], []
    if query:
        where.append("(n.full_id like ? or n.raw_id like ? or n.stable_id like ? or f.path like ?)")
        value = f"%{query}%"
        params.extend([value, value, value, value])
    if kind:
        where.append("n.kind=?")
        params.append(kind)
    clause = " where " + " and ".join(where) if where else ""
    rows = conn.execute(
        "select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.owner_node_id,f.path as file_path,"
        "n.file_id,n.xml_tag,n.properties_json from nodes n join model_files f on f.id=n.file_id" +
        clause + " order by n.kind,n.full_id,f.path limit ?", params + [limit]
    ).fetchall()
    return [_node(row) for row in rows]


def get_model(conn: sqlite3.Connection, stable_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.owner_node_id,f.path as file_path,"
        "n.file_id,n.xml_tag,n.properties_json from nodes n join model_files f on f.id=n.file_id where n.stable_id=?",
        (stable_id,),
    ).fetchone()
    if not row:
        return None
    item = _node(row)
    item["children"] = [_node(child) for child in conn.execute(
        "select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.owner_node_id,f.path as file_path,"
        "n.file_id,n.xml_tag,n.properties_json from nodes n join model_files f on f.id=n.file_id where n.owner_node_id=? order by n.kind,n.full_id",
        (row["id"],),
    )]
    item["edges"] = [dict(edge) for edge in conn.execute(
        "select e.id,src.stable_id as from_id,dst.stable_id as to_id,e.raw_target,e.relation_kind,"
        "e.evidence_value,e.confidence,ef.path as evidence_path from edges e join nodes src on src.id=e.from_node_id "
        "left join nodes dst on dst.id=e.to_node_id join model_files ef on ef.id=e.evidence_file_id "
        "where e.from_node_id=? or e.to_node_id=? order by e.relation_kind,e.id", (row["id"], row["id"])
    )]
    if item["kind"] == "TRANSACTION":
        sections = {"basic": [], "interfaces": [], "mappings": [], "orchestration": []}
        for child in item["children"]:
            tag = (child.get("xml_tag") or "").lower()
            kind = (child.get("kind") or "").upper()
            if tag in {"interface", "input", "output", "fields", "field", "parameter"} or "INTERFACE" in kind or "PARAMETER" in kind:
                sections["interfaces"].append(child)
            elif tag in {"mapping", "in_mappings", "out_mappings"} or "MAPPING" in kind:
                sections["mappings"].append(child)
            elif tag in {"flow", "service", "transaction", "condition", "route", "node"} or "FLOW" in kind or "CALL" in kind:
                sections["orchestration"].append(child)
        item["sections"] = sections
    return item


def get_embedded_xml(conn: sqlite3.Connection, stable_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "select f.path,n.full_id,n.xml_tag,x.content,x.content_encoding from nodes n join model_files f on f.id=n.file_id "
        "left join xml_documents x on x.file_id=f.id where n.stable_id=?", (stable_id,)
    ).fetchone()
    if not row:
        return None
    content = row["content"]
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return {"path": row["path"], "full_id": row["full_id"], "xml_tag": row["xml_tag"],
            "available": content is not None, "content": content, "content_encoding": row["content_encoding"]}
