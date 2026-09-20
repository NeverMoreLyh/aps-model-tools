from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .bridge import build_bridge_report
from .classification import audit_capabilities, render_markdown
from .ddl import generate_table_ddl
from .ddlgen import DdlGenConfig, generate_all_ddl
from .docx import DocExportReport, export_document
from .impact import build_impact_report
from .mcp_server import serve_stdio
from .registry import find_entry, registered_names, remove_entry, upsert_workspace, workspace_overview
from .scanner import (
    scan_workspace,
    scan_workspace_with_external_indexes,
    sync_workspace,
    workspace_status,
)
from .search_scope import SearchScope
from .store import connect, find_nodes, get_stats, references, search_nodes
from .workbench import close_workbenches, list_workbenches, serve_workbench
from .workspace_maintenance import (
    check_workspaces, rebuild_workspaces, select_targets,
    status_workspaces, sync_workspaces, vacuum_workspaces,
)
from .xlsx_export import ExcelExportReport, export_excel


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


def _positive_limit(value: str) -> int:
    limit = int(value)
    if limit < 1:
        raise argparse.ArgumentTypeError("limit must be >= 1")
    return limit


def _port_number(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 0-65535 (0 = pick a random free port)")
    return port


def _progress(message: str) -> None:
    print(f"[apsgraph] {message}", file=sys.stderr, flush=True)


_PROGRESS_BAR_RE = re.compile(r"(scan|sync): parsing (\d+)/(\d+)")


def _progress_bar(message: str) -> None:
    """Compact stderr progress: a single-line bar for per-file parsing,
    plain lines for other stages."""
    match = _PROGRESS_BAR_RE.search(message)
    if match:
        stage, done, total = match.group(1), int(match.group(2)), int(match.group(3))
        width = 24
        filled = int(width * done / total) if total else 0
        bar = "█" * filled + "░" * (width - filled)
        path = message.rsplit(" ", 1)[-1] if " " in message else ""
        sys.stderr.write(
            f"\r[apsgraph] {stage} |{bar}| {done}/{total} "
            f"({done * 100 // total}%) {path[:60]}\x1b[K")
        sys.stderr.flush()
        return
    sys.stderr.write("\n[apsgraph] " + message + "\n")


def _progress_bar_end() -> None:
    sys.stderr.write("\n")
    sys.stderr.flush()


def _print_scan_summary(summary: Dict[str, Any], elapsed: float) -> None:
    warnings = len(summary.get("unresolved_models") or [])
    sys.stderr.write(
        f"[apsgraph] scan 完成：{summary['parsed_files']}/{summary['discovered_files']} "
        f"文件已解析，{summary['failed_files']} 失败；节点 {summary['nodes']}，边 {summary['edges']}，"
        f"未解析引用 {summary['unresolved']}（警告 {warnings}）；耗时 {elapsed:.1f}s\n")


DEFAULT_DB = Path(".apsgraph/apsgraph.db")
DEFAULT_WORKSPACE = Path(".")

DEFAULT_OPTIONS = {
    "workspace": str(DEFAULT_WORKSPACE),
    "database": str(DEFAULT_DB),
    "external_indexes": [],
}


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", action="append", default=[], metavar="KIND",
                        help="metadata node kind; repeatable")
    parser.add_argument("--project", help="project path segment")
    parser.add_argument("--module", help="module path segment")
    parser.add_argument("--path", dest="path_glob", help="model file path glob")
    parser.add_argument("--file", help="relative path or basename")
    parser.add_argument("--owner", help="owner stable_id/full_id/raw_id")
    parser.add_argument("--top-level", action="store_true", help="only nodes with no owner")


