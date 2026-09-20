"""Global workspace registry at ``~/.apsgraph/registry.json``.

``scan`` is the only command that registers: after a successful scan it
upserts the workspace entry, keyed by the resolved workspace path.  The
workbench reads the registry on every request — a newly scanned workspace
is switchable without a server restart — and refreshes ``lastUsedAt``
when a registered workspace is served.

Writes use the same temp-file + atomic-replace strategy as index
publishing; a corrupted registry fails closed instead of being silently
rebuilt.  ``APSGRAPH_HOME`` redirects the registry directory (tests and
CI isolation).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REGISTRY_VERSION = 1

# In-process lock: workbench request threads refresh lastUsedAt while a
# concurrent scan may upsert.  Cross-process writers settle last-writer-wins
# on whole-file replace, which only ever loses a timestamp refresh.
_LOCK = threading.Lock()


def registry_dir() -> Path:
    home = os.environ.get("APSGRAPH_HOME")
    return Path(home).expanduser() if home else Path.home() / ".apsgraph"


def registry_path() -> Path:
    return registry_dir() / "registry.json"


@dataclass
class WorkspaceRef:
    """A resolved workspace: which index to open and where the sources live.

    ``workspace_path`` is ``None`` for sources that never registered: an
    explicit ``--db`` or an unregistered directory served via the loose rule.
    """

    db_path: Path
    source_root: Path
    workspace_path: Optional[Path] = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _empty_registry() -> Dict[str, Any]:
    return {"version": REGISTRY_VERSION, "workspaces": []}


def _validate_entry(entry: Any, source: Path) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"invalid workspace registry {source}: entries must be objects")
    for key in ("name", "workspacePath", "dbPath"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ValueError(
                f"invalid workspace registry {source}: entry field '{key}' must be a non-empty string")


def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the registry; a missing file is an empty registry, a broken one
    is an error (fail closed), never silently discarded."""
    target = Path(path) if path else registry_path()
    if not target.is_file():
        return _empty_registry()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid workspace registry {target}: {exc}") from exc
    if (not isinstance(payload, dict)
            or payload.get("version") != REGISTRY_VERSION
            or not isinstance(payload.get("workspaces"), list)):
        raise ValueError(f"invalid workspace registry {target}: unsupported structure")
    for entry in payload["workspaces"]:
        _validate_entry(entry, target)
    return payload


def save_registry(payload: Dict[str, Any], path: Optional[Path] = None) -> None:
    target = Path(path) if path else registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _entry_for(payload: Dict[str, Any], workspace: Path) -> Optional[Dict[str, Any]]:
    return next((item for item in payload["workspaces"]
                 if Path(item["workspacePath"]) == workspace), None)


def upsert_workspace(workspace: Path | str, db_path: Path | str,
                     name: Optional[str] = None,
                     path: Optional[Path] = None) -> Dict[str, Any]:
    """Register a workspace after a successful scan, or refresh its entry.

    The resolved workspace path is the unique key, so rescanning the same
    repository updates its entry instead of duplicating it; the display name
    defaults to the directory basename and may repeat across workspaces.
    """
    workspace = Path(workspace).resolve()
    db_path = Path(db_path).resolve()
    with _LOCK:
        payload = load_registry(path)
        entry = _entry_for(payload, workspace)
        if entry is None:
            entry = {"name": name or workspace.name, "workspacePath": str(workspace),
                     "dbPath": "", "lastScanAt": None, "lastUsedAt": None}
            payload["workspaces"].append(entry)
        if name:
            entry["name"] = name
        entry["dbPath"] = str(db_path)
        entry["lastScanAt"] = _now()
        save_registry(payload, path)
        return dict(entry)


def touch_workspace(workspace: Path | str, path: Optional[Path] = None) -> bool:
    """Refresh lastUsedAt for a registered workspace; unregistered paths are
    a no-op (there is nothing to touch)."""
    workspace = Path(workspace).resolve()
    with _LOCK:
        payload = load_registry(path)
        entry = _entry_for(payload, workspace)
        if entry is None:
            return False
        entry["lastUsedAt"] = _now()
        save_registry(payload, path)
        return True


