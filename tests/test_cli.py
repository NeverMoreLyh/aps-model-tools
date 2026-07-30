import tempfile
import unittest
from pathlib import Path

from aps_model_tools.cli import main
from aps_model_tools.store import connect


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

    def test_read_command_rejects_missing_database_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.db"
            rc = main(["stats", "--db", str(db)])
            self.assertEqual(2, rc)
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