def _scope(args: argparse.Namespace) -> SearchScope:
    return SearchScope.from_values(
        kinds=getattr(args, "kind", []), project=getattr(args, "project", None),
        module=getattr(args, "module", None), path=getattr(args, "path_glob", None),
        file=getattr(args, "file", None), owner=getattr(args, "owner", None),
        top_level=getattr(args, "top_level", False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apsgraph",
        description="Read-only APS metadata graph tools",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="workspace root (default: current directory)")
    scan.add_argument("--db", type=Path, default=DEFAULT_DB,
                      help="SQLite index path (default: .apsgraph/apsgraph.db)")
    scan.add_argument("--fail-on-parse-error", action="store_true")
    scan.add_argument("--external-db", action="append", type=Path, default=[], metavar="DB",
                      help="merge an APS SQLite index produced from another XML scan; repeatable")
    scan.add_argument("--show-warning", action="store_true",
                      help="list unresolved model reference warnings on stderr after the scan summary")
    scan.add_argument("--no-register", action="store_true",
                      help="do not record this workspace in the global registry "
                           "(~/.apsgraph/registry.json)")

    options = sub.add_parser(
        "options",
        help="show the current APSGraph version, default options, and effective workspace rules",
    )
    options.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                         help="workspace used to resolve effective rules (default: current directory)")

    sync = sub.add_parser("sync")
    sync.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                       help="workspace root (default: current directory)")
    sync.add_argument("--db", type=Path, default=DEFAULT_DB,
                      help="SQLite index path (default: .apsgraph/apsgraph.db)")
    sync.add_argument("--fail-on-parse-error", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                         help="workspace root (default: current directory)")
    status.add_argument("--db", type=Path, default=DEFAULT_DB,
                       help="SQLite index path (default: .apsgraph/apsgraph.db)")

    stats = sub.add_parser("stats")
    stats.add_argument("--db", type=Path, default=DEFAULT_DB)

    show = sub.add_parser("show")
    show.add_argument("query")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)

    search = sub.add_parser("search", help="fuzzy-search model IDs and descriptive XML attributes")
    search.add_argument("query")
    search.add_argument("--db", type=Path, default=DEFAULT_DB)
    search.add_argument("--limit", type=_positive_limit, default=50)
    _add_scope_arguments(search)

    find = sub.add_parser("find", help="exact-search stable_id, full_id, or raw_id with optional scope")
    find.add_argument("query")
    find.add_argument("--db", type=Path, default=DEFAULT_DB)
    find.add_argument("--limit", type=_positive_limit, default=50)
    _add_scope_arguments(find)

    refs = sub.add_parser("refs")
    refs.add_argument("query")
    refs.add_argument("--db", type=Path, default=DEFAULT_DB)
    refs.add_argument("--direction", choices=["in", "out", "both"], default="both")
    refs.add_argument("--depth", type=_positive_depth, default=1)

    impact = sub.add_parser("impact")
    impact.add_argument("query")
    impact.add_argument("--db", type=Path, default=DEFAULT_DB)
    impact.add_argument("--depth", type=_positive_depth, default=3)

    ddl = sub.add_parser("ddl")
    ddl.add_argument("query")
    ddl.add_argument("--db", type=Path, default=DEFAULT_DB)
    ddl.add_argument("--dialect", default="mysql")
    ddl.add_argument("--output", type=Path)

    ddlgen = sub.add_parser("ddl-gen", help="generate MySQL/Oracle/PostgreSQL DDL for all (or selected) tables from the SQLite model index")
    ddlgen.add_argument("--db", type=Path, default=DEFAULT_DB)
    ddlgen.add_argument("--dialect", choices=["mysql", "oracle", "postgresql"], default="mysql")
    ddlgen.add_argument("--tables", nargs="*", default=[], metavar="QUERY",
                        help="optional table queries; default: all TABLE nodes")
    ddlgen.add_argument("--output", type=Path, help="write SQL script to file")
    ddlgen.add_argument("--report", type=Path, help="write JSON report (warnings/errors/stats)")
    ddlgen.add_argument("--text-threshold", type=int, default=1000,
                        help="varchar(n) with n>=threshold becomes text/clob (default 1000)")
    ddlgen.add_argument("--db-ratio", type=float, default=1.0,
                        help="length multiplier for byCharacter=true types (default 1.0)")
    ddlgen.add_argument("--auto-increment", action="store_true",
                        help="MySQL only: prepend surrogate id bigint AUTO_INCREMENT column")
    ddlgen.add_argument("--username", default="", help="Oracle: emit public synonyms under this schema")
    ddlgen.add_argument("--charset", default="utf8mb4", help="MySQL table charset (default utf8mb4)")
    ddlgen.add_argument("--table-space", default="", help="Oracle tablespace clause")
    ddlgen.add_argument("--index-space", default="", help="Oracle index tablespace clause")

    dbdiff = sub.add_parser("db-diff",
                            help="compare APS metadata model against a live database schema, table-granular ERROR/WARNING report")
    dbdiff.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite metadata index")
    dbdiff.add_argument("--dialect", choices=["mysql", "oracle"], default="mysql",
                        help="target database dialect (postgresql pending psycopg driver)")
    dbdiff.add_argument("--dsn", default="",
                        help="live database DSN, e.g. mysql://user:***@host:port/db; "
                             "mutually exclusive with --actual-json")
    dbdiff.add_argument("--dsn-env", default="",
                        help="environment variable holding the DSN (avoids secrets on the command line)")
    dbdiff.add_argument("--actual-json", type=Path, default=None,
                        help="offline mode: read actual schema from a JSON export instead of a live connection")
    dbdiff.add_argument("--dump-actual", type=Path, default=None,
                        help="after reading the live database, dump the actual schema JSON to this path")
    dbdiff.add_argument("--tables", nargs="*", default=[], metavar="QUERY",
                        help="restrict the model side to these table queries")
    dbdiff.add_argument("--strict-extra-tables", action="store_true",
                        help="treat tables that exist in the database but not in the model as ERROR instead of WARNING")
    dbdiff.add_argument("--text-threshold", type=int, default=1000)
    dbdiff.add_argument("--db-ratio", type=float, default=1.0)
    dbdiff.add_argument("--output-json", type=Path, default=None)
    dbdiff.add_argument("--output-md", type=Path, default=None)

    bridge = sub.add_parser("bridge", help="map APS models to generated Java and CodeGraph consumers")
    bridge.add_argument("query")
    bridge.add_argument("--db", type=Path, default=DEFAULT_DB)
    bridge.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                                help="workspace root (default: current directory)")
    bridge.add_argument("--codegraph", action="append", default=[], metavar="REPO=DB",
                        help="repository name and read-only CodeGraph database; repeatable")
    bridge.add_argument("--output", type=Path)

    classify = sub.add_parser("classify", help="audit APS models and Java packages by functional capability")
    classify.add_argument("--db", type=Path, default=DEFAULT_DB)
    classify.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                             help="workspace root (default: current directory)")
    classify.add_argument("--output", required=True, type=Path)
    classify.add_argument("--json-output", type=Path)

    docexport = sub.add_parser("doc-export",
                               help="export model documentation as Markdown from the SQLite index")
    docexport.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite metadata index")
    docexport.add_argument("--type", default="all",
                           choices=["all", "table", "dict", "schema", "trans", "nsql", "service"],
                           help="document type to export (default: all)")
    docexport.add_argument("--tables", nargs="*", default=[], metavar="QUERY",
                           help="optional model queries to filter; default: all")
    docexport.add_argument("--output", type=Path, help="write Markdown to file")

    xls = sub.add_parser("xlsx-export",
                         help="export model documentation as Excel (.xlsx) files, aggregated by project")
    xls.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite metadata index")
    xls.add_argument("--output-dir", required=True, type=Path, help="output directory for .xlsx files")
    xls.add_argument("--types", nargs="*", default=[],
                     help="document types to export (default: all). Choices: table, table_list, "
                          "dict, dict_ref, enum, trans, nsql, service, service_v2, params, "
                          "error_code, batch_tran")
    xls.add_argument("--projects", nargs="*", default=[],
                     help="filter to specific project names (e.g. ap-parent aggr-parent)")
    mcp = sub.add_parser("serve-mcp", help="serve Metadata Graph tools over stdio MCP")
    mcp.add_argument("--db", type=Path, default=None,
                     help="SQLite metadata index (default: <workspace>/.apsgraph/apsgraph.db; "
                          "workspace resolved from --workspace, MCP client roots, or cwd)")
    mcp.add_argument("--workspace", type=Path, default=None,
                     help="workspace root (default: MCP client root when supported, otherwise cwd)")
    workbench = sub.add_parser(
        "workbench",
        help="serve the read-only SQLite metadata query workbench at 127.0.0.1 and open it in a browser")
    workbench.add_argument("--db", type=Path, default=None,
                           help="SQLite metadata index; explicit --db serves that single index, "
                                "otherwise all workspaces registered in ~/.apsgraph/registry.json")
    workbench.add_argument("--workspace", default=None, metavar="NAME|PATH",
                           help="registered workspace name or path to open on start; "
                                "the UI can still switch between all registered workspaces")
    workbench.add_argument("--port", type=_port_number, default=0,
                           help="local port to bind on 127.0.0.1; defaults to a "
                                "random free port (0)")
    workbench.add_argument("--no-browser", action="store_true",
                           help="do not open the default browser automatically")
    workbench_subs = workbench.add_subparsers(dest="wb_command")
    workbench_subs.add_parser(
        "list", help="list running workbench instances (port, PID, workspace, index)")
    wb_close = workbench_subs.add_parser(
        "close", help="stop a running workbench instance recorded in the instance registry")
    wb_close.add_argument("--port", type=_port_number, dest="close_port", metavar="PORT",
                          help="port of the instance to stop")
    wb_close.add_argument("--all", action="store_true", dest="close_all",
                          help="stop every running instance")

    ws_cmd = sub.add_parser(
        "workspace",
        help="maintenance commands over the global workspace registry (~/.apsgraph/registry.json)")
    ws_subs = ws_cmd.add_subparsers(dest="ws_command", required=True)

    def _add_json_flag(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--json", action="store_true",
                            help="emit machine-readable JSON instead of a human-readable table")

    ws_list = ws_subs.add_parser(
        "list", help="list registered workspaces with availability and timestamps")
    _add_json_flag(ws_list)

    ws_remove = ws_subs.add_parser(
        "remove", help="remove a workspace entry from the registry (indexes stay untouched)")
    ws_remove.add_argument("--workspace", required=True, metavar="NAME|PATH",
                           help="registered workspace id, name, or path")
    ws_remove.add_argument("--purge", action="store_true",
                           help="also delete the workspace's .apsgraph/ cache directory")
    _add_json_flag(ws_remove)

    def _add_ws_target(parser: argparse.ArgumentParser, action: str) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--workspace", metavar="ID|NAME|PATH",
                           help=f"single registered workspace to {action} (id, name, or path)")
        group.add_argument("--all", action="store_true",
                           help=f"{action} every registered workspace")

    ws_status = ws_subs.add_parser(
        "status", help="per-workspace index freshness: missing / stale / fresh")
    _add_ws_target(ws_status, "report")
    _add_json_flag(ws_status)
    ws_sync = ws_subs.add_parser("sync", help="incremental sync of registered workspace indexes")
    _add_ws_target(ws_sync, "sync")
    ws_sync.add_argument("--fail-on-parse-error", action="store_true")
    _add_json_flag(ws_sync)
    ws_check = ws_subs.add_parser(
        "check", help="index health: APS schema version and SQLite integrity_check")
    _add_ws_target(ws_check, "check")
    _add_json_flag(ws_check)
    ws_rebuild = ws_subs.add_parser(
        "rebuild", help="full rebuild of registered workspace indexes (staging + atomic replace)")
    _add_ws_target(ws_rebuild, "rebuild")
    ws_rebuild.add_argument("--fail-on-parse-error", action="store_true")
    _add_json_flag(ws_rebuild)
    ws_vacuum = ws_subs.add_parser("vacuum", help="in-place VACUUM of registered workspace indexes")
    _add_ws_target(ws_vacuum, "vacuum")
    _add_json_flag(ws_vacuum)
    return parser


