# APSGraph Agent Rules

## Scope

These rules apply to every agent working in this repository. They are in addition to the repository's product requirements, design documents, usage guide, and operations guide.

## 1. Product boundaries

- APSGraph is a read-only APS metadata analysis tool.
- Never modify business source repositories.
- Never execute generated DDL automatically.
- Never write to a CodeGraph database.
- Writes are allowed only under this tool repository, registered workspaces' `.apsgraph/` caches and their index databases (the `apsgraph workspace` maintenance commands may read, sync, rebuild, vacuum, and — with explicit `--purge` — delete these caches), and the user-level registries under `~/.apsgraph/` (overridable via `APSGRAPH_WORKBENCH_REGISTRY` / `APSGRAPH_HOME`): the workbench instance registry (`workbench-registry.json`) and the global workspace registry (`registry.json` — only `scan` registration, workbench last-used timestamp refreshes, and `workspace remove` may write there).

## 2. Documentation freshness

The following documents are long-lived source of truth and must be updated together with relevant behavior changes:

1. `docs/requirements.md`
2. `docs/design.md`
3. `docs/product-whitepaper.md`
4. `docs/usage-guide.md`
5. `docs/operations-guide.md`

A change that alters behavior must update the applicable document before release. Do not leave stale instructions in documentation or examples.

## 3. Code and test requirements

- Keep the core CLI usable with Python 3.9+ and no mandatory third-party runtime dependency.
- Preserve machine-readable JSON on stdout (the `apsgraph workspace` maintenance family prints human-readable tables by default and JSON via `--json`).
- Send human-facing stage and progress logs to stderr.
- Preserve fail-closed behavior for Maven build failure, coordinate version conflicts, JDK conflicts, and unsafe index replacement.
- Existing indexes must remain untouched when a build, dependency resolution, or import fails.
- Add focused regression tests for every bug fix.
- Add behavior tests for every new feature.
- Run the complete test suite before commit:

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests
```

- Compile all source files before commit:

```bash
PYTHONPYCACHEPREFIX=/tmp/apsgraph-pyc python3 -m py_compile src/apsgraph/*.py
```

## 4. Version management standard

Every completed change, including behavior changes, documentation baseline changes, and operational rule changes, must be released with a new project version.

For every change, an agent must, in order:

1. Update implementation and relevant documentation.
2. Add or update tests when applicable.
3. Bump the package version in both:
   - `pyproject.toml`
   - `src/apsgraph/__init__.py`
4. Bump document version headers that declare the active release.
5. Run syntax compilation and the complete test suite.
6. Create a Git commit with a clear conventional-style subject.
7. Build a wheel.
8. Install the wheel as the `apsgraph` command.
9. Verify:
   - `apsgraph --version`
   - `apsgraph options`
10. Push the commit to the configured remote branch.

Do not leave successful work uncommitted. Do not commit a new version without tests and documentation. Do not skip the installed CLI verification.

## 5. Release commands

Build and install:

```bash
WHEEL_DIR=/tmp/apsgraph-cli-wheel-$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])' 2>/dev/null || sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
mkdir -p "$WHEEL_DIR"
python3 -m pip wheel . --no-deps -w "$WHEEL_DIR"
python3 -m pip install --force-reinstall "$WHEEL_DIR"/apsgraph-*.whl
apsgraph --version
apsgraph options
```

Commit and push:

```bash
git status --short
git add .
git commit -m "<type>: <summary>"
git push origin HEAD
```

## 6. Definition of done

A task is complete only when:

- All applicable documents are fresh.
- All tests pass.
- The version is bumped.
- The change is committed.
- The commit is pushed.
- The wheel is built and installed.
- The installed `apsgraph` command reports the expected version.
- No unintended worktree changes remain.
