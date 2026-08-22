import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from apsgraph.cli import main
from apsgraph.scanner import scan_workspace
from apsgraph.store import connect
from tests.test_scanner import FIXTURE_FILES


class SyncCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.db = Path(self.tmp.name) / "models.db"
        for relative, content in FIXTURE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        scan_workspace(self.root, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_cli_reports_change_counts(self):
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["sync", "--workspace", str(self.root), "--db", str(self.db)])
        self.assertEqual(0, rc)
        data = json.loads(out.getvalue())
        self.assertEqual(len(FIXTURE_FILES), data["unchanged"])
        self.assertEqual(0, data["modified"])

    def test_sync_rejects_legacy_database(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        conn = connect(legacy, initialize=False)
        conn.execute("create table old_table(value text)")
        conn.commit(); conn.close()
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["sync", "--workspace", str(self.root), "--db", str(legacy)])
        self.assertEqual(2, rc)
        self.assertIn("legacy APS index schema", out.getvalue())


if __name__ == "__main__":
    unittest.main()
