import tempfile
import unittest
from pathlib import Path

from aps_model_tools.scanner import scan_workspace, sync_workspace, workspace_status
from aps_model_tools.store import connect, get_stats
from tests.test_scanner import FIXTURE_FILES


class IncrementalV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.db = Path(self.tmp.name) / "models.db"
        for relative, content in FIXTURE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_contains_edges_are_preserved(self):
        duplicate = self.root / "type/Duplicate.c_schema.xml"
        duplicate.parent.mkdir(parents=True, exist_ok=True)
        duplicate.write_text("""<schema id="Duplicate"><complexType id="T"><element id="same" type="string"/><element id="same" type="string"/></complexType></schema>""", encoding="utf-8")
        scan_workspace(self.root, self.db)
        conn = connect(self.db, read_only=True)
        owner = conn.execute("select id from nodes where full_id='Duplicate.T'").fetchone()[0]
        self.assertEqual(2, conn.execute("select count(*) from edges where from_node_id=? and relation_kind='CONTAINS' and evidence_value='Duplicate.T.same'", (owner,)).fetchone()[0])
        conn.close()

    def test_v2_schema_uses_integer_edge_references_and_file_ids(self):
        scan_workspace(self.root, self.db)
        conn = connect(self.db, read_only=True)
        edge_columns = {row[1]: row[2] for row in conn.execute("pragma table_info(edges)")}
        node_columns = {row[1]: row[2] for row in conn.execute("pragma table_info(nodes)")}
        self.assertEqual("INTEGER", edge_columns["from_node_id"])
        self.assertEqual("INTEGER", edge_columns["to_node_id"])
        self.assertEqual("INTEGER", node_columns["file_id"])
        self.assertNotIn("evidence_path", edge_columns)
        conn.close()

    def test_status_reports_pending_changes_without_writing_database(self):
        scan_workspace(self.root, self.db)
        before = self.db.stat().st_mtime_ns
        modified = self.root / "datatype/Base.u_schema.xml"
        modified.write_text(modified.read_text(encoding="utf-8").replace('maxLength="40"', 'maxLength="41"'), encoding="utf-8")
        status = workspace_status(self.root, self.db)
        self.assertEqual(0, status.added)
        self.assertEqual(1, status.modified)
        self.assertEqual(0, status.deleted)
        self.assertFalse(status.up_to_date)
        self.assertEqual(before, self.db.stat().st_mtime_ns)

    def test_sync_skips_unchanged_files(self):
        scan_workspace(self.root, self.db)
        summary = sync_workspace(self.root, self.db)
        self.assertEqual(0, summary.added)
        self.assertEqual(0, summary.modified)
        self.assertEqual(0, summary.deleted)
        self.assertEqual(len(FIXTURE_FILES), summary.unchanged)

    def test_sync_adds_modifies_and_deletes_files(self):
        scan_workspace(self.root, self.db)
        deleted = self.root / "dict/DemoDict.d_schema.xml"
        deleted.unlink()
        modified = self.root / "datatype/Base.u_schema.xml"
        modified.write_text(modified.read_text(encoding="utf-8").replace('maxLength="40"', 'maxLength="41"'), encoding="utf-8")
        added = self.root / "datatype/Added.u_schema.xml"
        added.write_text('<schema id="Added"><restrictionType id="U_CODE" base="string" maxLength="8"/></schema>', encoding="utf-8")

        summary = sync_workspace(self.root, self.db)
        self.assertEqual(1, summary.added)
        self.assertEqual(1, summary.modified)
        self.assertEqual(1, summary.deleted)
        conn = connect(self.db, read_only=True)
        paths = {row[0] for row in conn.execute("select path from model_files")}
        self.assertIn("datatype/Added.u_schema.xml", paths)
        self.assertNotIn("dict/DemoDict.d_schema.xml", paths)
        self.assertEqual([], conn.execute("select 1 from nodes where full_id='DemoDict.A.name'").fetchall())
        self.assertEqual(1, conn.execute("select count(*) from nodes where full_id='Base.U_NAME'").fetchone()[0])
        conn.close()

    def test_sync_rolls_back_all_file_changes_on_unexpected_failure(self):
        from unittest.mock import patch
        import aps_model_tools.scanner as scanner
        scan_workspace(self.root, self.db)
        before = connect(self.db, read_only=True)
        before_counts = get_stats(before)
        before.close()
        target = self.root / "datatype/Base.u_schema.xml"
        target.write_text(target.read_text(encoding="utf-8").replace('maxLength="40"', 'maxLength="41"'), encoding="utf-8")
        original = scanner._parse_file
        def fail_after_delete(conn, workspace, path, suffix):
            original(conn, workspace, path, suffix)
            raise RuntimeError("boom")
        with patch.object(scanner, "_parse_file", side_effect=fail_after_delete):
            with self.assertRaises(RuntimeError):
                sync_workspace(self.root, self.db)
        after = connect(self.db, read_only=True)
        self.assertEqual(before_counts, get_stats(after))
        old_props = after.execute("select properties_json from nodes where full_id='Base.U_NAME'").fetchone()[0]
        self.assertIn('40', old_props)
        after.close()

    def test_sync_parse_failure_replaces_only_changed_file(self):
        scan_workspace(self.root, self.db)
        target = self.root / "tables/Demo.tables.xml"
        target.write_text("<schema", encoding="utf-8")
        summary = sync_workspace(self.root, self.db)
        self.assertEqual(1, summary.modified)
        conn = connect(self.db, read_only=True)
        self.assertEqual("PARSE_FAILED", conn.execute("select parse_status from model_files where path='tables/Demo.tables.xml'").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from nodes where file_id=(select id from model_files where path='tables/Demo.tables.xml')").fetchone()[0])
        self.assertGreater(get_stats(conn)["nodes"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
