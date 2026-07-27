import tempfile
import unittest
from pathlib import Path

from aps_model_tools.cli import main


class CliTest(unittest.TestCase):
    def test_read_command_rejects_missing_database_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.db"
            rc = main(["stats", "--db", str(db)])
            self.assertEqual(2, rc)
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
