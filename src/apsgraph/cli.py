from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from dataclasses import replace as replace_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import __version__
from .bridge import build_bridge_report
from .classification import audit_capabilities, render_markdown
from .ddl import generate_table_ddl
from .ddlgen import DdlGenConfig, generate_all_ddl
from .docx import DocExportReport, export_document
from .impact import build_impact_report
from .maven import (
    DEFAULT_EXCLUDED_PROJECTS,
    MavenJdkConfig,
    import_maven_dependencies,
    load_maven_jdk_config,
    scan_workspace_with_dependencies,
)
from .scanner import import_jar_models, scan_workspace, sync_workspace, workspace_status
from .store import connect, find_nodes, get_stats, references
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


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _positive_depth(value: str) -> int:
    depth = int(value)
    if depth < 1:
        raise argparse.ArgumentTypeError("depth must be >= 1")
    return depth


DEFAULT_DB = Path(".apsgraph/apsgraph.db")
DEFAULT_WORKSPACE = Path(".")
DEFAULT_CACHE_DIR = Path(".apsgraph")
DEFAULT_MAVEN_JOBS = 4

DEFAULT_OPTIONS = {
    "workspace": str(DEFAULT_WORKSPACE),
    "database": str(DEFAULT_DB),
    "cache_dir": str(DEFAULT_CACHE_DIR),
    "maven": {
        "goal": ["install"],
        "skip_tests": True,
        "dependency_scope": "runtime",
        "executable": "mvn",
        "jobs": DEFAULT_MAVEN_JOBS,
        "default_excluded_projects": list(DEFAULT_EXCLUDED_PROJECTS),
    },
}


def _project_excludes(args: argparse.Namespace) -> List[str]:
    """Combine workspace rules, built-in defaults, and command-line overrides.

    The workspace file is ``.apsgraph.json``.  It may use either form:

    ``{"excludeProjects": ["foo"]}``

    or::

        {"maven": {"excludeProjects": ["foo"]}}

    Project rules always apply.  ``--no-default-project-excludes`` only
    disables APSGraph's built-in ``*dist`` rule, while repeatable
    ``--exclude-project`` values are appended last.
    """
    configured: List[str] = []
    config_path = args.workspace / ".apsgraph.json"
    if config_path.is_file():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid {config_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{config_path} must contain a JSON object")
        value = payload.get("excludeProjects")
        if value is None and isinstance(payload.get("maven"), dict):
            value = payload["maven"].get("excludeProjects")
        if value is None:
            value = []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(
                f"{config_path} field 'excludeProjects' must be an array of strings"
            )
        configured = value

    rules = list(configured)
    if not args.no_default_project_excludes:
        rules.extend(DEFAULT_EXCLUDED_PROJECTS)
    rules.extend(args.exclude_project)
    return list(dict.fromkeys(rules))


def _parse_pair(value: str, option: str) -> Tuple[str, str]:
    key, separator, configured = value.partition("=")
    key = key.strip()
    configured = configured.strip()
    if not separator or not key or not configured:
        raise ValueError(f"{option} must use KEY=VALUE")
    return key, configured


def _parse_pairs(values: List[str], option: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        key, configured = _parse_pair(value, option)
        if key in result and result[key] != configured:
            raise ValueError(f"duplicate {option} profile: {key}")
        result[key] = configured
    return result


def _add_maven_jdk_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--jdk", default=None, metavar="PROFILE",
                        help="JDK profile for all Maven execution (overrides workspace rules)")
    parser.add_argument("--project-jdk", action="append", default=[], metavar="PROJECT=PROFILE",
                        help="JDK profile for a Maven project/build-unit glob; repeatable")
    parser.add_argument("--java-home", action="append", default=[], metavar="PROFILE=PATH",
                        help="explicit JAVA_HOME for a JDK profile; repeatable")


def _jdk_options(args: argparse.Namespace) -> Tuple[MavenJdkConfig, Optional[str], Mapping[str, str]]:
    config = load_maven_jdk_config(args.workspace)
    homes = _parse_pairs(args.java_home, "--java-home")
    if homes:
        config = replace_dataclass(config, java_homes={**config.java_homes, **homes})
    return config, args.jdk, _parse_pairs(args.project_jdk, "--project-jdk")


def _progress(message: str) -> None:
    print(f"[apsgraph] {message}", file=sys.stderr, flush=True)


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
    scan.add_argument("--include-deps", action="store_true",
                      help="build Maven projects, copy resolved dependency jars, and include their model XML")
    scan.add_argument("--maven-goal", action="append", default=None, metavar="GOAL",
                      help="Maven goal (default: install); repeatable")
    scan.add_argument("--skip-tests", dest="skip_tests", action="store_true", default=True,
                      help="pass -DskipTests to Maven (default)")
    scan.add_argument("--no-skip-tests", dest="skip_tests", action="store_false",
                      help="run tests during Maven builds")
    scan.add_argument("--deps-scope", choices=["compile", "runtime", "test"], default="runtime",
                      help="dependency scope to copy and import (default: runtime)")
    scan.add_argument("--maven", default="mvn", help="Maven executable (default: mvn)")
    scan.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                      help="workspace-relative cache directory (default: .apsgraph)")
    scan.add_argument("--jobs", type=_positive_int, default=DEFAULT_MAVEN_JOBS, metavar="N",
                      help="parallel Maven jobs inside build/dependency phases (default: 4)")
    scan.add_argument("--exclude-project", action="append", default=[], metavar="PATTERN",
                      help="skip dependency analysis for a project glob; repeatable "
                           "(matched against artifactId/project path)")
    scan.add_argument("--no-default-project-excludes", action="store_true",
                      help="disable the default '*dist' dependency-analysis exclusion")
    _add_maven_jdk_arguments(scan)

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

    importdeps = sub.add_parser(
        "import-maven-deps",
        help="resolve Maven dependencies and replace JAR-imported models in an existing index",
    )
    importdeps.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                            help="workspace root (default: current directory)")
    importdeps.add_argument("--db", type=Path, default=DEFAULT_DB,
                            help="SQLite index path (default: .apsgraph/apsgraph.db)")
    importdeps.add_argument("--build", action="store_true",
                            help="build workspace projects before resolving dependencies")
    importdeps.add_argument("--maven-goal", action="append", default=None, metavar="GOAL",
                            help="Maven goal used with --build (default: install); repeatable")
    importdeps.add_argument("--skip-tests", dest="skip_tests", action="store_true", default=True,
                            help="pass -DskipTests to Maven (default)")
    importdeps.add_argument("--no-skip-tests", dest="skip_tests", action="store_false",
                            help="run tests during Maven builds")
    importdeps.add_argument("--deps-scope", choices=["compile", "runtime", "test"], default="runtime",
                            help="dependency scope to copy and import (default: runtime)")
    importdeps.add_argument("--maven", default="mvn", help="Maven executable (default: mvn)")
    importdeps.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                            help="workspace-relative cache directory (default: .apsgraph)")
    importdeps.add_argument("--jobs", type=_positive_int, default=DEFAULT_MAVEN_JOBS, metavar="N",
                            help="parallel Maven jobs inside build/dependency phases (default: 4)")
    importdeps.add_argument("--exclude-project", action="append", default=[], metavar="PATTERN",
                            help="skip dependency analysis for a project glob; repeatable "
                                 "(matched against artifactId/project path)")
    importdeps.add_argument("--no-default-project-excludes", action="store_true",
                            help="disable the default '*dist' dependency-analysis exclusion")
    _add_maven_jdk_arguments(importdeps)

    importjars = sub.add_parser("import-jars",
                                help="import APS model XML from Maven dependency jars (framework base models)")
    importjars.add_argument("--db", type=Path, default=DEFAULT_DB,
                                help="SQLite index path (default: .apsgraph/apsgraph.db)")
    importjars.add_argument("--jar", action="append", default=[], type=Path, metavar="JAR",
                            help="dependency jar to import; repeatable")
    importjars.add_argument("--no-reresolve", action="store_true",
                            help="skip re-resolving reference edges after import")

    stats = sub.add_parser("stats")
    stats.add_argument("--db", type=Path, default=DEFAULT_DB)

    show = sub.add_parser("show")
    show.add_argument("query")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)

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
    return parser


