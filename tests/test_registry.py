import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apsgraph.registry import (
    default_selection,
    load_registry,
    registry_path,
    resolve_workspace,
    save_registry,
    touch_workspace,
    upsert_workspace,
    workspace_overview,
)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.registry = self.home / "registry.json"
        patcher = mock.patch.dict(os.environ, {"APSGRAPH_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_workspace(self, name, db_name="apsgraph.db"):
        root = Path(self.tmp.name) / name
        (root / ".apsgraph").mkdir(parents=True, exist_ok=True)
        db = root / ".apsgraph" / db_name
        db.write_bytes(b"sqlite")
        return root, db

    def test_registry_path_honors_apshgraph_home_override(self):
        self.assertEqual(self.registry, registry_path())

    def test_load_missing_registry_returns_empty(self):
        self.assertEqual({"version": 1, "workspaces": []}, load_registry(self.registry))
        self.assertEqual([], workspace_overview(self.registry))

    def test_upsert_creates_entry_with_default_name_and_fields(self):
        root, db = self.make_workspace("repo-a")
        entry = upsert_workspace(root, db, path=self.registry)

        self.assertEqual("repo-a", entry["name"])
        self.assertEqual(str(root.resolve()), entry["workspacePath"])
        self.assertEqual(str(db.resolve()), entry["dbPath"])
        self.assertTrue(entry["lastScanAt"])
        self.assertIsNone(entry["lastUsedAt"])
        self.assertTrue(self.registry.is_file())
        self.assertEqual(1, len(workspace_overview(self.registry)))

    def test_upsert_is_keyed_by_resolved_workspace_path(self):
        root_a, db_a = self.make_workspace("repo-a")
        root_b, db_b = self.make_workspace("repo-a-copy")
        upsert_workspace(root_a, db_a, path=self.registry)
        upsert_workspace(root_b, db_b, path=self.registry)

        # 同名目录不算重复：唯一键是 workspace 绝对路径
        self.assertEqual(2, len(workspace_overview(self.registry)))
        # 重复 upsert 同一 workspace 是刷新而非新增
        updated = upsert_workspace(root_a, db_a, path=self.registry)
        self.assertEqual(2, len(workspace_overview(self.registry)))
        self.assertEqual(str(db_a.resolve()), updated["dbPath"])

    def test_upsert_keeps_existing_name_unless_explicit(self):
        root, db = self.make_workspace("repo-a")
        upsert_workspace(root, db, name="custom", path=self.registry)
        entry = upsert_workspace(root, db, path=self.registry)
        self.assertEqual("custom", entry["name"])
        entry = upsert_workspace(root, db, name="renamed", path=self.registry)
        self.assertEqual("renamed", entry["name"])

    def test_load_corrupted_registry_fails_closed(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.registry.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_registry(self.registry)

    def test_load_registry_rejects_unsupported_structure(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps({"version": 99, "workspaces": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_registry(self.registry)
        self.registry.write_text(json.dumps([{"name": "x"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_registry(self.registry)
        self.registry.write_text(json.dumps(
            {"version": 1, "workspaces": [{"name": "x", "workspacePath": "/tmp"}]}),
            encoding="utf-8")
        with self.assertRaises(ValueError):
            load_registry(self.registry)

    def test_save_leaves_no_temp_files_behind(self):
        root, db = self.make_workspace("repo-a")
        upsert_workspace(root, db, path=self.registry)
        leftovers = list(self.home.glob("registry.json.*.tmp"))
        self.assertEqual([], leftovers)

    def test_overview_flags_availability_and_sorts_by_last_used(self):
        root_a, db_a = self.make_workspace("repo-a")
        root_b, db_b = self.make_workspace("repo-b")
        upsert_workspace(root_a, db_a, path=self.registry)
        upsert_workspace(root_b, db_b, path=self.registry)
        touch_workspace(root_a, path=self.registry)

        overview = workspace_overview(self.registry)
        self.assertEqual(["repo-a", "repo-b"], [item["name"] for item in overview])
        self.assertTrue(overview[0]["available"])

        db_b.unlink()
        overview = workspace_overview(self.registry)
        by_name = {item["name"]: item for item in overview}
        self.assertTrue(by_name["repo-a"]["available"])
        self.assertFalse(by_name["repo-b"]["available"])

    def test_touch_is_noop_for_unregistered_workspace(self):
        root, _ = self.make_workspace("stranger")
        self.assertFalse(touch_workspace(root, path=self.registry))
        self.assertEqual([], workspace_overview(self.registry))

    def test_resolve_by_name(self):
        root, db = self.make_workspace("repo-a")
        upsert_workspace(root, db, path=self.registry)
        ref = resolve_workspace("repo-a", path=self.registry)
        self.assertEqual(db.resolve(), ref.db_path)
        self.assertEqual(root.resolve(), ref.source_root)
        self.assertEqual(root.resolve(), ref.workspace_path)

    def test_resolve_ambiguous_name_lists_candidates(self):
        root_a, db_a = self.make_workspace("dir-one")
        root_b, db_b = self.make_workspace("dir-two")
        upsert_workspace(root_a, db_a, name="same", path=self.registry)
        upsert_workspace(root_b, db_b, name="same", path=self.registry)
        with self.assertRaises(ValueError) as context:
            resolve_workspace("same", path=self.registry)
        message = str(context.exception)
        self.assertIn("ambiguous workspace name: same", message)
        self.assertIn(str(root_a.resolve()), message)
        self.assertIn(str(root_b.resolve()), message)

    def test_resolve_by_workspace_path(self):
        root, db = self.make_workspace("repo-a")
        upsert_workspace(root, db, name="named", path=self.registry)
        ref = resolve_workspace(str(root), path=self.registry)
        self.assertEqual(root.resolve(), ref.workspace_path)

    def test_resolve_loose_unregistered_directory(self):
        root, db = self.make_workspace("loose")
        ref = resolve_workspace(str(root), path=self.registry)
        self.assertEqual(db.resolve(), ref.db_path)
        self.assertEqual(root.resolve(), ref.source_root)
        self.assertIsNone(ref.workspace_path)
        self.assertEqual([], workspace_overview(self.registry))

    def test_resolve_unknown_token_lists_registered_names(self):
        root, db = self.make_workspace("repo-a")
        upsert_workspace(root, db, path=self.registry)
        with self.assertRaises(ValueError) as context:
            resolve_workspace("nope", path=self.registry)
        self.assertIn("unknown workspace: nope", str(context.exception))
        self.assertIn("repo-a", str(context.exception))

    def test_default_selection_prefers_registered_cwd(self):
        root, db = self.make_workspace("cwd-repo")
        upsert_workspace(root, db, path=self.registry)
        other, other_db = self.make_workspace("other")
        upsert_workspace(other, other_db, path=self.registry)
        touch_workspace(other, path=self.registry)

        ref = default_selection(cwd=root, path=self.registry)
        self.assertEqual(root.resolve(), ref.workspace_path)

    def test_default_selection_falls_back_to_loose_cwd_index(self):
        root, db = self.make_workspace("unregistered")
        ref = default_selection(cwd=root, path=self.registry)
        self.assertEqual(db.resolve(), ref.db_path)
        self.assertIsNone(ref.workspace_path)

    def test_default_selection_prefers_last_used_then_latest_scan(self):
        root_a, db_a = self.make_workspace("repo-a")
        root_b, db_b = self.make_workspace("repo-b")
        upsert_workspace(root_a, db_a, path=self.registry)
        upsert_workspace(root_b, db_b, path=self.registry)
        nowhere = Path(self.tmp.name) / "nowhere"

        # 都未使用过：取最近扫描的（显式时间戳，避免同秒并列）
        payload = load_registry(self.registry)
        by_name = {item["name"]: item for item in payload["workspaces"]}
        by_name["repo-a"]["lastScanAt"] = "2026-01-01 00:00:00"
        by_name["repo-b"]["lastScanAt"] = "2025-01-01 00:00:00"
        save_registry(payload, self.registry)
        ref = default_selection(cwd=nowhere, path=self.registry)
        self.assertEqual(root_a.resolve(), ref.workspace_path)

        # repo-b 被使用过：优先上次使用的
        touch_workspace(root_b, path=self.registry)
        ref = default_selection(cwd=nowhere, path=self.registry)
        self.assertEqual(root_b.resolve(), ref.workspace_path)

    def test_default_selection_prefers_available_index_over_stale(self):
        root_a, db_a = self.make_workspace("repo-a")
        root_b, db_b = self.make_workspace("repo-b")
        upsert_workspace(root_a, db_a, path=self.registry)
        upsert_workspace(root_b, db_b, path=self.registry)
        touch_workspace(root_a, path=self.registry)
        db_a.unlink()

        # 上次使用的 repo-a 索引已失效：回退到仍可用的 repo-b
        ref = default_selection(cwd=Path(self.tmp.name) / "nowhere", path=self.registry)
        self.assertEqual(root_b.resolve(), ref.workspace_path)

    def test_default_selection_fails_closed_when_nothing_exists(self):
        with self.assertRaises(FileNotFoundError):
            default_selection(cwd=Path(self.tmp.name) / "empty", path=self.registry)


if __name__ == "__main__":
    unittest.main()
