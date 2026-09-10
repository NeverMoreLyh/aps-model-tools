import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apsgraph.mcp_server import MetadataGraphMcp, TOOLS
from apsgraph.scanner import scan_workspace


class MetadataGraphMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "mod/src/main/resources").mkdir(parents=True)
        (self.root / "mod/src/main/resources/Demo.tables.xml").write_text(
            '<schema id="Demo"><table id="account" longname="账户表">'
            '<fields><field id="code" longname="账户编码" type="string"/></fields>'
            '</table></schema>', encoding="utf-8")
        self.db = self.root / "models.db"
        scan_workspace(self.root, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tools_list_and_scoped_calls(self):
        server = MetadataGraphMcp(self.db)
        init = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertIsNotNone(init)
        self.assertEqual("apsgraph-metadata", init["result"]["serverInfo"]["name"])
        listed = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIsNotNone(listed)
        self.assertEqual({
            "search_metadata", "find_entity", "find_references", "get_dependencies",
            "get_impact", "find_unresolved_references", "get_entity_source",
        }, {x["name"] for x in listed["result"]["tools"]})
        result = server.dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_metadata", "arguments": {
                "query": "账户", "kinds": ["FIELD"], "project": "mod"
            }},
        })
        self.assertIsNotNone(result)
        self.assertIsNotNone(result)
        payload = result["result"]["structuredContent"]
        self.assertEqual(1, payload["count"])
        self.assertEqual("FIELD", payload["results"][0]["kind"])

    def test_stdio_json_line_protocol(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "find_entity", "arguments": {"query": "Demo.account", "kinds": ["TABLE"]}
            }},
        ]
        proc = subprocess.run(
            [sys.executable, "-m", "apsgraph", "serve-mcp", "--db", str(self.db), "--workspace", str(self.root)],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual([1, 2, 3], [item["id"] for item in responses])
        self.assertEqual(7, len(responses[1]["result"]["tools"]))
        self.assertEqual(1, responses[2]["result"]["structuredContent"]["count"])


if __name__ == "__main__":
    unittest.main()
