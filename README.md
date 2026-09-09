# APSGraph

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

Read-only APS metadata scanner, compact SQLite relationship index, FTS5 fuzzy model search, incremental sync, model-to-CodeGraph bridge, capability classification report, impact query, and Table-to-MySQL-DDL preview.

## Install as a command

```bash
# Core CLI (standard library only)
pip install .

# Include Excel export
pip install ".[excel]"

# Development install with Excel export
pip install -e ".[excel]"
```

This installs the `apsgraph` command. Dependencies are declared in `pyproject.toml`; a separate `requirements.txt` is not required for normal installation.

## Default paths

Run `apsgraph` from the APS workspace root whenever possible:

- Default workspace: current directory
- Default index: `.apsgraph/apsgraph.db`

```bash
cd /path/to/v8.7-all
apsgraph scan
apsgraph sync
apsgraph status
apsgraph stats
```

Use `apsgraph --version` for the installed version. To inspect that version's default options and the effective rules in `.apsgraph.json`:

```bash
apsgraph options
```

## XML indexing

APSGraph only reads recognized APS XML files from the workspace and writes their
semantic nodes and relationships to SQLite. It does not invoke Maven, select a
JDK, modify business source repositories, or execute generated DDL.

```bash
apsgraph scan
apsgraph sync
apsgraph scan --external-db /path/to/shared-index/.apsgraph/apsgraph.db
```

Unresolved model references remain warnings.

## Build or rebuild the index

```bash
apsgraph scan
```

`scan` is a full rebuild and upgrades known APS legacy indexes by rebuilding them as schema V2. It refuses databases containing unrelated tables; use a dedicated index path.

## Incrementally sync source changes

```bash
apsgraph sync
```

`sync` compares normalized relative paths and SHA-256 hashes, applies added/modified/deleted files in one transaction, then rebinds cross-file references. It refuses legacy schema files and indexes owned by another workspace.

## Query and preview DDL

```bash
apsgraph stats
apsgraph show SysDbTable.kapp_sundry_busi
apsgraph search "账户类型"
apsgraph impact BpDict.A.addr
apsgraph ddl kapp_sundry_busi --dialect mysql
```

DDL output is a fail-closed experimental subset and is never executed.

## Bridge an APS model to generated Java and CodeGraph consumers


```bash
cd /path/to/v8.7-all/ap-parent
mvn generate-sources
```

Then run the bridge from the workspace root:

```bash
apsgraph bridge SysParmTable.kapb_txn_log --codegraph ap-parent=/path/to/v8.7-all/ap-parent/.codegraph/codegraph.db --output .apsgraph/reports/kapb_txn_log-bridge.json
```

The bridge verifies `target/gen` directly using package, generated symbol, and `@ConfigType` evidence, then reads CodeGraph SQLite in immutable read-only mode to find indexed Java consumers. It never writes CodeGraph's private database. Coverage is explicit: repositories without a CodeGraph index are reported as gaps, and dynamic/runtime references remain out of scope.

## Audit model and Java package capability grouping

```bash
apsgraph classify --output .apsgraph/reports/capability-audit.md --json-output .apsgraph/reports/capability-audit.json
```

Classification uses model IDs, descriptions, paths, packages, and class names as evidence. It is a heuristic architecture audit, not an automatic rewrite: mixed and unclassified results require owner confirmation before moving models or Java packages.
