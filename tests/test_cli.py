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
            "maven": {
                "jdk": {
                    "default": "8",
                    "javaHomes": {"8": "/opt/jdk8"},
                    "rules": [{"match": ["modern/*"], "jdk": "17"}],
                }
            },
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
            "cache_dir": ".apsgraph",
            "maven": {
                "goal": ["install"],
                "skip_tests": True,
                "dependency_scope": "runtime",
                "executable": "mvn",
                "jobs": 4,
                "default_excluded_projects": ["*dist"],
                "deps_mode": "full",
            },
            "external_indexes": [],
        }, report["defaults"])
        self.assertEqual(["legacy/*", "*dist"], report["effective"]["exclude_projects"])
        self.assertEqual("8", report["effective"]["maven_jdk"]["default"])
        self.assertEqual("/opt/jdk8", report["effective"]["maven_jdk"]["java_homes"]["8"])
        self.assertEqual(
            {"match": ["modern/*"], "jdk": "17"},
            report["effective"]["maven_jdk"]["rules"][0],
        )

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
