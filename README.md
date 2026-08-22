# APSGraph

Read-only APS metadata scanner, compact SQLite relationship index, incremental sync, model-to-CodeGraph bridge, capability classification report, impact query, and Table-to-MySQL-DDL preview.

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

## Maven dependencies and framework JAR models

`apsgraph scan` and `apsgraph sync` only read workspace XML and never invoke Maven. To build the workspace, copy resolved runtime dependencies, and combine their framework XML with workspace XML:

```bash
apsgraph scan --include-deps
```

Defaults are configurable:

| Option | Default |
|---|---|
| Maven goal | `install` |
| Tests | skipped (`-DskipTests`) |
| Dependency scope | `runtime` (includes compile/runtime) |
| Index | `.apsgraph/apsgraph.db` |
| Cache | `.apsgraph/` |
| Parallel Maven jobs | `4` (`--jobs`) |

APSGraph discovers `pom.xml` projects, identifies Maven reactor/aggregator roots, and builds those roots in workspace dependency order, stops on the first failure, rejects different versions of the same Maven `groupId:artifactId` coordinate, and only atomically publishes the completed index. Dependency analysis skips packaging/aggregator POMs and intermediate workspace parents; the top boundary of a local parent chain is analyzed once (for example, `prod-parent -> ap-out-parent -> ap-parent -> external parent` analyzes only `ap-parent` from that parent chain). Different groups may reuse an artifact ID safely. Projects matching `*dist` are excluded from dependency analysis by default. Workspace rules can be tracked in `.apsgraph.json` (`excludeProjects` or `maven.excludeProjects`), while repeatable `--exclude-project PATTERN` adds command-line overrides and `--no-default-project-excludes` disables the built-in default rule. Exclusion affects dependency analysis, not reactor builds. Existing indexes remain unchanged when a build, dependency check, or JAR XML import fails.

Workspaces that mix JDK 8 and JDK 17 projects can define reactor-level JDK profiles. Resolution is fail-closed; a reactor that requires multiple `JAVA_HOME` values is rejected. CLI project overrides win over `--jdk`, which wins over workspace rules/default:

```json
{
  "maven": {
    "excludeProjects": ["*dist"],
    "jdk": {
      "default": "8",
      "javaHomes": {
        "8": "/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home",
        "17": "auto"
      },
      "rules": [
        {"match": ["api-parent", "api-parent/*"], "jdk": "17"}
      ]
    }
  }
}
```

Build units respect inter-reactor dependencies and independent build units can run in parallel with `--jobs`; dependency resolution starts only after the entire build phase succeeds and also parallelizes independent projects. Use `--project-jdk PROJECT=PROFILE`, `--jdk PROFILE`, or `--java-home PROFILE=PATH` to override the workspace policy. The same resolved JDK is used for build, `dependency:list`, and `dependency:copy-dependencies`. During `scan --include-deps`, APSGraph prints major-stage and per-project progress to stderr while keeping machine-readable JSON on stdout.

To refresh dependency models in an existing index without rebuilding workspace XML:

```bash
apsgraph import-maven-deps
```

Add `--build` to run Maven `install` first. Manual JAR import remains available:

```bash
apsgraph import-jars --jar /path/to/aps-foundation.jar --jar /path/to/aps-common.jar
```

Imported JAR entries use logical `jar:<jar>!/<entry>` paths, are retained during workspace sync, and participate in reference resolution.

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
apsgraph impact BpDict.A.addr
apsgraph ddl kapp_sundry_busi --dialect mysql
```

DDL output is a fail-closed experimental subset and is never executed.

## Bridge an APS model to generated Java and CodeGraph consumers

Build generated Java first when `target/gen` is absent (normally with the project's Maven generate-sources/build goal):

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
