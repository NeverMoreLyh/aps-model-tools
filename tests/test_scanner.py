import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apsgraph.scanner import scan_workspace
from apsgraph.store import connect, find_nodes, get_stats, references, search_nodes


FIXTURE_FILES = {
    "datatype/Base.u_schema.xml": """<?xml version="1.0"?>
<schema id="Base" package="demo.datatype">
  <restrictionType id="U_NAME" base="string" maxLength="40"/>
  <restrictionType id="U_NAME_CHILD" base="Base.U_NAME"/>
  <restrictionType id="U_STATUS" base="string" maxLength="1"/>
  <restrictionType id="U_ID" base="long"/>
</schema>
""",
    "dict/DemoDict.d_schema.xml": """<?xml version="1.0"?>
<schema id="DemoDict" package="demo.dict">
  <complexType id="A" dict="true">
    <element id="name" type="Base.U_NAME"/>
  </complexType>
  <complexType id="E" dict="true">
    <element id="status" type="Base.U_STATUS" default="O"/>
  </complexType>
</schema>
""",
    "tables/Demo.tables.xml": """<?xml version="1.0"?>
<schema id="DemoTables" package="demo.tables">
  <table id="audit" name="audit">
    <fields><field id="created_at" type="string" nullable="false"/></fields>
  </table>
  <table id="demo_user" name="demo_user" extension=" DemoTables.audit ">
    <fields>
      <field id="id" type="Base.U_ID" primarykey="true" nullable="false"/>
      <field id="name" type="Base.U_NAME_CHILD" ref="DemoDict.A.name" nullable="false" default="''"/>
      <field id="status" type="Base.U_STATUS" ref="DemoDict.E.status" nullable="false"/>
    </fields>
    <odbindexes><index id="odb1" type="unique" fields="id"/></odbindexes>
    <indexes><index id="idx_demo_name" type="index" fields="name"/></indexes>
  </table>
</schema>
""",
    "broken/Broken.c_schema.xml": "<schema id=\"Broken\"><complexType",
}


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for relative, content in FIXTURE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        # Generated copies must not count as source models.
        copied = self.root / "module/target/classes/datatype/Base.u_schema.xml"
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_text(FIXTURE_FILES["datatype/Base.u_schema.xml"], encoding="utf-8")
        self.db = self.root / "models.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_indexes_nodes_edges_and_parse_failures(self):
        summary = scan_workspace(self.root, self.db)

        self.assertEqual(4, summary.discovered_files)
        self.assertEqual(3, summary.parsed_files)
        self.assertEqual(1, summary.failed_files)
        self.assertGreaterEqual(summary.nodes, 10)
        self.assertGreaterEqual(summary.edges, 8)

        conn = connect(self.db)
        stats = get_stats(conn)
        self.assertEqual(4, stats["files"])
        self.assertEqual(1, stats["parse_failed"])

        base = find_nodes(conn, "Base.U_NAME")
        self.assertEqual(1, len(base))
        incoming = references(conn, base[0]["stable_id"], "in", 1)
        kinds = {edge["relation_kind"] for edge in incoming["edges"]}
        self.assertIn("TYPE_REF", kinds)

        dictionary = find_nodes(conn, "DemoDict.A.name")
        self.assertEqual(1, len(dictionary))
        dict_incoming = references(conn, dictionary[0]["stable_id"], "in", 1)
        self.assertIn("DICT_REF", {e["relation_kind"] for e in dict_incoming["edges"]})

    def test_non_top_level_root_full_id_is_not_duplicated(self):
        root = self.root / "err-root"
        root.mkdir()
        (root / "MdError.error.xml").write_text(
            '<errorConf id="MdError" longname="错误码">'
            '<errors id="Cuce"><error id="E0002" type="error" message="x"/></errors></errorConf>',
            encoding="utf-8",
        )
        db = root / "models.db"
        scan_workspace(root, db)
        conn = connect(db)
        try:
            conf = find_nodes(conn, "MdError")
            self.assertEqual(1, len(conf))
            self.assertEqual("MdError", conf[0]["full_id"])
            err = find_nodes(conn, "MdError.Cuce.E0002")
            self.assertEqual(1, len(err))
            self.assertEqual("ERROR", err[0]["kind"])
        finally:
            conn.close()

    def test_non_model_attributes_do_not_become_unresolved_references(self):
        root = self.root / "non-model"
        root.mkdir()
        (root / "Demo.error.xml").write_text(
            '<errors><error id="E1" type="error" message="failed">'
            '<parameter id="qty" type="BaseType.U_LRG_QTY" ref="数量"/></error></errors>',
            encoding="utf-8",
        )
        (root / "Demo.nsql.xml").write_text(
            '''<sqls id="DemoSql">'''
            '<parameterMap id="map" class="java.util.Map">'
            '<parameter id="systemId" type="systemId"/></parameterMap>'
            '<select id="find" type="sql">select 1</select></sqls>',
            encoding="utf-8",
        )
        db = root / "models.db"
        summary = scan_workspace(root, db)
        self.assertEqual([], summary.unresolved_models)
        conn = connect(db)
        try:
            raw_targets = {row[0] for row in conn.execute("select raw_target from edges")}
        finally:
            conn.close()
        self.assertFalse({"error", "message", "java.util.Map", "systemId", "sql"} & raw_targets)

    def test_fts_search_matches_ids_and_chinese_descriptions(self):
        root = self.root / "search"
        root.mkdir()
        (root / "Account.tables.xml").write_text(
            '<schema id="Account"><table id="account_type" longname="账户类型" '
            'description="开户账户类型"/></schema>', encoding="utf-8")
        db = root / "models.db"
        scan_workspace(root, db)
        conn = connect(db, read_only=True)
        try:
            account = search_nodes(conn, "账户")
            opening = search_nodes(conn, "开户")
            identifier = search_nodes(conn, "account_type")
        finally:
            conn.close()

        self.assertEqual("Account.account_type", account[0]["full_id"])
        self.assertEqual("TABLE", account[0]["kind"])
        self.assertEqual("账户类型", account[0]["chinese_name"])
        self.assertEqual("开户账户类型", account[0]["description"])
        self.assertIn("longname", account[0]["matched_fields"])
        self.assertEqual("description", opening[0]["matched_fields"][0])
        self.assertIn("full_id", identifier[0]["matched_fields"])

    def test_multi_valued_table_extension_creates_one_edge_per_target(self):
        root = self.root / "multi-extension"
        root.mkdir()
        (root / "Demo.tables.xml").write_text(
            '''<schema id="Demo">'''
            '<table id="first"/><table id="second"/>'
            '<table id="both" extension="Demo.first Demo.second"/></schema>',
            encoding="utf-8",
        )
        db = root / "models.db"
        summary = scan_workspace(root, db)
        self.assertEqual(0, summary.unresolved)
        conn = connect(db)
        try:
            raw_targets = {
                row[0] for row in conn.execute(
                    "select raw_target from edges where relation_kind='EXTENDS'"
                )
            }
        finally:
            conn.close()
        self.assertEqual({"Demo.first", "Demo.second"}, raw_targets)

    def test_scan_rejects_missing_workspace(self):
        missing = self.root / "does-not-exist"
        with self.assertRaises(FileNotFoundError):
            scan_workspace(missing, self.db)

    def test_scan_rejects_empty_workspace(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            scan_workspace(empty, self.db)

    def test_scan_is_idempotent(self):
        first = scan_workspace(self.root, self.db)
        second = scan_workspace(self.root, self.db)
        self.assertEqual(first.nodes, second.nodes)
        self.assertEqual(first.edges, second.edges)
        conn = connect(self.db)
        duplicate_count = conn.execute(
            "select count(*) from (select stable_id,count(*) c from nodes group by stable_id having c > 1)"
        ).fetchone()[0]
        self.assertEqual(0, duplicate_count)


if __name__ == "__main__":
    unittest.main()
