"""Maintenance actions over the global workspace registry.

Every batch action resolves its targets through the registry (a single
``--workspace`` token or ``--all``) and returns one result dict per
workspace; a failure on one workspace is recorded and does not stop the
batch (fail-soft), and the caller turns the overall flag into the exit
code.  Index-writing actions reuse the existing scan/sync primitives
(their staging + atomic-replace and cross-workspace checks are inherited);
VACUUM runs in place, relying on SQLite's transactional crash safety, and
a locked database is a recorded error, never a crash.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .registry import _match_entry, load_registry, upsert_workspace
from .scanner import scan_workspace, sync_workspace, workspace_status
from .store import connect


def select_targets(token: Optional[str], select_all: bool,
                   registry: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Resolve maintenance targets: every registered entry, or one entry
    matched registry-only (name or resolved path, no loose rule)."""
    payload = load_registry(registry)
    if select_all:
        if not payload["workspaces"]:
            raise ValueError("no registered workspaces in the registry; run 'apsgraph scan' first")
        return [dict(item) for item in payload["workspaces"]]
    if not token:
        raise ValueError("either --workspace NAME|PATH or --all is required")
    entry = _match_entry(payload, token)
    if entry is None:
        names = ", ".join(item["name"] for item in payload["workspaces"]) or "（无）"
        raise ValueError(f"unknown workspace: {token}; registered workspaces: {names}")
    return [dict(entry)]


def _result(entry: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    result = {"name": entry["name"], "workspace": entry["workspacePath"],
              "db": entry["dbPath"]}
    result.update(fields)
    return result


def _run_batch(entries: List[Dict[str, Any]],
               action: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    results: List[Dict[str, Any]] = []
    ok = True
    for entry in entries:
        try:
            results.append(action(entry))
        except (ValueError, OSError, sqlite3.Error) as exc:
            ok = False
            results.append(_result(entry, state="error", error=str(exc)))
    return results, ok


def _require_db(entry: Dict[str, Any]) -> Path:
    db = Path(entry["dbPath"])
    if not db.is_file():
        raise FileNotFoundError(
            f"index database does not exist: {db}; run 'apsgraph scan' first")
    return db


def status_workspaces(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Per-workspace freshness: missing (index or sources gone), stale
    (source changes detected via the existing change-set logic), or fresh."""

    def one(entry: Dict[str, Any]) -> Dict[str, Any]:
        workspace = Path(entry["workspacePath"])
        db = Path(entry["dbPath"])
        if not workspace.is_dir():
            return _result(entry, state="missing", detail="workspace directory does not exist")
        if not db.is_file():
            return _result(entry, state="missing", detail="index database does not exist")
        state = workspace_status(workspace, db)
        payload = {key: value for key, value in asdict(state).items() if key != "up_to_date"}
        return _result(entry, state="fresh" if state.up_to_date else "stale", **payload)

    return _run_batch(entries, one)


def sync_workspaces(entries: List[Dict[str, Any]], fail_on_parse_error: bool = False,
                    progress: Optional[Callable[[str], None]] = None
                    ) -> Tuple[List[Dict[str, Any]], bool]:
    """Incremental sync of each target index via the existing sync primitive."""

    def one(entry: Dict[str, Any]) -> Dict[str, Any]:
        db = _require_db(entry)
        summary = sync_workspace(Path(entry["workspacePath"]), db, fail_on_parse_error, progress)
        return _result(entry, state="synced", **asdict(summary))

    return _run_batch(entries, one)


def check_workspaces(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Health check: index opens as an APS schema-v2 database and passes
    ``PRAGMA integrity_check``.  Missing indexes are a reported state, not
    an error; only unexpected failures (corruption, lock) mark the batch."""

    def one(entry: Dict[str, Any]) -> Dict[str, Any]:
        db = Path(entry["dbPath"])
        if not db.is_file():
            return _result(entry, state="missing", detail="index database does not exist")
        conn = connect(db, read_only=True)
        try:
            schema_version = conn.execute("pragma user_version").fetchone()[0]
            integrity = [row[0] for row in conn.execute("pragma integrity_check")]
        finally:
            conn.close()
        healthy = integrity == ["ok"]
        return _result(entry, state="ok" if healthy else "corrupt",
                       schema_version=schema_version,
                       integrity="ok" if healthy else "; ".join(integrity))

    return _run_batch(entries, one)


def rebuild_workspaces(entries: List[Dict[str, Any]], fail_on_parse_error: bool = False,
                       progress: Optional[Callable[[str], None]] = None,
                       registry: Optional[Path] = None) -> Tuple[List[Dict[str, Any]], bool]:
    """Full rebuild via the existing scan primitive (staging + atomic
    replace); each success refreshes the registry entry like a scan would."""

    def one(entry: Dict[str, Any]) -> Dict[str, Any]:
        workspace = Path(entry["workspacePath"])
        db = _require_db(entry)
        summary = scan_workspace(workspace, db, fail_on_parse_error, progress)
        upsert_workspace(workspace, db, path=registry)
        return _result(entry, state="rebuilt", **asdict(summary))

    return _run_batch(entries, one)


def vacuum_workspaces(entries: List[Dict[str, Any]]
                      ) -> Tuple[List[Dict[str, Any]], bool]:
    """In-place VACUUM of each target index; the entry must still verify as
    an APS index first.  A locked database (e.g. served by a workbench) is
    recorded per workspace by the fail-soft batch runner."""

    def one(entry: Dict[str, Any]) -> Dict[str, Any]:
        db = _require_db(entry)
        connect(db, read_only=True).close()  # refuse non-APS / wrong-version files
        size_before = db.stat().st_size
        conn = sqlite3.connect(str(db))
        try:
            conn.isolation_level = None  # VACUUM cannot run inside a transaction
            conn.execute("vacuum")
        finally:
            conn.close()
        return _result(entry, state="vacuumed", size_before=size_before,
                       size_after=db.stat().st_size)

    return _run_batch(entries, one)
