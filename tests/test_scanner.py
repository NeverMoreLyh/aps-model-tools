import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aps_model_tools.scanner import scan_workspace
from aps_model_tools.store import connect, find_nodes, get_stats, references


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
  <table id="demo_user" name="demo_user" extension="DemoTables.audit">
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