def _options_report(workspace: Path) -> Dict[str, Any]:
    """Return immutable CLI defaults plus rules effective for this workspace.

    This command is intentionally read-only: it never creates the workspace,
    cache, or index.  It gives scripts a stable way to inspect defaults because
    those defaults may change between APSGraph releases.
    """
    scan_args = build_parser().parse_args([
        "scan", "--include-deps", "--workspace", str(workspace)
    ])
    effective_excludes = _project_excludes(scan_args)
    jdk_config = load_maven_jdk_config(workspace)
    return {
        "version": __version__,
        "workspace": str(Path(workspace).resolve()),
        "defaults": DEFAULT_OPTIONS,
        "effective": {
            "exclude_projects": effective_excludes,
            "maven_jdk": {
                "default": jdk_config.default_profile,
                "java_homes": dict(jdk_config.java_homes),
                "rules": [asdict(rule) for rule in jdk_config.rules],
            },
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


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            if args.include_deps:
                jdk_config, jdk_profile, project_jdk = _jdk_options(args)
                result = scan_workspace_with_dependencies(
                    args.workspace,
                    args.db,
                    goals=args.maven_goal or ["install"],
                    skip_tests=args.skip_tests,
                    dependency_scope=args.deps_scope,
                    maven=args.maven,
                    cache_dir=args.cache_dir,
                    excluded_projects=_project_excludes(args),
                    fail_on_parse_error=args.fail_on_parse_error,
                    jdk_config=jdk_config,
                    jdk_profile=jdk_profile,
                    project_jdk=project_jdk,
                    progress=_progress,
                    jobs=args.jobs,
                )
                _json(asdict(result))
            else:
                summary = scan_workspace(args.workspace, args.db, args.fail_on_parse_error)
                _json(asdict(summary))
            return 0
        if args.command == "options":
            _json(_options_report(args.workspace))
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
        if args.command == "import-maven-deps":
            jdk_config, jdk_profile, project_jdk = _jdk_options(args)
            result = import_maven_dependencies(
                args.workspace,
                args.db,
                goals=args.maven_goal or ["install"],
                skip_tests=args.skip_tests,
                dependency_scope=args.deps_scope,
                build=args.build,
                maven=args.maven,
                cache_dir=args.cache_dir,
                excluded_projects=_project_excludes(args),
                jdk_config=jdk_config,
                jdk_profile=jdk_profile,
                project_jdk=project_jdk,
                progress=_progress,
                jobs=args.jobs,
            )
            _json(result)
            return 0
        if args.command == "import-jars":
            if not args.jar:
                _json({"error": "at least one --jar is required"})
                return 2
            _json(import_jar_models(args.db, args.jar, reresolve=not args.no_reresolve))
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
                    from .store import find_nodes
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
