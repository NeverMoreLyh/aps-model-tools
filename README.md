# APS Model Tools

Read-only APS metadata scanner, compact SQLite relationship index, incremental sync, model-to-CodeGraph bridge, capability classification report, impact query, and Table-to-MySQL-DDL preview.

## Build or rebuild the index

```bash
PYTHONPATH=src python3 -m aps_model_tools scan \
  --workspace /Users/joshua/code/v8.7-all \
  --db .data/v87-models-v2.db
```

`scan` is a full rebuild and upgrades known APS legacy indexes by rebuilding them as schema V2. It refuses databases containing unrelated tables; use a dedicated index path.

## Incrementally sync source changes

```bash
PYTHONPATH=src python3 -m aps_model_tools sync \
  --workspace /Users/joshua/code/v8.7-all \
  --db .data/v87-models-v2.db
```

`sync` compares normalized relative paths and SHA-256 hashes, applies added/modified/deleted files in one transaction, then rebinds cross-file references. It refuses legacy schema files and indexes owned by another workspace.

## Query and preview DDL

```bash
PYTHONPATH=src python3 -m aps_model_tools stats --db .data/v87-models-v2.db
PYTHONPATH=src python3 -m aps_model_tools show SysDbTable.kapp_sundry_busi --db .data/v87-models-v2.db
PYTHONPATH=src python3 -m aps_model_tools impact BpDict.A.addr --db .data/v87-models-v2.db
PYTHONPATH=src python3 -m aps_model_tools ddl kapp_sundry_busi --dialect mysql --db .data/v87-models-v2.db
```

DDL output is a fail-closed experimental MySQL subset and is never executed.

## Bridge an APS model to generated Java and CodeGraph consumers

```bash
PYTHONPATH=src python3 -m aps_model_tools bridge \
  SysParmTable.kapb_txn_log \
  --workspace /Users/joshua/code/v8.7-all \
  --db .data/v87-models.db \
  --codegraph ap-parent=/Users/joshua/code/v8.7-all/ap-parent/.codegraph/codegraph.db \
  --output .data/reports/kapb_txn_log-bridge.json
```

The bridge verifies `target/gen` directly using package, generated symbol, and `@ConfigType` evidence, then reads CodeGraph SQLite in immutable read-only mode to find indexed Java consumers. It never writes CodeGraph's private database. Coverage is explicit: repositories without a CodeGraph index are reported as gaps, and dynamic/runtime references remain out of scope.

## Audit model and Java package capability grouping

```bash
PYTHONPATH=src python3 -m aps_model_tools classify \
  --workspace /Users/joshua/code/v8.7-all \
  --db .data/v87-models.db \
  --output .data/reports/v87-capability-audit.md \
  --json-output .data/reports/v87-capability-audit.json
```

Classification uses model IDs, descriptions, paths, packages, and class names as evidence. It is a heuristic architecture audit, not an automatic rewrite: mixed and unclassified results require owner confirmation before moving models or Java packages.
