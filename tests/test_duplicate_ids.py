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

    def test_stable_ids_use_semantic_identity_not_ordinal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "models/foo.tables.xml"
            model.parent.mkdir(parents=True)
            model.write_text("""<schema id="Loan">
              <table id="Customer"><fields>
                <field id="name"/>
                <field id="amount"/>
              </fields></table>
            </schema>""", encoding="utf-8")
            db = root / "models.db"
            scan_workspace(root, db)
            conn = connect(db, read_only=True)
            before = {
                row["full_id"]: row["stable_id"]
                for row in conn.execute("select full_id,stable_id from nodes where full_id!=''")
            }
            conn.close()

            model.write_text("""<schema id="Loan">
              <table id="Customer"><fields>
                <field id="created_at"/>
                <field id="name"/>
                <field id="amount"/>
              </fields></table>
            </schema>""", encoding="utf-8")
            scan_workspace(root, db)
            conn = connect(db, read_only=True)
            after = {
                row["full_id"]: row["stable_id"]
                for row in conn.execute("select full_id,stable_id from nodes where full_id!=''")
            }
            conn.close()

            self.assertEqual("model:TABLE:models/foo.tables.xml#Loan.Customer", before["Loan.Customer"])
            self.assertEqual("model:FIELD:models/foo.tables.xml#Loan.Customer.name", before["Loan.Customer.name"])
            self.assertEqual(before["Loan.Customer"], after["Loan.Customer"])
            self.assertEqual(before["Loan.Customer.name"], after["Loan.Customer.name"])
            self.assertEqual(before["Loan.Customer.amount"], after["Loan.Customer.amount"])

    def test_duplicate_semantic_ids_get_deterministic_disambiguators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "models/foo.tables.xml"
            model.parent.mkdir(parents=True)
            model.write_text("""<schema id="Loan">
              <table id="Customer"><fields>
                <field id="name"/><field id="name"/>
              </fields></table>
            </schema>""", encoding="utf-8")
            db = root / "models.db"
            scan_workspace(root, db)
            conn = connect(db, read_only=True)
            values = [row[0] for row in conn.execute(
                "select stable_id from nodes where kind='FIELD' order by id")]
            conn.close()

            self.assertEqual([
                "model:FIELD:models/foo.tables.xml#Loan.Customer.name~1",
                "model:FIELD:models/foo.tables.xml#Loan.Customer.name~2",
            ], values)


if __name__ == "__main__":
    unittest.main()
