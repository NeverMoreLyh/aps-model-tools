# APSGraph

English | [简体中文](README.zh-CN.md)

Read-only APS metadata analysis toolkit: scan APS XML models into a compact SQLite relationship index, then query, analyze, generate DDL, diff against live databases, export documentation, bridge to generated Java/CodeGraph, and browse everything in a local web workbench.

- **Read-only by design** — business repositories, CodeGraph databases, and generated DDL are never modified or executed.
- **Zero mandatory runtime dependencies** — the core CLI runs on Python 3.9+ with the standard library only.
- **Machine-friendly** — every command emits JSON on stdout; human-facing logs go to stderr.

## Features

| Area | Capabilities |
|---|---|
| Indexing | Full scan (`scan`) and incremental sync (`sync`) of APS XML models (tables, dictionaries, enums, services, transactions, batches, named SQL, sharding, error codes, constants, base types) into SQLite Schema V2 with semantic nodes, reference edges, and an FTS5 search index (raw XML is never stored; XML fragments are read on demand from source files) |
| Query & analysis | Exact find, fuzzy search (id/fullId/Chinese name/description), reference graphs (`refs`), reverse impact analysis (`impact`), workspace status and index statistics |
| DDL | Single-table DDL preview (`ddl`), full MySQL/Oracle/PostgreSQL/TDSQL/GoldenDB DDL generation (`ddl-gen`; distributed dialects support table shard type and RANGE daily partitioning in the workbench dialog), model-vs-live-database schema diff (`db-diff`, DSN or offline JSON) |
| Documentation | Markdown export (`doc-export`) and project-aggregated Excel export (`xlsx-export`, optional `openpyxl`) |
| Bridge & audit | Map APS models to generated Java symbols and CodeGraph consumers (`bridge`); heuristic functional-capability classification of models and Java packages (`classify`) |
| MCP | Expose the metadata graph as stdio MCP tools for AI clients (`serve-mcp`) |
| Workbench | Local read-only web UI over the index (`workbench`): dashboard, per-kind query pages, structured detail views, flow charts, DDL preview; multi-instance support with random ports and `list`/`close` commands |

## Architecture

```text
APS workspace (XML models)          read-only
        │
        ▼
┌───────────────────────┐   scan / sync   ┌─────────────────────────────┐
│  scanner              │ ──────────────▶ │  SQLite index (Schema V2)   │
│  discover → parse →   │                 │  model_files · nodes ·      │
│  resolve references   │                 │  edges · model_search(FTS5) │
└───────────────────────┘                 │  scan_state                 │
                                          └──────────────┬──────────────┘
        ┌────────────────────────────────────────────────┼──────────────┐
        ▼                ▼               ▼               ▼              ▼
   CLI commands      MCP (stdio)    Web workbench    DDL/db-diff     docs export
   search/find/refs  search, find,  127.0.0.1 only,  ddlgen rules    markdown /
   impact/bridge…    impact, source read-only, GET    + validation    xlsx
```

- **scanner** (`scanner.py`) — discovers recognized APS XML files, parses them into semantic nodes and relations, resolves cross-file references, and supports merging external APS indexes (`--external-db`). Writes are restricted to the dedicated index path.
- **store** (`store.py`) — read-only connection helpers and the Schema V2 data model: files, nodes (with stable ids), edges, FTS5 search, and scan state.
- **Query layer** (`search_scope.py`, `impact.py`) — scoped exact/fuzzy search and recursive reverse-impact traversal.
- **Generators/analyzers** (`ddlgen.py`, `dbdiff.py`, `bridge.py`, `classification.py`) — DDL rules per dialect, schema diffing with ERROR/WARNING verdicts, Java/CodeGraph bridging with explicit evidence, capability classification.
- **Surfaces** (`cli.py`, `mcp_server.py`, `workbench.py` + `workbench_static/`) — the CLI, a stdio MCP server, and a self-contained local web UI (vanilla JS, mermaid bundled for offline use).
- **Workbench instance registry** — each workbench records `{port, pid, url, db, workspace, started_at}` in `~/.apsgraph/workbench-registry.json` so instances from different folders can be listed and closed from anywhere (override with `APSGRAPH_WORKBENCH_REGISTRY`).

## Install

```bash
# Core CLI (standard library only)
pip install .

# Include Excel export
pip install ".[excel]"

# Development install with Excel export
pip install -e ".[excel]"
```

Optional dependencies: `sqlglot` (SQL validation for `db-diff` and workbench DDL preview), `openpyxl` (Excel export).

