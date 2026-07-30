import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aps_model_tools.bridge import build_bridge_report
from aps_model_tools.scanner import scan_workspace


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        model = self.workspace / "ap-parent/ap-base/src/main/resources/tables/SysParmTable.tables.xml"
        model.parent.mkdir(parents=True)
        model.write_text(
            '<schema id="SysParmTable" package="cn.sunline.ltts.busi.apbase.tables">'
            '<table id="kapb_txn_log" name="kapb_txn_log" longname="交易日志表"><fields/></table>'
            '</schema>', encoding="utf-8")
        generated = self.workspace / "ap-parent/ap-base/target/gen/cn/sunline/ltts/busi/apbase/tables/SysParmTable.java"
        generated.parent.mkdir(parents=True)
        generated.write_text(
            'package cn.sunline.ltts.busi.apbase.tables;\n'
            'public interface SysParmTable {\n'
            ' @cn.sunline.aps.meta.ConfigType(value="SysParmTable.kapb_txn_log")\n'
            ' interface kapb_txn_log {}\n'
            ' class Kapb_txn_logDao {}\n'
            '}\n', encoding="utf-8")
        self.db = self.root / "models.db"
        scan_workspace(self.workspace, self.db)
        self.codegraph = self.root / "codegraph.db"
        conn = sqlite3.connect(self.codegraph)
        conn.execute("create table nodes(id text primary key,kind text,name text,qualified_name text,file_path text,language text,start_line integer,end_line integer,start_column integer,end_column integer,signature text)")
        conn.execute("insert into nodes values(?,?,?,?,?,?,?,?,?,?,?)", (
            "import:1", "import", "cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_logDao",
            "demo::cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_logDao",
            "ap-base/src/main/java/demo/Consumer.java", "java", 3, 3, 0, 70,
            "import cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_logDao;"))
        conn.execute("insert into nodes values(?,?,?,?,?,?,?,?,?,?,?)", (
            "import:other", "import", "cn.sunline.ltts.busi.apbase.tables.SysParmTable.OtherDao",
            "demo::cn.sunline.ltts.busi.apbase.tables.SysParmTable.OtherDao",
            "ap-base/src/main/java/demo/Unrelated.java", "java", 4, 4, 0, 60,
            "import cn.sunline.ltts.busi.apbase.tables.SysParmTable.OtherDao;"))
        conn.execute("insert into nodes values(?,?,?,?,?,?,?,?,?,?,?)", (
            "import:suffix", "import", "cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_log_rlvcDao",
            "demo::cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_log_rlvcDao",
            "ap-base/src/main/java/demo/Suffix.java", "java", 5, 5, 0, 75,
            "import cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_log_rlvcDao;"))
        conn.commit(); conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_table_model_maps_to_generated_entity_and_dao_and_codegraph_consumer(self):
        report = build_bridge_report(self.db, self.workspace, "SysParmTable.kapb_txn_log",
                                     {"ap-parent": self.codegraph})
        links = {link["java_symbol"]: link for link in report["generated_links"]}
        self.assertIn("cn.sunline.ltts.busi.apbase.tables.SysParmTable.kapb_txn_log", links)
        self.assertIn("cn.sunline.ltts.busi.apbase.tables.SysParmTable.Kapb_txn_logDao", links)
        self.assertEqual("CERTAIN", links["cn.sunline.ltts.busi.apbase.tables.SysParmTable.kapb_txn_log"]["confidence"])
        self.assertEqual("ap-base/src/main/java/demo/Consumer.java", report["code_consumers"][0]["file_path"])
        self.assertEqual("CERTAIN", report["code_consumers"][0]["confidence"])
        self.assertFalse(any(item["file_path"].endswith("Unrelated.java") for item in report["code_consumers"]))
        self.assertFalse(any(item["file_path"].endswith("Suffix.java") for item in report["code_consumers"]))

    def test_codegraph_database_is_opened_immutable_read_only(self):
        before = self.codegraph.read_bytes()
        os.chmod(self.codegraph, 0o444)
        try:
            report = build_bridge_report(self.db, self.workspace, "SysParmTable.kapb_txn_log",
                                         {"ap-parent": self.codegraph})
        finally:
            os.chmod(self.codegraph, 0o644)
        self.assertTrue(report["code_consumers"])
        self.assertEqual(before, self.codegraph.read_bytes())

    def test_unsupported_codegraph_schema_is_reported_without_query_failure(self):
        legacy = self.root / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.execute("create table nodes(id text,kind text,name text,qualified_name text,file_path text,language text)")
        conn.commit(); conn.close()
        report = build_bridge_report(self.db, self.workspace, "SysParmTable.kapb_txn_log", {"legacy": legacy})
        self.assertEqual("UNSUPPORTED_INDEX", report["coverage"]["repositories"][0]["status"])
        self.assertEqual([], report["code_consumers"])

    def test_missing_codegraph_database_is_reported_as_coverage_gap(self):
        report = build_bridge_report(self.db, self.workspace, "SysParmTable.kapb_txn_log",
                                     {"missing": self.root / "missing.db"})
        self.assertEqual("MISSING_INDEX", report["coverage"]["repositories"][0]["status"])
        self.assertEqual([], report["code_consumers"])


if __name__ == "__main__":
    unittest.main()
