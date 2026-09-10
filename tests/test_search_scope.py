import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from apsgraph.cli import main
from apsgraph.scanner import scan_workspace
from apsgraph.search_scope import SearchScope
from apsgraph.store import connect, find_nodes, search_nodes


class SearchScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "alpha/src/main/resources").mkdir(parents=True)
        (self.root / "beta/src/main/resources").mkdir(parents=True)
        (self.root / "alpha/src/main/resources/Alpha.tables.xml").write_text(
            '<schema id="Alpha"><table id="account" longname="账户表">'
            '<fields><field id="code" longname="账户编码" type="string"/></fields></table></schema>',
            encoding="utf-8",
        )
        (self.root / "beta/src/main/resources/Beta.c_schema.xml").write_text(
            '<schema id="Beta"><complexType id="account" longname="账户类型">'
            '<element id="code" longname="账户编码" type="string"/></complexType></schema>',
            encoding="utf-8",
        )
        self.db = self.root / "models.db"
        scan_workspace(self.root, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_scope_filters_kind_project_module_path_and_top_level(self):
        conn = connect(self.db, read_only=True)
        try:
            self.assertEqual(1, len(search_nodes(conn, "账户", scope=SearchScope.from_values(kinds=["TABLE"]))))
            self.assertEqual(2, len(search_nodes(conn, "账户", scope=SearchScope.from_values(project="alpha"))))
            self.assertEqual(2, len(search_nodes(conn, "账户", scope=SearchScope.from_values(module="beta"))))
            self.assertEqual(2, len(search_nodes(conn, "账户", scope=SearchScope.from_values(path="beta/**"))))
            top = search_nodes(conn, "账户", scope=SearchScope.from_values(top_level=True))
            self.assertEqual([], top)
        finally:
            conn.close()

    def test_find_scope_filters_exact_duplicate_raw_id(self):
        conn = connect(self.db, read_only=True)
        try:
            self.assertEqual(2, len(find_nodes(conn, "account")))
            tables = find_nodes(conn, "account", SearchScope.from_values(kinds=["TABLE"]))
            self.assertEqual(1, len(tables))
            self.assertEqual("TABLE", tables[0]["kind"])
        finally:
            conn.close()

    def test_cli_search_and_find_return_scope(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["search", "账户", "--db", str(self.db), "--kind", "TABLE", "--project", "alpha"]))
        result = json.loads(output.getvalue())
        self.assertEqual(["TABLE"], result["scope"]["kinds"])
        self.assertEqual("alpha", result["scope"]["project"])
        self.assertEqual(1, result["count"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["find", "account", "--db", str(self.db), "--kind", "TABLE"]))
        result = json.loads(output.getvalue())
        self.assertEqual(1, result["count"])
        self.assertEqual("TABLE", result["results"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
