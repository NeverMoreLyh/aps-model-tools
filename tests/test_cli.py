import contextlib
import io
import sys
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import apsgraph.cli as cli_module
from apsgraph import __version__
from apsgraph.cli import main
from apsgraph.store import connect


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "index.db"
        conn = connect(self.db)
        conn.close()
        # scan 会写全局注册表；测试统一重定向到临时目录，避免污染真实 ~/.apsgraph
        self.registry_home = Path(self.tmp.name) / "apsgraph-home"
        patcher = mock.patch.dict(os.environ, {"APSGRAPH_HOME": str(self.registry_home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_refs_and_impact_reject_negative_depth(self):
        with self.assertRaises(SystemExit) as refs_exit:
            main(["refs", "anything", "--depth", "-1", "--db", str(self.db)])
        self.assertEqual(2, refs_exit.exception.code)
        with self.assertRaises(SystemExit) as impact_exit:
            main(["impact", "anything", "--depth", "-1", "--db", str(self.db)])
        self.assertEqual(2, impact_exit.exception.code)

    def test_bridge_cli_rejects_invalid_codegraph_mapping(self):
        rc = main(["bridge", "anything", "--workspace", self.tmp.name,
                   "--db", str(self.db), "--codegraph", "invalid"])
        self.assertEqual(2, rc)

    def test_xlsx_export_returns_clean_error_without_openpyxl(self):
        error = ImportError("openpyxl is required for Excel export")
        output = io.StringIO()
        with mock.patch.object(cli_module, "export_excel", side_effect=error), \
             contextlib.redirect_stdout(output):
            rc = main(["xlsx-export", "--db", str(self.db), "--output-dir", str(self.tmp.name)])

        self.assertEqual(2, rc)
        self.assertEqual({"error": str(error)}, json.loads(output.getvalue()))

    def test_version_uses_package_version(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as exit_context, \
                contextlib.redirect_stdout(output):
            main(["--version"])

        self.assertEqual(0, exit_context.exception.code)
        self.assertEqual(f"apsgraph {__version__}\n", output.getvalue())

    def test_options_show_defaults_and_effective_workspace_rules(self):
        workspace = Path(self.tmp.name) / "configured"
        workspace.mkdir()
        (workspace / ".apsgraph.json").write_text(json.dumps({
            "excludeProjects": ["legacy/*"],
        }), encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = main(["options", "--workspace", str(workspace)])

        self.assertEqual(0, rc)
        report = json.loads(output.getvalue())
        self.assertEqual(__version__, report["version"])
        self.assertEqual(str(workspace.resolve()), report["workspace"])
        self.assertEqual({
            "workspace": ".",
            "database": str(cli_module.DEFAULT_DB),  # 平台相关分隔符，直接取自默认值
            "external_indexes": [],
        }, report["defaults"])
        self.assertEqual({"external_indexes": []}, report["effective"])

    def test_removed_maven_and_ui_commands_are_not_available(self):
        for argv in (
            ["scan", "--include-deps"],
            ["scan", "--deps-mode", "framework"],
            ["scan", "--jdk", "17"],
            ["scan", "--project-jdk", "app=17"],
            ["scan", "--java-home", "17=/opt/jdk"],
            ["import-maven-deps"],
            ["import-jars"],
        ):
            with self.assertRaises(SystemExit) as context:
                cli_module.build_parser().parse_args(argv)
            self.assertEqual(2, context.exception.code)

    def test_scan_uses_workspace_and_database_defaults(self):
        workspace = Path(self.tmp.name) / "workspace" / "repo"
        model = workspace / "ap-base/src/main/resources/tables/SysParmTable.tables.xml"
        model.parent.mkdir(parents=True)
        model.write_text(
            '<schema id="SysParmTable" package="p"><table id="t" name="t"><fields/></table></schema>',
            encoding="utf-8",
        )
        old_cwd = Path.cwd()
        try:
            os.chdir(workspace)
            rc = main(["scan"])
        finally:
            os.chdir(old_cwd)

        self.assertEqual(0, rc)
        self.assertTrue((workspace / ".apsgraph/apsgraph.db").is_file())

    def _make_scan_workspace(self, name):
        from apsgraph.scanner import scan_workspace as _scan

        workspace = Path(self.tmp.name) / name
        model = workspace / "tables/Demo.tables.xml"
        model.parent.mkdir(parents=True)
        model.write_text(
            '<schema id="Demo" package="p"><table id="t" name="t"><fields/></table></schema>',
            encoding="utf-8")
        return workspace

    def test_scan_registers_workspace_in_global_registry(self):
        from apsgraph.registry import load_registry

        workspace = self._make_scan_workspace("reg-repo")
        rc = main(["scan", "--workspace", str(workspace), "--db", str(self.db)])
        self.assertEqual(0, rc)

        payload = load_registry()
        self.assertEqual(1, len(payload["workspaces"]))
        entry = payload["workspaces"][0]
        self.assertEqual("reg-repo", entry["name"])
        self.assertEqual(str(workspace.resolve()), entry["workspacePath"])
        self.assertEqual(str(self.db.resolve()), entry["dbPath"])
        self.assertTrue(entry["lastScanAt"])

        # 重复 scan 是刷新而非新增
        main(["scan", "--workspace", str(workspace), "--db", str(self.db)])
        self.assertEqual(1, len(load_registry()["workspaces"]))

    def test_scan_no_register_skips_registry(self):
        from apsgraph.registry import load_registry

        workspace = self._make_scan_workspace("skip-repo")
        rc = main(["scan", "--workspace", str(workspace), "--db", str(self.db), "--no-register"])
        self.assertEqual(0, rc)
        self.assertEqual({"version": 1, "workspaces": []}, load_registry())
        self.assertFalse((self.registry_home / "registry.json").exists())

    def test_scan_failed_run_does_not_register(self):
        from apsgraph.registry import load_registry

        empty = Path(self.tmp.name) / "empty-workspace"
        empty.mkdir()
        rc = main(["scan", "--workspace", str(empty), "--db", str(self.db)])
        self.assertEqual(2, rc)
        self.assertEqual({"version": 1, "workspaces": []}, load_registry())

    def test_scan_stderr_summary_and_show_warning_flag(self):
        import contextlib
        import io

        workspace = Path(self.tmp.name) / "warn-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        # 引用一个不存在的模型，产生 unresolved warning
        (workspace / "Demo.tables.xml").write_text(
            '<schema id="Demo"><table id="t"><fields>'
            '<field id="f" type="Missing.U_NOPE"/></fields></table></schema>',
            encoding="utf-8")
        err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = err
        try:
            rc = main(["scan", "--workspace", str(workspace), "--db", str(self.db)])
        finally:
            sys.stderr = old_stderr
        self.assertEqual(0, rc)
        err_text = err.getvalue()
        self.assertIn("scan 完成", err_text)
        self.assertIn("警告 1", err_text)
        self.assertNotIn("unresolved model reference", err_text)
        self.assertEqual("", sys.stdout.getvalue() if hasattr(sys.stdout, "getvalue") else "")

        err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = err
        try:
            rc = main(["scan", "--workspace", str(workspace), "--db", str(self.db),
                       "--show-warning"])
        finally:
            sys.stderr = old_stderr
        self.assertEqual(0, rc)
        self.assertIn("warning: unresolved model reference: Missing.U_NOPE",
                      err.getvalue())

    def test_search_cli_returns_ranked_model_metadata(self):
        workspace = Path(self.tmp.name) / "search-workspace"
        model = workspace / "Account.tables.xml"
        model.parent.mkdir(parents=True)
        model.write_text(
            '<schema id="Account"><table id="account_type" longname="账户类型" '
            'description="开户账户类型"/></schema>', encoding="utf-8")
        main(["scan", "--workspace", str(workspace), "--db", str(self.db)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["search", "开户", "--db", str(self.db)])

        self.assertEqual(0, rc)
        result = json.loads(output.getvalue())
        self.assertEqual(1, result["count"])
        self.assertEqual("TABLE", result["results"][0]["kind"])
        self.assertEqual("Account.account_type", result["results"][0]["full_id"])
        self.assertEqual("账户类型", result["results"][0]["chinese_name"])
        self.assertEqual("开户账户类型", result["results"][0]["description"])

    def test_read_command_rejects_missing_database_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.db"
            rc = main(["stats", "--db", str(db)])
            self.assertEqual(2, rc)
            self.assertFalse(db.exists())

    def test_workbench_port_argument_validation(self):
        for bad in ("70000", "-1", "abc"):
            with self.assertRaises(SystemExit) as context:
                cli_module.build_parser().parse_args(["workbench", "--port", bad])
            self.assertEqual(2, context.exception.code)
        args = cli_module.build_parser().parse_args(["workbench", "--port", "0"])
        self.assertEqual(0, args.port)
        # 默认即为随机空闲端口
        args = cli_module.build_parser().parse_args(["workbench"])
        self.assertEqual(0, args.port)

    def test_workbench_list_and_close_argument_rules(self):
        registry = Path(self.tmp.name) / "wb-registry.json"
        with mock.patch.dict(os.environ, {"APSGRAPH_WORKBENCH_REGISTRY": str(registry)}):
            # 默认 human 表格；空注册表输出表头
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "list"])
            self.assertEqual(0, rc)
            self.assertIn("PORT", output.getvalue())
            self.assertNotIn('"instances"', output.getvalue())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "list", "--json"])
            self.assertEqual(0, rc)
            self.assertEqual([], json.loads(output.getvalue())["instances"])

            # close 未注册端口 → rc 2 且报告 not_found（--json）
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "close", "--port", "8321", "--json"])
            self.assertEqual(2, rc)
            self.assertEqual([8321], json.loads(output.getvalue())["not_found"])

            # close 缺少 --port/--all，或两者同时给出 → rc 2
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "close"])
            self.assertEqual(2, rc)
            self.assertIn("error", json.loads(output.getvalue()))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "close", "--port", "1", "--all"])
            self.assertEqual(2, rc)
            self.assertIn("error", json.loads(output.getvalue()))

    def test_workbench_close_cli_stops_registered_instance(self):
        from apsgraph.workbench import REGISTRY_ENV, WorkbenchServer, register_workbench

        registry = Path(self.tmp.name) / "wb-registry.json"
        server = WorkbenchServer(self.db, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        register_workbench(
            {"port": server.server_port, "pid": os.getpid(),
             "url": f"http://127.0.0.1:{server.server_port}/", "db": str(self.db),
             "workspace": str(self.tmp.name), "started_at": "t"},
            path=registry)

        with mock.patch.dict(os.environ, {REGISTRY_ENV: str(registry)}), \
                mock.patch("apsgraph.workbench._terminate_pid") as terminate:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "close", "--port", str(server.server_port), "--json"])

        self.assertEqual(0, rc)
        payload = json.loads(output.getvalue())
        self.assertEqual([server.server_port], [e["port"] for e in payload["closed"]])
        terminate.assert_called_once_with(os.getpid())

        # 默认 human 输出：单行 stopped 确认
        register_workbench(
            {"port": server.server_port, "pid": os.getpid(),
             "url": f"http://127.0.0.1:{server.server_port}/", "db": str(self.db),
             "workspace": str(self.tmp.name), "started_at": "t"},
            path=registry)
        with mock.patch.dict(os.environ, {REGISTRY_ENV: str(registry)}), \
                mock.patch("apsgraph.workbench._terminate_pid"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(["workbench", "close", "--port", str(server.server_port)])
        self.assertEqual(0, rc)
        self.assertIn("stopped", output.getvalue())
        self.assertIn(f"pid {os.getpid()}", output.getvalue())
        self.assertNotIn('"closed"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
