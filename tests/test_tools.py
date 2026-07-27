import tempfile
import unittest
from pathlib import Path

from aps_model_tools.ddl import generate_table_ddl
from aps_model_tools.impact import build_impact_report
from aps_model_tools.scanner import scan_workspace
from aps_model_tools.store import connect, find_nodes
from tests.test_scanner import FIXTURE_FILES


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for relative, content in FIXTURE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.db = self.root / "models.db"
        scan_workspace(self.root, self.db)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_mysql_ddl_resolves_restricted_types_and_indexes(self):
        result = generate_table_ddl(self.conn, "DemoTables.demo_user", "mysql")
        self.assertEqual([], result.errors)
        self.assertIn("CREATE TABLE `demo_user`", result.sql)
        self.assertIn("`created_at` VARCHAR(255) NOT NULL", result.sql)
        self.assertIn("`id` BIGINT NOT NULL", result.sql)
        self.assertIn("`name` VARCHAR(40) NOT NULL DEFAULT ''", result.sql)
        self.assertIn("`status` VARCHAR(1) NOT NULL DEFAULT 'O'", result.sql)
        self.assertIn("PRIMARY KEY (`id`)", result.sql)
        self.assertNotIn("CREATE UNIQUE INDEX `odb1`", result.sql)
        self.assertIn("CREATE INDEX `idx_demo_name` ON `demo_user` (`name`);", result.sql)
        self.assertIn("preview only", result.sql)

    def test_ddl_fails_closed_for_unresolved_type(self):
        table = self.root / "tables/Bad.tables.xml"
        table.write_text("""<schema id="Bad"><table id="bad" name="bad"><fields>
          <field id="value" type="Missing.U_VALUE" nullable="true"/>
        </fields></table></schema>""", encoding="utf-8")
        self.conn.close()
        scan_workspace(self.root, self.db)
        self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "Bad.bad", "mysql")
        self.assertTrue(result.errors)
        self.assertEqual("", result.sql)

    def test_ddl_fails_closed_when_index_references_missing_field(self):
        table = self.root / "tables/BadIndex.tables.xml"
        table.write_text("""<schema id="BadIndex"><table id="bad" name="bad"><fields>
          <field id="value" type="string" nullable="true"/>
        </fields><indexes><index id="idx_bad" type="index" fields="missing"/></indexes>
        </table></schema>""", encoding="utf-8")
        self.conn.close()
        scan_workspace(self.root, self.db)
        self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "BadIndex.bad", "mysql")
        self.assertTrue(result.errors)
        self.assertEqual("", result.sql)

    def test_ddl_rejects_empty_expanded_table(self):
        table = self.root / "tables/Empty.tables.xml"
        table.write_text("""<schema id="Empty"><table id="empty" name="empty"><fields/></table></schema>""", encoding="utf-8")
        self.conn.close()
        scan_workspace(self.root, self.db)
        self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "Empty.empty", "mysql")
        self.assertTrue(result.errors)
        self.assertEqual("", result.sql)

    def test_show_resolves_table_id_without_container_ambiguity(self):
        nodes = find_nodes(self.conn, "DemoTables.demo_user")
        self.assertEqual(["TABLE"], [node["kind"] for node in nodes])

    def test_ddl_fails_closed_for_empty_index_fields(self):
        table = self.root / "tables/EmptyIndex.tables.xml"
        table.write_text("""<schema id="EmptyIndex"><table id="bad" name="bad"><fields>
          <field id="value" type="string" nullable="true"/>
        </fields><indexes><index id="idx_empty" type="index" fields=""/></indexes>
        </table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "EmptyIndex.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_ddl_rejects_unkeyed_auto_increment(self):
        table = self.root / "tables/Identity.tables.xml"
        table.write_text("""<schema id="Identity"><table id="bad" name="bad"><fields>
          <field id="value" type="Base.U_ID" identity="true" nullable="false"/>
        </fields></table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "Identity.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_ddl_rejects_decimal_scale_greater_than_precision(self):
        datatype = self.root / "datatype/BadDecimal.u_schema.xml"
        datatype.write_text("""<schema id="BadDecimal"><restrictionType id="U_BAD" base="decimal" dbLength="2" dbFractionDigits="3"/></schema>""", encoding="utf-8")
        table = self.root / "tables/Decimal.tables.xml"
        table.write_text("""<schema id="Decimal"><table id="bad" name="bad"><fields><field id="value" type="BadDecimal.U_BAD"/></fields></table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "Decimal.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_ddl_rejects_non_positive_string_length(self):
        datatype = self.root / "datatype/BadString.u_schema.xml"
        datatype.write_text("""<schema id="BadString"><restrictionType id="U_BAD" base="string" maxLength="-1"/></schema>""", encoding="utf-8")
        table = self.root / "tables/BadString.tables.xml"
        table.write_text("""<schema id="BadStringTable"><table id="bad" name="bad"><fields><field id="value" type="BadString.U_BAD"/></fields></table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "BadStringTable.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_ddl_rejects_auto_increment_on_non_integer_type(self):
        table = self.root / "tables/StringIdentity.tables.xml"
        table.write_text("""<schema id="StringIdentity"><table id="bad" name="bad"><fields>
          <field id="value" type="Base.U_NAME" identity="true" primarykey="true" nullable="false"/>
        </fields></table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "StringIdentity.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_ddl_rejects_mysql_string_lengths_above_declared_limits(self):
        datatype = self.root / "datatype/TooLong.u_schema.xml"
        datatype.write_text("""<schema id="TooLong">
          <restrictionType id="U_VARCHAR" base="string" maxLength="65536"/>
          <restrictionType id="U_CHAR" base="fixString" maxLength="256"/>
        </schema>""", encoding="utf-8")
        table = self.root / "tables/TooLong.tables.xml"
        table.write_text("""<schema id="TooLongTable"><table id="bad" name="bad"><fields>
          <field id="v" type="TooLong.U_VARCHAR"/><field id="c" type="TooLong.U_CHAR"/>
        </fields></table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "TooLongTable.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_ddl_rejects_duplicate_physical_index_names(self):
        table = self.root / "tables/DuplicateIndex.tables.xml"
        table.write_text("""<schema id="DuplicateIndex"><table id="bad" name="bad"><fields>
          <field id="a" type="string"/><field id="b" type="string"/>
        </fields><indexes><index id="ix" fields="a"/><index id="ix" fields="b"/></indexes>
        </table></schema>""", encoding="utf-8")
        self.conn.close(); scan_workspace(self.root, self.db); self.conn = connect(self.db)
        result = generate_table_ddl(self.conn, "DuplicateIndex.bad", "mysql")
        self.assertTrue(result.errors); self.assertEqual("", result.sql)

    def test_impact_groups_type_and_dictionary_consumers(self):
        base = build_impact_report(self.conn, "Base.U_NAME", depth=3)
        self.assertEqual("Base.U_NAME", base["target"]["full_id"])
        self.assertGreaterEqual(base["summary"]["TYPE_REF"], 2)
        self.assertIn("DemoTables.demo_user.name", {n["full_id"] for n in base["affected_nodes"]})

        dictionary = build_impact_report(self.conn, "DemoDict.A.name", depth=2)
        self.assertEqual(1, dictionary["summary"]["DICT_REF"])
        self.assertIn("DemoTables.demo_user.name", {n["full_id"] for n in dictionary["affected_nodes"]})


if __name__ == "__main__":
    unittest.main()
