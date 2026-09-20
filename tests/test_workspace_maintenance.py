import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apsgraph.cli import main
from apsgraph.registry import load_registry, upsert_workspace
from apsgraph.scanner import scan_workspace


class WorkspaceMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = Path(self.tmp.name) / "home" / "registry.json"
        patcher = mock.patch.dict(os.environ, {"APSGRAPH_HOME": str(Path(self.tmp.name) / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspaces = {}
        for name, table in (("alpha", "table_a"), ("beta", "table_b")):
            root = Path(self.tmp.name) / name
            model = root / "tables" / "Demo.tables.xml"
            model.parent.mkdir(parents=True)
            model.write_text(
                '<schema id="Demo" package="p"><table id="%s" name="%s"><fields/></table></schema>'
                % (table, table), encoding="utf-8")
            db = root / "index.db"
            scan_workspace(root, db)
            upsert_workspace(root, db, path=self.registry)
            self.workspaces[name] = root

    def _run(self, argv):
        """Run a workspace command; JSON output is requested explicitly so
        the default human-readable tables stay covered by dedicated tests."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main([*argv, "--json"])
        return rc, json.loads(output.getvalue()) if output.getvalue().strip() else None

    def _modify_model(self, name, table):
        model = self.workspaces[name] / "tables" / "Demo.tables.xml"
        model.write_text(
            '<schema id="Demo" package="p"><table id="%s" name="%s"><fields>'
            '<field id="extra" type="string"/></fields></table></schema>' % (table, table),
            encoding="utf-8")

    def test_list_reports_registered_entries(self):
        rc, payload = self._run(["workspace", "list"])
        self.assertEqual(0, rc)
        names = {item["name"] for item in payload["workspaces"]}
        self.assertEqual({"alpha", "beta"}, names)
        self.assertTrue(all(item["available"] for item in payload["workspaces"]))

    def test_select_targets_registry_only_semantics(self):
        from apsgraph.workspace_maintenance import select_targets

        self.assertEqual(2, len(select_targets(None, True, self.registry)))
        self.assertEqual(["alpha"], [item["name"] for item in
                                     select_targets("alpha", False, self.registry)])
        self.assertEqual(["alpha"], [item["name"] for item in
                                     select_targets(str(self.workspaces["alpha"]), False, self.registry)])

        # 宽松直开是 workbench 专属：维护命令对未注册目录必须报错
        with self.assertRaises(ValueError):
            select_targets(str(self.workspaces["alpha"] / "nope"), False, self.registry)

    def test_select_targets_all_fails_on_empty_registry(self):
        from apsgraph.workspace_maintenance import select_targets

        empty = Path(self.tmp.name) / "empty" / "registry.json"  # 不存在 → 空注册表
        with self.assertRaises(ValueError) as context:
            select_targets(None, True, empty)
        self.assertIn("no registered workspaces", str(context.exception))

    def test_status_reports_fresh_stale_missing(self):
        self._modify_model("beta", "table_b")
        (self.workspaces["alpha"] / "index.db").unlink()

        # missing/stale/fresh 是报告型结果，不算执行失败：退出码 0
        rc, payload = self._run(["workspace", "status", "--all"])
        self.assertEqual(0, rc)
        by_name = {item["name"]: item for item in payload["results"]}
        self.assertEqual("missing", by_name["alpha"]["state"])
        self.assertEqual("stale", by_name["beta"]["state"])
        self.assertEqual(1, by_name["beta"]["modified"])

        # 修复 alpha 后回到 fresh
        scan_workspace(self.workspaces["alpha"], self.workspaces["alpha"] / "index.db")
        rc, payload = self._run(["workspace", "status", "--all"])
        self.assertEqual(0, rc)
        by_name = {item["name"]: item for item in payload["results"]}
        self.assertEqual("fresh", by_name["alpha"]["state"])

    def test_status_missing_directory(self):
        import shutil as _shutil

        _shutil.rmtree(self.workspaces["alpha"])
        rc, payload = self._run(["workspace", "status", "--all"])
        by_name = {item["name"]: item for item in payload["results"]}
        self.assertEqual("missing", by_name["alpha"]["state"])
        self.assertIn("directory", by_name["alpha"]["detail"])

    def test_sync_batch_is_fail_soft(self):
        self._modify_model("alpha", "table_a")
        real_sync = __import__("apsgraph.workspace_maintenance", fromlist=["sync_workspace"]).sync_workspace

        def broken(workspace, db, *args, **kwargs):
            if Path(workspace).name == "beta":
                raise sqlite3.OperationalError("database is locked")
            return real_sync(workspace, db, *args, **kwargs)

        with mock.patch("apsgraph.workspace_maintenance.sync_workspace", side_effect=broken):
            rc, payload = self._run(["workspace", "sync", "--all"])
        self.assertEqual(2, rc)
        by_name = {item["name"]: item for item in payload["results"]}
        self.assertEqual("synced", by_name["alpha"]["state"])
        self.assertEqual("error", by_name["beta"]["state"])
        self.assertIn("locked", by_name["beta"]["error"])

        # 修复后重跑：全部同步，退出码 0
        rc, payload = self._run(["workspace", "sync", "--all"])
        self.assertEqual(0, rc)
        self.assertTrue(all(item["state"] == "synced" for item in payload["results"]))
        self.assertEqual(0, payload["results"][0]["added"])  # 上次已同步

    def test_rebuild_refreshes_index_and_registry(self):
        from apsgraph.registry import load_registry, save_registry

        self._modify_model("alpha", "table_a")
        # 把 lastScanAt 改成过去的固定值，避免同秒时间戳相等导致断言失效
        payload = load_registry(self.registry)
        by_name = {item["name"]: item for item in payload["workspaces"]}
        by_name["alpha"]["lastScanAt"] = "2000-01-01 00:00:00"
        save_registry(payload, self.registry)

        rc, payload = self._run(["workspace", "rebuild", "--workspace", "alpha"])
        self.assertEqual(0, rc)
        self.assertEqual("rebuilt", payload["results"][0]["state"])
        self.assertGreater(payload["results"][0]["nodes"], 0)

        after = {item["name"]: item["lastScanAt"] for item in load_registry(self.registry)["workspaces"]}
        self.assertNotEqual("2000-01-01 00:00:00", after["alpha"])
        rc, payload = self._run(["workspace", "status", "--workspace", "alpha"])
        self.assertEqual("fresh", payload["results"][0]["state"])

    def test_check_reports_corrupt_and_missing(self):
        (self.workspaces["alpha"] / "index.db").write_bytes(b"not a sqlite file")
        (self.workspaces["beta"] / "index.db").unlink()

        rc, payload = self._run(["workspace", "check", "--all"])
        by_name = {item["name"]: item for item in payload["results"]}
        self.assertEqual(2, rc)  # alpha 索引损坏到无法打开，属执行错误
        self.assertEqual("error", by_name["alpha"]["state"])
        self.assertEqual("missing", by_name["beta"]["state"])

        # 健康索引给出 integrity 与 schema 版本（先移除垃圾文件，scan 拒绝覆盖非法索引）
        (self.workspaces["alpha"] / "index.db").unlink()
        scan_workspace(self.workspaces["alpha"], self.workspaces["alpha"] / "index.db")
        rc, payload = self._run(["workspace", "check", "--workspace", "alpha"])
        self.assertEqual(0, rc)
        result = payload["results"][0]
        self.assertEqual("ok", result["state"])
        self.assertEqual("ok", result["integrity"])
        self.assertEqual(2, result["schema_version"])

    def test_vacuum_reports_sizes(self):
        self._modify_model("alpha", "table_a")
        self._run(["workspace", "rebuild", "--workspace", "alpha"])  # 制造可回收空间
        rc, payload = self._run(["workspace", "vacuum", "--all"])
        self.assertEqual(0, rc)
        self.assertTrue(all(item["state"] == "vacuumed" for item in payload["results"]))
        for item in payload["results"]:
            self.assertGreater(item["size_before"], 0)

    def test_remove_entry_and_purge(self):
        rc, payload = self._run(["workspace", "remove", "--workspace", "alpha"])
        self.assertEqual(0, rc)
        self.assertEqual("alpha", payload["removed"]["name"])
        self.assertFalse(payload["purged"])
        self.assertTrue((self.workspaces["alpha"] / "index.db").is_file())  # 索引保留
        self.assertEqual({"beta"}, {item["name"] for item in
                                    load_registry(self.registry)["workspaces"]})

        # --purge 连 .apsgraph/ 缓存目录一起删
        cache_root = self.workspaces["beta"] / ".apsgraph"
        cache_root.mkdir()
        (cache_root / "apsgraph.db").write_bytes(b"x")
        rc, payload = self._run(["workspace", "remove", "--workspace", "beta", "--purge"])
        self.assertEqual(0, rc)
        self.assertTrue(payload["purged"])
        self.assertFalse(cache_root.exists())
        self.assertEqual([], load_registry(self.registry)["workspaces"])

    def test_remove_unknown_and_ambiguous_fail_closed(self):
        rc, payload = self._run(["workspace", "remove", "--workspace", "ghost"])
        self.assertEqual(2, rc)
        self.assertIn("unknown workspace: ghost", payload["error"])

        # 同名条目：不指定路径时拒绝执行并列出候选
        upsert_workspace(Path(self.tmp.name) / "gamma", Path(self.tmp.name) / "gamma" / "x.db",
                         name="alpha", path=self.registry)
        rc, payload = self._run(["workspace", "remove", "--workspace", "alpha"])
        self.assertEqual(2, rc)
        self.assertIn("ambiguous workspace name: alpha", payload["error"])
        # 按路径仍可精确删除
        rc, payload = self._run(["workspace", "remove", "--workspace", str(self.workspaces["alpha"])])
        self.assertEqual(0, rc)

    def test_target_arguments_are_required_and_exclusive(self):
        with self.assertRaises(SystemExit):
            main(["workspace", "sync"])
        with self.assertRaises(SystemExit):
            main(["workspace", "sync", "--workspace", "alpha", "--all"])

    def test_ids_are_deterministic_and_listed(self):
        from apsgraph.registry import entry_id

        rc, payload = self._run(["workspace", "list"])
        by_name = {item["name"]: item for item in payload["workspaces"]}
        self.assertEqual(entry_id(self.workspaces["alpha"]), by_name["alpha"]["id"])
        # 确定性：同一路径重复 upsert 得到同一 id
        upsert_workspace(self.workspaces["alpha"], self.workspaces["alpha"] / "index.db",
                         path=self.registry)
        self.assertEqual(by_name["alpha"]["id"], entry_id(self.workspaces["alpha"]))

    def test_workspace_token_accepts_id(self):
        from apsgraph.registry import entry_id

        alpha_id = entry_id(self.workspaces["alpha"])
        rc, payload = self._run(["workspace", "status", "--workspace", alpha_id])
        self.assertEqual(0, rc)
        self.assertEqual(["alpha"], [item["name"] for item in payload["results"]])

    def test_ambiguous_name_error_lists_ids_and_id_resolves(self):
        from apsgraph.registry import entry_id, upsert_workspace

        upsert_workspace(Path(self.tmp.name) / "gamma", Path(self.tmp.name) / "gamma" / "x.db",
                         name="alpha", path=self.registry)
        rc, payload = self._run(["workspace", "remove", "--workspace", "alpha"])
        self.assertEqual(2, rc)
        self.assertIn("ambiguous workspace name: alpha", payload["error"])
        self.assertIn(entry_id(self.workspaces["alpha"]), payload["error"])

        # 同名冲突下用 id 仍可精确删除
        rc, payload = self._run(["workspace", "remove", "--workspace",
                                 entry_id(self.workspaces["alpha"])])
        self.assertEqual(0, rc)
        self.assertEqual("alpha", payload["removed"]["name"])

    def test_default_output_is_human_table_and_json_is_opt_in(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["workspace", "list"])
        text = output.getvalue()
        self.assertEqual(0, rc)
        self.assertIn("ID", text)
        self.assertIn("NAME", text)
        self.assertIn("alpha", text)
        self.assertNotIn('"workspaces"', text)  # 默认不是 JSON

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["workspace", "list", "--json"])
        self.assertEqual(0, rc)
        self.assertIn('"workspaces"', output.getvalue())

        # 批量命令的默认输出是表格 + stderr 汇总；--json 输出 results 数组
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["workspace", "status", "--all"])
        self.assertIn("STATE", output.getvalue())
        self.assertIn("fresh", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["workspace", "status", "--all", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual({"alpha", "beta"}, {item["name"] for item in payload["results"]})

        # remove 默认单行确认
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["workspace", "remove", "--workspace", "alpha"])
        self.assertIn("removed alpha", output.getvalue())
        self.assertIn("id", output.getvalue())


if __name__ == "__main__":
    unittest.main()
