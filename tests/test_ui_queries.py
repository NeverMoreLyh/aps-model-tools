import sqlite3
import tempfile
import unittest
from pathlib import Path

from apsgraph.ui_queries import list_models
from apsgraph.scanner import scan_workspace


class UiQueryTest(unittest.TestCase):
    def test_list_models_returns_top_level_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Demo.tables.xml").write_text(
                '<schema id="Demo"><table id="t"><fields><field id="id"/></fields></table></schema>',
                encoding="utf-8",
            )
            db_path = root / "index.db"
            scan_workspace(root, db_path)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                models = list_models(conn)
                self.assertEqual(["SCHEMA"], [model["kind"] for model in models])
                self.assertEqual(["Demo"], [model["full_id"] for model in models])
            finally:
                conn.close()
