import tempfile
import unittest
from pathlib import Path

from apsgraph.scanner import scan_workspace
from apsgraph.store import connect


class DuplicateIdTest(unittest.TestCase):
    def test_duplicate_nested_ids_are_preserved_as_distinct_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "Duplicate.serviceType.xml"
            model.write_text("""<schema id="Duplicate" package="demo">
              <table id="demo" name="demo">
                <fields><field id="value" type="string"/></fields>
                <odbindexes><index id="idx" type="unique" fields="value"/></odbindexes>
                <indexes><index id="idx" type="unique" fields="value"/></indexes>
              </table>
            </schema>""", encoding="utf-8")
            db = root / "models.db"
            summary = scan_workspace(root, db)
            self.assertEqual(1, summary.parsed_files)
            conn = connect(db)
            values = conn.execute("select stable_id,full_id from nodes where raw_id='idx' order by stable_id").fetchall()
            self.assertEqual(2, len(values))
            self.assertNotEqual(values[0]["stable_id"], values[1]["stable_id"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
