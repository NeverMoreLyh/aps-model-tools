"""Regression tests for ddl-gen (multi-dialect DDL) and import-jars (Maven dependency models)."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from apsgraph.ddlgen import DdlGenConfig, generate_all_ddl
from apsgraph.scanner import import_jar_models, scan_workspace
from apsgraph.store import connect

WORKSPACE_FILES = {
    "datatype/Base.u_schema.xml": """<?xml version="1.0"?>
<schema id="Base" package="demo.datatype">
  <restrictionType id="U_NAME" base="string" maxLength="40"/>
  <restrictionType id="U_STATUS" base="string" maxLength="1">
    <enumeration id="O" value="O" longname="正常"/>
    <enumeration id="D" value="D" longname="删除"/>
  </restrictionType>
  <restrictionType id="U_ID" base="long"/>
</schema>
""",
    # Framework-style base type that only exists in a dependency jar.
    "tables/Demo.tables.xml": """<?xml version="1.0"?>
<schema id="DemoTables" package="demo.tables">
  <table id="demo_user" name="demo_user" longname="演示用户表">
    <fields>
      <field id="id" type="Base.U_ID" primarykey="true" nullable="false"/>
      <field id="name" type="Base.U_NAME" nullable="false"/>
      <field id="status" type="Base.U_STATUS" nullable="false" default="#O"/>
      <field id="memo" type="KBaseType.U_LONG_TEXT"/>
    </fields>
    <indexes><index id="idx_demo_name" type="index" fields="name"/></indexes>
    <dbSequence id="demo_user_seq" startWith="1" incrementBy="1" cache="20" cycle="false" maxValue="99999"/>
  </table>
  <table id="demo_abs" name="demo_abs" abstract="true">
    <fields><field id="x" type="string"/></fields>
  </table>
</schema>
""",
}

JAR_FILES = {
    "datatype/KBaseType.u_schema.xml": """<?xml version="1.0"?>
<schema id="KBaseType" package="framework.datatype">
  <restrictionType id="U_LONG_TEXT" base="string" maxLength="2000"/>
</schema>
""",
}


class DdlGenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for relative, content in WORKSPACE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.db = self.root / "models.db"
        scan_workspace(self.root, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _generate(self, dialect):
        conn = connect(self.db, read_only=True)
        try:
            return generate_all_ddl(conn, DdlGenConfig(dialect=dialect))
        finally:
            conn.close()

    def test_unresolved_dependency_type_is_reported_before_import(self):
        report = self._generate("mysql")
        self.assertTrue(any("KBaseType.U_LONG_TEXT" in e for e in report.errors),
                        "missing dependency type must be reported as an error")
        # Field with the missing type is skipped, remaining columns still generated.
        self.assertNotIn("`memo`", report.sql)
        self.assertIn("`name`", report.sql)

    def test_import_jars_resolves_dependency_types(self):
        jar_path = self.root / "kbase.jar"
        with zipfile.ZipFile(jar_path, "w") as archive:
            for name, content in JAR_FILES.items():
                archive.writestr(name, content)
        result = import_jar_models(self.db, [jar_path])
        self.assertEqual(1, result["imported_files"])
        self.assertEqual(0, result["failed_files"])

        report = self._generate("mysql")
        self.assertEqual([], report.errors)
        self.assertEqual(1, report.tables_generated)
        self.assertEqual(1, report.tables_skipped_abstract)
        self.assertIn("create table `demo_user`", report.sql)
        # enum default '#O' resolves to the enumeration value
        self.assertIn("DEFAULT 'O'", report.sql)
        # varchar(2000) >= text threshold 1000 becomes text on mysql
        self.assertIn("text", report.sql)
        self.assertIn("alter table `demo_user` add constraint pk_demo_user", report.sql)
        self.assertIn("create index idx_demo_name", report.sql)
        self.assertIn("ksys_liusdy", report.sql)  # mysql sequence registry convention

    def test_oracle_and_postgresql_dialects(self):
        jar_path = self.root / "kbase.jar"
        with zipfile.ZipFile(jar_path, "w") as archive:
            for name, content in JAR_FILES.items():
                archive.writestr(name, content)
        import_jar_models(self.db, [jar_path])

        oracle = self._generate("oracle")
        self.assertEqual([], oracle.errors)
        self.assertIn("varchar2(40)", oracle.sql)
        self.assertIn("comment on table demo_user", oracle.sql)
        self.assertIn("create sequence demo_user_seq", oracle.sql)
        # varchar(2000) stays varchar2 on oracle (<=4000)
        self.assertIn("varchar2(2000)", oracle.sql)

        pg = self._generate("postgresql")
        self.assertEqual([], pg.errors)
        self.assertIn("create sequence demo_user_seq", pg.sql)
        self.assertIn("comment on column demo_user.status", pg.sql)
        # varchar(2000) >= threshold becomes text on postgresql
        self.assertIn("text", pg.sql)

    def test_imported_jar_files_excluded_from_workspace_sync(self):
        from apsgraph.scanner import workspace_status

        jar_path = self.root / "kbase.jar"
        with zipfile.ZipFile(jar_path, "w") as archive:
            for name, content in JAR_FILES.items():
                archive.writestr(name, content)
        import_jar_models(self.db, [jar_path])

        status = workspace_status(self.root, self.db)
        self.assertTrue(status.up_to_date,
                        "jar-imported files must not appear as workspace deletions/additions")


if __name__ == "__main__":
    unittest.main()
