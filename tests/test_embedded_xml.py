import tempfile
import unittest
from pathlib import Path

from apsgraph.scanner import scan_workspace
from apsgraph.store import connect


class EmbeddedXmlTest(unittest.TestCase):
    def test_scan_can_embed_source_xml_without_changing_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Demo.c_schema.xml"
            text = '<schema id="Demo"><complexType id="T"/></schema>\n'
            source.write_text(text, encoding="utf-8")
            db = root / "index.db"

            summary = scan_workspace(root, db, embed_xml=True)
            self.assertEqual(1, summary.parsed_files)
            conn = connect(db, read_only=True)
            try:
                row = conn.execute("select content,content_encoding,content_size from xml_documents").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(text.encode(), row["content"])
                self.assertEqual("UTF-8", row["content_encoding"])
                self.assertEqual(len(text.encode()), row["content_size"])
            finally:
                conn.close()

    def test_scan_default_does_not_embed_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Demo.c_schema.xml").write_text('<schema id="Demo"/>', encoding="utf-8")
            db = root / "index.db"
            scan_workspace(root, db)
            conn = connect(db, read_only=True)
            try:
                self.assertEqual(0, conn.execute("select count(*) from xml_documents").fetchone()[0])
            finally:
                conn.close()
