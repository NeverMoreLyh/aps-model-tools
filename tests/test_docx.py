"""doc-export 模块测试。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aps_model_tools.docx import export_document, _bool_str, DocExportReport


class TestBoolStr(unittest.TestCase):
    def test_true_values(self):
        self.assertEqual(_bool_str(True), "是")
        self.assertEqual(_bool_str("true"), "是")
        self.assertEqual(_bool_str("True"), "是")
        self.assertEqual(_bool_str("1"), "是")

    def test_false_values(self):
        self.assertEqual(_bool_str(False), "否")
        self.assertEqual(_bool_str("false"), "否")
        self.assertEqual(_bool_str("0"), "否")

    def test_none(self):
        self.assertEqual(_bool_str(None), "")

    def test_other(self):
        self.assertEqual(_bool_str("custom"), "custom")


class TestExportDocument(unittest.TestCase):
    """用临时 SQLite 索引测试文档导出。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            PRAGMA user_version = 2;
            CREATE TABLE model_files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                suffix TEXT NOT NULL,
                content_hash BLOB NOT NULL,
                parse_status TEXT NOT NULL,
                root_name TEXT,
                model_id TEXT,
                package_name TEXT,
                error_message TEXT
            );
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY,
                stable_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                raw_id TEXT,
                full_id TEXT,
                owner_node_id INTEGER,
                file_id INTEGER NOT NULL,
                xml_tag TEXT,
                properties_json TEXT NOT NULL,
                FOREIGN KEY(owner_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
                FOREIGN KEY(file_id) REFERENCES model_files(id) ON DELETE CASCADE
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY,
                from_node_id INTEGER NOT NULL,
                to_node_id INTEGER,
                raw_target TEXT,
                relation_kind TEXT NOT NULL,
                evidence_file_id INTEGER NOT NULL,
                evidence_value TEXT,
                confidence TEXT NOT NULL,
                FOREIGN KEY(from_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
                FOREIGN KEY(to_node_id) REFERENCES nodes(id) ON DELETE SET NULL,
                FOREIGN KEY(evidence_file_id) REFERENCES model_files(id) ON DELETE CASCADE
            );
            CREATE TABLE scan_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                workspace TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                scanner_version TEXT NOT NULL
            );
        """)

        # 插入文件
        conn.execute("INSERT INTO model_files (id, path, suffix, content_hash, parse_status) VALUES (1, 'test.tables.xml', '.tables.xml', x'00', 'ok')")
        conn.execute("INSERT INTO model_files (id, path, suffix, content_hash, parse_status) VALUES (2, 'test.d_schema.xml', '.d_schema.xml', x'00', 'ok')")
        conn.execute("INSERT INTO model_files (id, path, suffix, content_hash, parse_status) VALUES (3, 'test.flowtrans.xml', '.flowtrans.xml', x'00', 'ok')")

        # 插入 TABLE 节点
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (1, 'file::/table[1]', 'TABLE', 'TestTable.t_order', NULL, 1, 'table',
            '{"id":"t_order","name":"t_order","longname":"订单表","description":"订单数据","tableType":"ORDINARY","abstract":"false","virtual":"false"}')""")

        # FIELDS 容器
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (2, 'file::/table[1]/fields[1]', 'FIELDS', '', 1, 1, 'fields', '{}')""")

        # FIELD 子节点
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (3, 'file::/table[1]/fields[1]/field[1]', 'FIELD', 't_order.id', 2, 1, 'field',
            '{"id":"order_id","longname":"订单ID","type":"BaseType.U_LONG","nullable":"false","primarykey":"true"}')""")

        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (4, 'file::/table[1]/fields[1]/field[2]', 'FIELD', 't_order.amount', 2, 1, 'field',
            '{"id":"amount","longname":"金额","type":"BaseType.U_AMOUNT","nullable":"true","primarykey":"false","maxLength":"20","fractionDigits":"2"}')""")

        # INDEXES 容器
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (5, 'file::/table[1]/indexes[1]', 'INDEXES', '', 1, 1, 'indexes', '{}')""")

        # INDEX 子节点
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (6, 'file::/table[1]/indexes[1]/index[1]', 'INDEX', 't_order.pk1', 5, 1, 'index',
            '{"id":"pk1","type":"primarykey","fields":"order_id"}')""")

        # RESTRICTION_TYPE 节点
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (7, 'file::/schema[1]/restrictionType[1]', 'RESTRICTION_TYPE', 'BpDict.A.status', NULL, 2, 'restrictionType',
            '{"id":"E_STATUS","longname":"状态","base":"BaseType.U_1_BYTE_ENUM","byCharacter":"true"}')""")

        # ENUM_VALUE 子节点
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (8, 'file::/schema[1]/restrictionType[1]/enumeration[1]', 'ENUM_VALUE', 'BpDict.A.status.active', 7, 2, 'enumeration',
            '{"id":"01","longname":"正常","value":"1"}')""")

        # TRANSACTION 节点
        conn.execute("""INSERT INTO nodes (id, stable_id, kind, full_id, owner_node_id, file_id, xml_tag, properties_json)
            VALUES (9, 'file::/flowtran[1]', 'TRANSACTION', 'dp0001', NULL, 3, 'flowtran',
            '{"id":"dp0001","longname":"存款交易","kind":"deposit","description":"存款处理","package":"cn.test.dps"}')""")

        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_export_table(self):
        markdown, report = export_document(self.conn, "table")
        self.assertEqual(report.tables_exported, 1)
        self.assertIn("t_order", markdown)
        self.assertIn("订单表", markdown)
        self.assertIn("order_id", markdown)
        self.assertIn("订单ID", markdown)
        self.assertIn("pk1", markdown)
        self.assertIn("primarykey", markdown)

    def test_export_table_field_details(self):
        markdown, _ = export_document(self.conn, "table")
        # 检查字段表头
        self.assertIn("序号", markdown)
        self.assertIn("类型", markdown)
        self.assertIn("长度", markdown)
        self.assertIn("主键", markdown)
        # 检查具体值
        self.assertIn("BaseType.U_LONG", markdown)
        self.assertIn("BaseType.U_AMOUNT", markdown)
        self.assertIn("20", markdown)  # maxLength

    def test_export_dict(self):
        markdown, report = export_document(self.conn, "dict")
        self.assertEqual(report.dicts_exported, 1)
        self.assertIn("E_STATUS", markdown)
        self.assertIn("状态", markdown)
        self.assertIn("BaseType.U_1_BYTE_ENUM", markdown)
        self.assertIn("01", markdown)
        self.assertIn("正常", markdown)

    def test_export_trans(self):
        markdown, report = export_document(self.conn, "trans")
        self.assertEqual(report.trans_exported, 1)
        self.assertIn("dp0001", markdown)
        self.assertIn("存款交易", markdown)
        self.assertIn("deposit", markdown)
        self.assertIn("cn.test.dps", markdown)

    def test_export_all(self):
        markdown, report = export_document(self.conn, "all")
        self.assertEqual(report.tables_exported, 1)
        self.assertEqual(report.dicts_exported, 1)
        self.assertEqual(report.trans_exported, 1)
        self.assertIn("表定义文档", markdown)
        self.assertIn("字典与限制类型文档", markdown)
        self.assertIn("交易流程文档", markdown)

    def test_export_empty(self):
        # nsql 和 service 没有数据
        markdown, report = export_document(self.conn, "nsql")
        self.assertEqual(report.nsqls_exported, 0)
        self.assertEqual(markdown, "")

    def test_bool_str_in_markdown(self):
        markdown, _ = export_document(self.conn, "table")
        self.assertIn("否", markdown)  # abstract=false
        self.assertIn("是", markdown)  # primarykey=true


if __name__ == "__main__":
    unittest.main()