def workspace_overview(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Registry entries annotated with index availability, most recently
    used first; used by the workbench workspace selector."""
    payload = load_registry(path)
    entries: List[Dict[str, Any]] = [dict(item) for item in payload["workspaces"]]
    for entry in entries:
        entry["available"] = Path(entry["dbPath"]).is_file()
    entries.sort(key=lambda item: item["name"])
    entries.sort(key=lambda item: item.get("lastUsedAt") or "", reverse=True)
    return entries


def _ref_from_entry(entry: Dict[str, Any]) -> WorkspaceRef:
    workspace = Path(entry["workspacePath"])
    return WorkspaceRef(db_path=Path(entry["dbPath"]), source_root=workspace,
                        workspace_path=workspace)


def _match_entry(payload: Dict[str, Any], token: str) -> Optional[Dict[str, Any]]:
    """Registry-only match for a name|path token: exact name first (ambiguous
    names fail with the candidate list), then the resolved workspace path.
    No loose rule here — opening an unregistered index is a workbench-only
    behavior; registry writes must never touch unregistered entries."""
    text = str(token)
    named = [item for item in payload["workspaces"] if item["name"] == text]
    if len(named) > 1:
        listing = "\n".join(f"  - {item['name']}: {item['workspacePath']}" for item in named)
        raise ValueError(f"ambiguous workspace name: {text}; matching entries:\n{listing}")
    if named:
        return named[0]
    resolved = Path(text).expanduser().resolve()
    for item in payload["workspaces"]:
        if Path(item["workspacePath"]) == resolved:
            return item
    return None


def resolve_workspace(token: str, path: Optional[Path] = None) -> WorkspaceRef:
    """Resolve a workbench ``ws``/``--workspace`` token.

    Order: exact name in the registry (ambiguous names fail with the
    candidate list), then a registered workspace path compared resolved,
    then the loose rule — a real directory holding
    ``<dir>/.apsgraph/apsgraph.db`` is served read-only without being
    registered, because registration only ever happens in scan.
    """
    text = str(token)
    payload = load_registry(path)
    entry = _match_entry(payload, text)
    if entry is not None:
        return _ref_from_entry(entry)
    resolved = Path(text).expanduser().resolve()
    loose_db = resolved / ".apsgraph" / "apsgraph.db"
    if resolved.is_dir() and loose_db.is_file():
        return WorkspaceRef(db_path=loose_db, source_root=resolved)
    names = ", ".join(item["name"] for item in payload["workspaces"]) or "（无）"
    raise ValueError(f"unknown workspace: {text}; registered workspaces: {names}")


def find_entry(token: str, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Registry-only lookup of a name|path token; ``None`` when unregistered.
    Ambiguous names raise with the candidate list."""
    return _match_entry(load_registry(path), token)


def remove_entry(token: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Remove a registry entry matched by name or resolved workspace path.

    Only the entry is removed — index files and sources stay untouched.
    Unregistered tokens are an error, not a no-op, so a typo cannot silently
    pass as success.
    """
    text = str(token)
    with _LOCK:
        payload = load_registry(path)
        entry = _match_entry(payload, text)
        if entry is None:
            names = ", ".join(item["name"] for item in payload["workspaces"]) or "（无）"
            raise ValueError(f"unknown workspace: {text}; registered workspaces: {names}")
        payload["workspaces"].remove(entry)
        save_registry(payload, path)
        return dict(entry)


def default_selection(cwd: Optional[Path] = None, path: Optional[Path] = None) -> WorkspaceRef:
    """Pick the workspace a bare ``apsgraph workbench`` opens.

    Registered current directory first, then an unregistered index in the
    current directory (the pre-registry habit keeps working), then the
    last-used entry, then the most recently scanned one; fail closed when
    nothing exists at all.
    """
    current = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    payload = load_registry(path)
    entry = _entry_for(payload, current)
    if entry is not None:
        return _ref_from_entry(entry)
    loose_db = current / ".apsgraph" / "apsgraph.db"
    if loose_db.is_file():
        return WorkspaceRef(db_path=loose_db, source_root=current)
    entries = payload["workspaces"]
    if entries:
        # Prefer an index that still exists so the workbench opens on data;
        # stale entries stay selectable in the UI and explain themselves.
        usable = [item for item in entries if Path(item["dbPath"]).is_file()]
        pool = usable or entries

        def sort_key(item: Dict[str, Any]) -> Any:
            return (item.get("lastUsedAt") or "", item.get("lastScanAt") or "")

        return _ref_from_entry(max(pool, key=sort_key))
    raise FileNotFoundError(
        f"no registered workspaces and no index in {current}; run 'apsgraph scan' first")
