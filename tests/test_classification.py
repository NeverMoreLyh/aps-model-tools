import tempfile
import unittest
from pathlib import Path

from aps_model_tools.classification import audit_capabilities, classify_text, render_markdown
from aps_model_tools.scanner import scan_workspace


class ClassificationTest(unittest.TestCase):
    def test_audit_flags_multifunction_model_and_classifies_java_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in {
                "ap-parent/ap-base/src/main/resources/type/ApCommonType.c_schema.xml":
                    '<schema id="ApCommonType"><complexType id="FileTransferRequest" longname="文件传输请求"/>'
                    '<complexType id="DataCleanResult" longname="数据清理结果"/></schema>',
                "ap-parent/ap-base/src/main/java/cn/demo/security/EncryptTool.java":
                    'package cn.demo.security; public class EncryptTool {}',
            }.items():
                path = root / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
            db = root / "index.db"; scan_workspace(root, db)
            report = audit_capabilities(db, root)
            common = next(item for item in report["model_files"] if item["model_id"] == "ApCommonType")
            self.assertEqual({"file-transfer", "data-clean"}, set(common["capabilities"]))
            self.assertTrue(common["mixed_capabilities"])
            self.assertEqual("security", report["java_packages"][0]["primary_capability"])
            self.assertEqual([], classify_text("cn.demo.spring.engine"))
            self.assertEqual([], classify_text("MappingService ShippingAddress ShoppingCart spinning"))
            markdown = render_markdown(report)
            self.assertIn("ApCommonType", markdown)
            self.assertIn("建议目标", markdown)
    def test_classification_rejects_index_from_another_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / "source"; other = root / "other"
            model = source / "x/Model.c_schema.xml"; model.parent.mkdir(parents=True)
            model.write_text('<schema id="Model"><complexType id="X"/></schema>')
            other.mkdir()
            db = root / "index.db"; scan_workspace(source, db)
            with self.assertRaisesRegex(ValueError, "another workspace"):
                audit_capabilities(db, other)


if __name__ == "__main__": unittest.main()