## Quick start

```bash
cd /path/to/aps-workspace      # APS model workspace root
apsgraph scan                  # build the index at .apsgraph/apsgraph.db
apsgraph sync                  # incrementally apply source changes
apsgraph status                # workspace/index drift report
apsgraph stats                 # index statistics (JSON)
apsgraph options               # version, defaults, effective workspace rules
```

## Command reference

| Command | Purpose |
|---|---|
| `options` | Show version, default options, and effective workspace rules |
| `scan` | Full (re)build of the SQLite index from workspace XML |
| `sync` | Incremental sync by relative path + SHA-256; rebinds references |
| `status` | Report added/modified/deleted models vs the index |
| `stats` | Index statistics as JSON |
| `show` / `find` / `search` | Exact node detail; exact id search; fuzzy FTS5 search |
| `refs` | Incoming/outgoing reference graph with depth |
| `impact` | Reverse dependency impact report |
| `ddl` | Single-table DDL preview (experimental subset, never executed) |
| `ddl-gen` | Generate MySQL/Oracle/PostgreSQL/TDSQL/GoldenDB DDL for all or selected tables |
| `db-diff` | Compare model schema against a live DB (DSN) or a JSON export |
| `bridge` | Map models to generated Java and CodeGraph consumers |
| `classify` | Heuristic capability audit of models and Java packages |
| `doc-export` | Export model documentation as Markdown |
| `xlsx-export` | Export model documentation as Excel files per project |
| `serve-mcp` | Serve the metadata graph as a stdio MCP server |
| `workbench` | Serve the read-only local web workbench and open a browser |
| `workbench list` | List running workbench instances (port, PID, workspace, index) |
| `workbench close` | Stop instances by `--port N` or `--all` |

## Workbench

```bash
apsgraph workbench                       # random free port on 127.0.0.1 + auto browser
apsgraph workbench --port 8321           # fixed port if you prefer a stable URL
apsgraph workbench --db /other/.apsgraph/apsgraph.db --no-browser
```

Because the server binds `127.0.0.1` only and opens the index read-only, you can safely keep one workbench per workspace. Each instance binds a random free port by default; the effective URL is printed to stderr and recorded in the registry, and an explicitly requested port that is unavailable fails with an error instead of being silently shared.

```bash
apsgraph workbench list                  # running instances, stale entries pruned
apsgraph workbench close --port 8321     # stop one instance
apsgraph workbench close --all           # stop every instance of this user
```

The UI provides a dashboard with per-page record counts, per-kind query pages (tables, services, transactions, batches, named SQL, dictionaries, enums, base types, error codes, constants, sharding, parse failures), structured detail views with input/output fields, common-field tables, flow-orchestration charts (offline mermaid), unresolved-reference highlighting, original XML fragments (read on demand from source files, never stored in the index), and per-table MySQL/Oracle/PostgreSQL DDL preview with optional sqlglot validation.

## Safety boundaries

- Business source repositories are only ever read; the index lives under the workspace's `.apsgraph/` directory (or an explicit `--db`).
- The workbench binds `127.0.0.1`, is GET-only, and opens SQLite in read-only mode; non-GET requests get `405`.
- Generated DDL is printed or written to files — never executed.
- CodeGraph databases are opened immutably; Maven builds and JDK selection are out of scope.
- Unresolved model references stay warnings; index replacement is fail-closed on parse errors (`--fail-on-parse-error`), legacy schemas, and foreign workspaces.

## Documentation map

| Document | Purpose |
|---|---|
| [Requirements](docs/requirements.md) | Long-lived requirement baseline |
| [Design](docs/design.md) | Architecture, key design, and performance |
| [Product whitepaper](docs/product-whitepaper.md) | Product positioning, architecture, and functional map |
| [Usage guide](docs/usage-guide.md) | Installation and command usage |
| [Operations guide](docs/operations-guide.md) | Release, inspection, troubleshooting, performance, and rollback |
| [Feature matrix](docs/feature-matrix.md) | Detailed capability status |
| [DDL rules](docs/ddl-generation-rules.md) | Dialect-specific DDL generation rules |
| [APS 元模型规则](docs/aps-metamodel-rules.md) | 核心概念、UML、顶层/普通模型、XML 规则与 Demo |
| [APS 类型/数据库映射](docs/aps-type-database-mapping.md) | 基础类型递归解析与 MySQL/Oracle/PostgreSQL 列类型规则 |
| [MCP guide](docs/mcp-guide.md) | MCP server usage with AI clients |