def _external_indexes(args: argparse.Namespace) -> List[Path]:
    """Combine workspace external-index rules with command-line overrides."""
    configured: List[str] = []
    config_path = args.workspace / ".apsgraph.json"
    if config_path.is_file():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid {config_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{config_path} must contain a JSON object")
        value = payload.get("externalIndexes", [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(
                f"{config_path} field 'externalIndexes' must be an array of strings"
            )
        configured = value

    result = [args.workspace / item for item in configured]
    result.extend(args.external_db)
    return list(dict.fromkeys(result))


def _options_report(workspace: Path) -> Dict[str, Any]:
    """Return immutable CLI defaults plus rules effective for this workspace.

    This command is intentionally read-only: it never creates the workspace,
    cache, or index.  It gives scripts a stable way to inspect defaults because
    those defaults may change between APSGraph releases.
    """
    scan_args = build_parser().parse_args(["scan", "--workspace", str(workspace)])
    external_indexes = _external_indexes(scan_args)
    return {
        "version": __version__,
        "workspace": str(Path(workspace).resolve()),
        "defaults": DEFAULT_OPTIONS,
        "effective": {
            "external_indexes": [str(value.resolve()) for value in external_indexes],
        },
    }


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


def _registered_listing() -> str:
    return registered_names(workspace_overview())


def _display_width(text: str) -> int:
    """Terminal cell width; CJK fullwidth characters count as two cells so
    tables with Chinese text still align."""
    return sum(2 if unicodedata.east_asian_width(char) in "FW" else 1
               for char in text)


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _display_width(str(text)))


def _truncate(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if _display_width(flat) <= limit else flat[:limit - 1] + "…"


def _render_table(headers: List[str], rows: List[List[Any]]) -> str:
    """Aligned plain-text table using only the standard library."""
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(str(cell)))
    def fmt(row: List[Any]) -> str:
        return "  ".join(_pad(cell, widths[index])
                         for index, cell in enumerate(row)).rstrip()
    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


_WS_BATCH_COLUMNS = {
    "status": ["ID", "NAME", "STATE", "ADD", "MOD", "DEL", "NOTE"],
    "sync": ["ID", "NAME", "STATE", "ADD", "MOD", "DEL", "NODES", "EDGES", "NOTE"],
    "check": ["ID", "NAME", "STATE", "SCHEMA", "INTEGRITY", "NOTE"],
    "rebuild": ["ID", "NAME", "STATE", "NODES", "EDGES", "NOTE"],
    "vacuum": ["ID", "NAME", "STATE", "SIZE BEFORE", "SIZE AFTER", "NOTE"],
}
_NOTE_LIMIT = 70


def _ws_row(action: str, item: Dict[str, Any]) -> List[Any]:
    """Human cells for one result row; empty values render as —, the NOTE
    column carries missing details and truncated error messages."""
    note = item.get("detail") or item.get("error") or ""
    state = item.get("state", "?")
    if action == "status":
        return [item["id"], item["name"], state,
                item.get("added", "—"), item.get("modified", "—"),
                item.get("deleted", "—"), _truncate(note, _NOTE_LIMIT)]
    if action in ("sync", "rebuild"):
        base = [item["id"], item["name"], state, item.get("added", "—"),
                item.get("modified", "—"), item.get("deleted", "—")]
        if action == "rebuild":
            base = [item["id"], item["name"], state,
                    item.get("nodes", "—"), item.get("edges", "—")]
        else:
            base += [item.get("nodes", "—"), item.get("edges", "—")]
        return base + [_truncate(note, _NOTE_LIMIT)]
    if action == "check":
        return [item["id"], item["name"], state, item.get("schema_version", "—"),
                item.get("integrity", "—"), _truncate(note, _NOTE_LIMIT)]
    if action == "vacuum":
        return [item["id"], item["name"], state, item.get("size_before", "—"),
                item.get("size_after", "—"), _truncate(note, _NOTE_LIMIT)]
    return [item["id"], item["name"], state, _truncate(note, _NOTE_LIMIT)]


def _render_workspace_results(action: str, results: List[Dict[str, Any]]) -> str:
    headers = _WS_BATCH_COLUMNS[action]
    rows = [_ws_row(action, item) for item in results]
    return _render_table(headers, rows)


def _run_workspace_command(args: argparse.Namespace) -> int:
    """Dispatch the `apsgraph workspace` maintenance family.

    `list`/`remove` are registry-level; the batch actions resolve targets
    through the registry and report fail-soft per-workspace results.  Target
    resolution errors land in main()'s error handling; per-workspace failures
    are recorded in the JSON payload and only mark the exit code.
    """
    if args.ws_command == "list":
        payload = {"workspaces": workspace_overview()}
        if args.json:
            _json(payload)
        else:
            rows = [[item["id"], item["name"],
                     "ok" if item["available"] else "MISSING",
                     item.get("lastScanAt") or "—", item.get("lastUsedAt") or "—",
                     item["workspacePath"]]
                    for item in payload["workspaces"]]
            print(_render_table(
                ["ID", "NAME", "STATE", "LAST SCAN", "LAST USED", "PATH"], rows))
        return 0
    if args.ws_command == "remove":
        entry = find_entry(args.workspace)
        if entry is None:
            raise ValueError(f"unknown workspace: {args.workspace}; "
                             f"registered workspaces: {_registered_listing()}")
        purged = False
        if args.purge:
            cache = Path(entry["workspacePath"]) / ".apsgraph"
            if cache.is_dir():
                shutil.rmtree(cache)
                purged = True
        removed = remove_entry(args.workspace)
        payload = {"removed": removed, "purged": purged}
        if args.json:
            _json(payload)
        else:
            line = f"removed {removed['name']} (id {removed['id']})"
            if purged:
                line += "; .apsgraph/ cache directory purged"
            print(line)
        return 0

    entries = select_targets(args.workspace, args.all)
    if args.ws_command == "status":
        results, ok = status_workspaces(entries)
    elif args.ws_command == "sync":
        results, ok = sync_workspaces(entries, args.fail_on_parse_error, _progress_bar)
        _progress_bar_end()
    elif args.ws_command == "check":
        results, ok = check_workspaces(entries)
    elif args.ws_command == "rebuild":
        results, ok = rebuild_workspaces(entries, args.fail_on_parse_error, _progress_bar)
        _progress_bar_end()
    elif args.ws_command == "vacuum":
        results, ok = vacuum_workspaces(entries)
    else:  # pragma: no cover - argparse enforces the choice
        raise ValueError(f"unknown workspace command: {args.ws_command}")
    payload = {"results": results, "ok": ok}
    if args.json:
        _json(payload)
    else:
        print(_render_workspace_results(args.ws_command, results))
        failures = sum(1 for item in results if item.get("state") == "error")
        summary = f"[apsgraph] {len(results)} 个 workspace，{failures} 个失败" if failures \
            else f"[apsgraph] {len(results)} 个 workspace 全部成功"
        print(summary)
    return 0 if ok else 2


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            started = time.time()
            external_indexes = _external_indexes(args)
            if external_indexes:
                summary, external = scan_workspace_with_external_indexes(
                    args.workspace, args.db, external_indexes, args.fail_on_parse_error, _progress_bar
                )
            else:
                summary = scan_workspace(args.workspace, args.db, args.fail_on_parse_error, _progress_bar)
            _progress_bar_end()
            _print_scan_summary(asdict(summary), time.time() - started)
            if args.show_warning:
                for target in summary.unresolved_models:
                    print(f"[apsgraph] warning: unresolved model reference: {target}",
                          file=sys.stderr, flush=True)
            if not args.no_register:
                registered = upsert_workspace(args.workspace, args.db)
                _progress(f"workspace registered: {registered['name']} -> {registered['dbPath']}")
            return 0
        if args.command == "options":
            _json(_options_report(args.workspace))
            return 0
        if args.command == "sync":
            started = time.time()
            summary = sync_workspace(args.workspace, args.db, args.fail_on_parse_error, _progress_bar)
            _progress_bar_end()
            info = asdict(summary)
            sys.stderr.write(
                f"[apsgraph] sync 完成：新增 {info['added']}，修改 {info['modified']}，"
                f"删除 {info['deleted']}；节点 {info['nodes']}，边 {info['edges']}；"
                f"耗时 {time.time() - started:.1f}s\n")
            _json(asdict(summary))
            return 0
        if args.command == "serve-mcp":
            return serve_stdio(args.db, args.workspace)
        if args.command == "workbench":
            if args.wb_command == "list":
                _json(list_workbenches(progress=_progress))
                return 0
            if args.wb_command == "close":
                if args.close_port is None and not args.close_all:
                    _json({"error": "workbench close requires --port PORT or --all"})
                    return 2
                if args.close_port is not None and args.close_all:
                    _json({"error": "workbench close accepts either --port PORT or --all, not both"})
                    return 2
                result = close_workbenches(port=args.close_port,
                                           close_all=args.close_all, progress=_progress)
                _json(result)
                return 0 if not result["not_found"] and not result["failed"] else 2
            return serve_workbench(args.db, args.port,
                                   open_browser=not args.no_browser, progress=_progress,
                                   workspace=args.workspace)
        if args.command == "workspace":
            return _run_workspace_command(args)
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
        if args.command == "doc-export":
            conn = connect(args.db, read_only=True)
            try:
                markdown, report = export_document(conn, args.type, args.tables or None)
            finally:
                conn.close()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(markdown, encoding="utf-8")
                _json({"output": str(args.output),
                       "type": report.doc_type,
                       "tables": report.tables_exported,
                       "dicts": report.dicts_exported,
                       "schemas": report.schemas_exported,
                       "transactions": report.trans_exported,
                       "named_sqls": report.nsqls_exported,
                       "services": report.services_exported,
                       "warnings": len(report.warnings),
                       "errors": len(report.errors)})
            else:
                print(markdown)
            return 2 if report.errors else 0
        if args.command == "xlsx-export":
            conn = connect(args.db, read_only=True)
            try:
                doc_types = args.types or None
                project_filter = args.projects or None
                reports = export_excel(conn, str(args.output_dir), doc_types, project_filter)
            except ImportError as exc:
                _json({"error": str(exc)})
                return 2
            finally:
                conn.close()
            _json([{
                "project": r.project,
                "output_file": r.output_file,
                "sheets": r.sheets_generated,
                "tables": r.tables,
                "dicts": r.dicts,
                "enums": r.enums,
                "trans": r.trans,
                "nsqls": r.nsqls,
                "services": r.services,
                "params": r.params,
                "error_codes": r.error_codes,
                "batch_trans": r.batch_trans,
                "warnings": len(r.warnings),
                "errors": len(r.errors),
            } for r in reports])
            has_errors = any(r.errors for r in reports)
            return 2 if has_errors else 0
        if args.command == "ddl-gen":
            cfg = DdlGenConfig(
                dialect=args.dialect,
                text_threshold=args.text_threshold,
                db_ratio=args.db_ratio,
                auto_increment=args.auto_increment,
                username=args.username,
                charset=args.charset,
                table_space=args.table_space,
                index_space=args.index_space,
            )
            conn = connect(args.db, read_only=True)
            try:
                report = generate_all_ddl(conn, cfg, args.tables or None)
            finally:
                conn.close()
            summary = {
                "dialect": report.dialect,
                "tables_generated": report.tables_generated,
                "tables_skipped_abstract": report.tables_skipped_abstract,
                "warnings": len(report.warnings),
                "errors": len(report.errors),
            }
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(report.sql, encoding="utf-8")
                summary["output"] = str(args.output)
            else:
                print(report.sql)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(
                    {"summary": summary, "warnings": report.warnings, "errors": report.errors},
                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                summary["report"] = str(args.report)
            _json(summary)
            return 2 if report.errors else 0
        if args.command == "db-diff":
            from datetime import datetime, timezone, timedelta

            from .dbdiff import (ACTUAL_FETCHERS, expected_schema_from_db, load_actual_json,
                                 dump_actual_json, redact_dsn,
                                 render_markdown as render_diff_markdown, run_diff)

            if args.actual_json:
                json_dialect, actual = load_actual_json(args.actual_json)
                dsn_display = f"offline:{args.actual_json}"
                dialect = json_dialect
                if args.dialect and args.dialect != json_dialect:
                    print(f"-- warning: --actual-json dialect is {json_dialect}, overriding --dialect {args.dialect}",
                          flush=True)
            else:
                dsn = args.dsn
                if args.dsn_env:
                    import os
                    dsn = os.environ.get(args.dsn_env, "")
                if not dsn:
                    _json({"error": "either --dsn/--dsn-env or --actual-json is required"})
                    return 2
                dialect = args.dialect
                if dialect not in ACTUAL_FETCHERS:
                    _json({"error": f"live connection for dialect '{dialect}' not implemented yet; use --actual-json"})
                    return 2
                actual = ACTUAL_FETCHERS[dialect](dsn)
                dsn_display = redact_dsn(dsn)
                if args.dump_actual:
                    dump_actual_json(actual, args.dump_actual, dialect)

            cfg = DdlGenConfig(dialect=dialect,
                               text_threshold=args.text_threshold, db_ratio=args.db_ratio)
            conn = connect(args.db, read_only=True)
            try:
                expected, ddl_errors = expected_schema_from_db(conn, dialect, cfg)
                if args.tables:
                    wanted = set()
                    for q in args.tables:
                        for n in find_nodes(conn, q):
                            if n["kind"] == "TABLE":
                                props = n.get("properties") or {}
                                phys = props.get("name") or n["id"]
                                wanted.add(str(phys).lower())
                    expected = {k: v for k, v in expected.items() if k in wanted}
            finally:
                conn.close()

            report = run_diff(expected, actual, strict_extra_tables=args.strict_extra_tables)
            tz = timezone(timedelta(hours=8))
            meta = {"dialect": dialect, "dsn_redacted": dsn_display,
                    "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")}

            payload = {
                "meta": meta,
                "summary": report["summary"],
                "model_generation_errors": ddl_errors,
                "tables": [
                    {"table": r.table, "status": r.status,
                     "errors": [{"kind": i.kind, "object": i.object, "detail": i.detail} for i in r.errors],
                     "warnings": [{"kind": i.kind, "object": i.object, "detail": i.detail} for i in r.warnings]}
                    for r in report["tables"]
                ],
            }
            if args.output_json:
                args.output_json.parent.mkdir(parents=True, exist_ok=True)
                args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
            if args.output_md:
                args.output_md.parent.mkdir(parents=True, exist_ok=True)
                args.output_md.write_text(render_diff_markdown(report, meta), encoding="utf-8")
            _json({"meta": meta, "summary": report["summary"],
                   "model_generation_errors": len(ddl_errors),
                   "output_json": str(args.output_json) if args.output_json else None,
                   "output_md": str(args.output_md) if args.output_md else None})
            return 1 if report["summary"]["total_errors"] else 0
        conn = connect(args.db, read_only=True)
        try:
            if args.command == "stats":
                _json(get_stats(conn))
            elif args.command == "show":
                node = _resolve_one(conn, args.query)
                node["relations"] = references(conn, node["stable_id"], "both", 1)
                _json(node)
            elif args.command == "search":
                scope = _scope(args)
                results = search_nodes(conn, args.query, args.limit, scope)
                _json({"query": args.query, "scope": asdict(scope), "count": len(results), "results": results})
            elif args.command == "find":
                scope = _scope(args)
                results = find_nodes(conn, args.query, scope)[:args.limit]
                _json({"query": args.query, "scope": asdict(scope), "count": len(results), "results": results})
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
