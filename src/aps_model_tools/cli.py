from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bridge import build_bridge_report
from .classification import audit_capabilities, render_markdown
from .ddl import generate_table_ddl
from .impact import build_impact_report
from .scanner import scan_workspace, sync_workspace, workspace_status
from .store import connect, find_nodes, get_stats, references


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _resolve_one(conn, query: str) -> Dict[str, Any]:
    nodes = find_nodes(conn, query)
    if not nodes:
        raise ValueError(f"model not found: {query}")
    if len(nodes) > 1:
        _json({"error": "ambiguous", "query": query, "candidates": nodes})
        raise SystemExit(2)
    return nodes[0]


def _positive_depth(value: str) -> int:
    depth = int(value)
    if depth < 1:
        raise argparse.ArgumentTypeError("depth must be >= 1")
    return depth


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aps-model", description="Read-only APS metadata model tools")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--workspace", required=True, type=Path)
    scan.add_argument("--db", required=True, type=Path)
    scan.add_argument("--fail-on-parse-error", action="store_true")

    sync = sub.add_parser("sync")
    sync.add_argument("--workspace", required=True, type=Path)
    sync.add_argument("--db", required=True, type=Path)
    sync.add_argument("--fail-on-parse-error", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--workspace", required=True, type=Path)
    status.add_argument("--db", required=True, type=Path)

    stats = sub.add_parser("stats")
    stats.add_argument("--db", required=True, type=Path)

    show = sub.add_parser("show")
    show.add_argument("query")
    show.add_argument("--db", required=True, type=Path)

    refs = sub.add_parser("refs")
    refs.add_argument("query")
    refs.add_argument("--db", required=True, type=Path)
    refs.add_argument("--direction", choices=["in", "out", "both"], default="both")
    refs.add_argument("--depth", type=_positive_depth, default=1)

    impact = sub.add_parser("impact")
    impact.add_argument("query")
    impact.add_argument("--db", required=True, type=Path)
    impact.add_argument("--depth", type=_positive_depth, default=3)

    ddl = sub.add_parser("ddl")
    ddl.add_argument("query")
    ddl.add_argument("--db", required=True, type=Path)
    ddl.add_argument("--dialect", default="mysql")
    ddl.add_argument("--output", type=Path)

    bridge = sub.add_parser("bridge", help="map APS models to generated Java and CodeGraph consumers")
    bridge.add_argument("query")
    bridge.add_argument("--db", required=True, type=Path)
    bridge.add_argument("--workspace", required=True, type=Path)
    bridge.add_argument("--codegraph", action="append", default=[], metavar="REPO=DB",
                        help="repository name and read-only CodeGraph database; repeatable")
    bridge.add_argument("--output", type=Path)

    classify = sub.add_parser("classify", help="audit APS models and Java packages by functional capability")
    classify.add_argument("--db", required=True, type=Path)
    classify.add_argument("--workspace", required=True, type=Path)
    classify.add_argument("--output", required=True, type=Path)
    classify.add_argument("--json-output", type=Path)
    return parser


def _parse_codegraph_databases(values: List[str]) -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    for value in values:
        repository, separator, database = value.partition("=")
        if not separator or not repository.strip() or not database.strip():
            raise ValueError("--codegraph must use REPO=DB")
        if repository in result:
            raise ValueError(f"duplicate CodeGraph repository: {repository}")
        result[repository] = Path(database)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            summary = scan_workspace(args.workspace, args.db, args.fail_on_parse_error)
            _json(asdict(summary))
            return 0
        if args.command == "sync":
            summary = sync_workspace(args.workspace, args.db, args.fail_on_parse_error)
            _json(asdict(summary))
            return 0
        if args.command == "status":
            _json(asdict(workspace_status(args.workspace, args.db)))
            return 0
        if args.command == "bridge":
            result = build_bridge_report(args.db, args.workspace, args.query,
                                         _parse_codegraph_databases(args.codegraph))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                _json({"output": str(args.output), "summary": {
                    "generated_links": len(result["generated_links"]),
                    "code_consumers": len(result["code_consumers"]),
                }})
            else:
                _json(result)
            return 0
        if args.command == "classify":
            result = audit_capabilities(args.db, args.workspace)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_markdown(result), encoding="utf-8")
            if args.json_output:
                args.json_output.parent.mkdir(parents=True, exist_ok=True)
                args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _json({"output": str(args.output), "json_output": str(args.json_output) if args.json_output else None,
                   "summary": result["summary"]})
            return 0
        conn = connect(args.db, read_only=True)
        try:
            if args.command == "stats":
                _json(get_stats(conn))
            elif args.command == "show":
                node = _resolve_one(conn, args.query)
                node["relations"] = references(conn, node["stable_id"], "both", 1)
                _json(node)
            elif args.command == "refs":
                node = _resolve_one(conn, args.query)
                _json(references(conn, node["stable_id"], args.direction, args.depth))
            elif args.command == "impact":
                _json(build_impact_report(conn, args.query, args.depth))
            elif args.command == "ddl":
                result = generate_table_ddl(conn, args.query, args.dialect)
                if result.errors:
                    _json(asdict(result))
                    return 2
                text = result.sql
                if result.warnings:
                    text += "\n-- warnings:\n" + "\n".join("-- " + x for x in result.warnings)
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(text + "\n", encoding="utf-8")
                    _json({"output": str(args.output), "warnings": result.warnings, "evidence": result.evidence})
                else:
                    print(text)
            return 0
        finally:
            conn.close()
    except (ValueError, OSError) as exc:
        _json({"error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
