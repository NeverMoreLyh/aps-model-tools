import contextlib
import io
import json
import os
import tempfile
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
            "database": ".apsgraph/apsgraph.db",
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

    def test_read_command_rejects_missing_database_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.db"
            rc = main(["stats", "--db", str(db)])
            self.assertEqual(2, rc)
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
