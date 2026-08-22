import builtins
import importlib
import sys
import unittest
from unittest import mock


class XlsxOptionalDependencyTest(unittest.TestCase):
    def test_module_imports_without_openpyxl(self):
        import apsgraph.xlsx_export as xlsx_export

        real_import = builtins.__import__

        def block_openpyxl(name, *args, **kwargs):
            if name == "openpyxl" or name.startswith("openpyxl."):
                raise ImportError("No module named 'openpyxl'")
            return real_import(name, *args, **kwargs)

        original = sys.modules[xlsx_export.__name__]
        try:
            with mock.patch("builtins.__import__", side_effect=block_openpyxl):
                module = importlib.reload(xlsx_export)

            self.assertFalse(module.HAS_OPENPYXL)
            self.assertIsNone(module._TITLE_FILL)
            self.assertIsNone(module._THIN_BORDER)
            with self.assertRaisesRegex(ImportError, "openpyxl is required"):
                module.export_excel(object(), "/unused")
        finally:
            sys.modules[xlsx_export.__name__] = original
            importlib.reload(xlsx_export)


if __name__ == "__main__":
    unittest.main()
