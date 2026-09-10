from __future__ import annotations

import json
import sys
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .impact import build_impact_report
from .search_scope import SearchScope
from .store import EDGE_SELECT, connect, find_nodes, references, search_nodes

SERVER_NAME = "apsgraph-metadata"
SERVER_VERSION = "0.1.0"

_SCOPE_PROPERTIES = {
    "kinds": {"type": "array", "items": {"type": "string"}},
    "project": {"type": "string"}, "module": {"type": "string"},
    "path": {"type": "string"}, "file": {"type": "string"},
    "owner": {"type": "string"}, "top_level": {"type": "boolean"},
}
_SCOPE_SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    **_SCOPE_PROPERTIES,
}}

TOOLS = [
    {"name": "search_metadata", "description": "Search APS metadata by fuzzy text across IDs, Chinese names, descriptions, and source paths. Scope supports metadata kind, project, module, file path, owner, and top-level nodes.", "inputSchema": {**_SCOPE_SCHEMA, "required": ["query"]}},
    {"name": "find_entity", "description": "Find an exact APS metadata entity by stable_id, full_id, or raw_id. Scope filters prevent ambiguity.", "inputSchema": {**_SCOPE_SCHEMA, "required": ["query"]}},
    {"name": "find_references", "description": "Find incoming and/or outgoing graph references for an exact entity.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "direction": {"type": "string", "enum": ["in", "out", "both"]}, "depth": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}},
    {"name": "get_dependencies", "description": "Traverse outgoing metadata dependencies from an exact entity.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}},
    {"name": "get_impact", "description": "Analyze reverse metadata impact for an exact entity.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}},
    {"name": "find_unresolved_references", "description": "List unresolved metadata references in the current SQLite index.", "inputSchema": {"type": "object", "properties": {"relation_type": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}}},
    {"name": "get_entity_source", "description": "Return the source XML location and raw XML for an exact entity.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 100, "maximum": 200000}, **_SCOPE_PROPERTIES}, "required": ["query"]}},
]


def scope_from(arguments: Dict[str, Any]) -> SearchScope:
    return SearchScope.from_values(kinds=arguments.get("kinds"), project=arguments.get("project"), module=arguments.get("module"), path=arguments.get("path"), file=arguments.get("file"), owner=arguments.get("owner"), top_level=bool(arguments.get("top_level", False)))


def result_text(value: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}], "structuredContent": value}


class MetadataGraphMcp:
    def __init__(self, db: Path, workspace: Path = Path(".")):
        self.db = db
        self.workspace = workspace.resolve()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        scope = scope_from(arguments)
        limit = min(int(arguments.get("limit", 50)), 500)
        with connect(self.db, read_only=True) as conn:
            if name == "search_metadata":
                rows = search_nodes(conn, str(arguments.get("query", "")), limit, scope)
                return result_text({"query": arguments.get("query", ""), "scope": scope.__dict__, "count": len(rows), "results": rows})
            if name == "find_entity":
                rows = find_nodes(conn, str(arguments.get("query", "")), scope)[:limit]
                return result_text({"query": arguments.get("query", ""), "scope": scope.__dict__, "count": len(rows), "results": rows})
            if name in {"find_references", "get_dependencies"}:
                nodes = find_nodes(conn, str(arguments.get("query", "")), scope)
                if len(nodes) != 1:
                    return result_text({"query": arguments.get("query", ""), "error": "not_found_or_ambiguous", "candidates": nodes})
                direction = arguments.get("direction", "out" if name == "get_dependencies" else "both")
                depth = min(int(arguments.get("depth", 3 if name == "get_dependencies" else 1)), 10)
                return result_text(references(conn, nodes[0]["stable_id"], direction, depth))
            if name == "get_impact":
                return result_text(build_impact_report(conn, str(arguments.get("query", "")), min(int(arguments.get("depth", 3)), 10)))
            if name == "find_unresolved_references":
                clauses = ["e.to_node_id is null", "e.raw_target is not null"]; params: List[Any] = []
                if arguments.get("relation_type"):
                    clauses.append("e.relation_kind=?"); params.append(arguments["relation_type"])
                rows = conn.execute(EDGE_SELECT + " where " + " and ".join(clauses) + " order by e.id limit ?", [*params, limit]).fetchall()
                return result_text({"count": len(rows), "results": [dict(row) for row in rows]})
            if name == "get_entity_source":
                nodes = find_nodes(conn, str(arguments.get("query", "")), scope)
                if len(nodes) != 1:
                    return result_text({"query": arguments.get("query", ""), "error": "not_found_or_ambiguous", "candidates": nodes})
                node = nodes[0]; source = Path(node["file_path"])
                if not source.is_absolute(): source = self.workspace / source
                max_chars = min(int(arguments.get("max_chars", 50000)), 200000)
                raw = source.read_text(encoding="utf-8", errors="replace")[:max_chars] if source.is_file() else ""
                return result_text({"entity": node, "source_path": str(source), "content_truncated": len(raw) >= max_chars, "xml": raw})
        raise ValueError(f"unknown tool: {name}")

    def dispatch(self, request: Dict[str, Any]) -> Dict[str, Any] | None:
        method = request.get("method"); request_id = request.get("id")
        if method == "notifications/initialized": return None
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
        if method == "tools/list": return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            try:
                params = request.get("params") or {}; return {"jsonrpc": "2.0", "id": request_id, "result": self.call_tool(params.get("name", ""), params.get("arguments") or {})}
            except Exception as exc: return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
        if request_id is not None: return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return None


def serve_stdio(db: Path, workspace: Path = Path(".")) -> int:
    server = MetadataGraphMcp(db, workspace)
    for line in sys.stdin:
        if not line.strip(): continue
        try:
            response = server.dispatch(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"); sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}, ensure_ascii=False) + "\n"); sys.stdout.flush()
    return 0
