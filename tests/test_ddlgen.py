"""Regression tests for ddl-gen (multi-dialect DDL)."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from apsgraph.ddlgen import DEFAULT_FACETS, DdlGenConfig, LENGTH_TYPES, TYPE_BASE, generate_all_ddl
from apsgraph.scanner import import_jar_models, scan_workspace
from apsgraph.store import connect

WORKSPACE_FILES = {
    "datatype/Base.u_schema.xml": """<?xml version="1.0"?>
<schema id="Base" package="demo.datatype">
  <restrictionType id="U_NAME" base="string" maxLength="40"/>
  <restrictionType id="U_STATUS" base="string" maxLength="1">
    <enumeration id="O" value="O" longname="正常"/>
    <enumeration id="D" value="D" longname="删除"/>
  </restrictionType>
  <restrictionType id="U_ID" base="long"/>
</schema>
""",
    # Framework-style base type that only exists in a dependency jar.
    "tables/Demo.tables.xml": """<?xml version="1.0"?>
<schema id="DemoTables" package="demo.tables">
  <table id="demo_user" name="demo_user" longname="演示用户表">
    <fields>
      <field id="id" type="Base.U_ID" primarykey="true" nullable="false"/>
      <field id="name" type="Base.U_NAME" nullable="false"/>
      <field id="status" type="Base.U_STATUS" nullable="false" default="#O"/>
      <field id="memo" type="KBaseType.U_LONG_TEXT"/>
    </fields>
    <indexes><index id="idx_demo_name" type="index" fields="name"/></indexes>
    <dbSequence id="demo_user_seq" startWith="1" incrementBy="1" cache="20" cycle="false" maxValue="99999"/>
  </table>
  <table id="demo_abs" name="demo_abs" abstract="true">
    <fields><field id="x" type="string"/></fields>
  </table>
</schema>
""",
}

JAR_FILES = {
    "datatype/KBaseType.u_schema.xml": """<?xml version="1.0"?>
<schema id="KBaseType" package="framework.datatype">
  <restrictionType id="U_LONG_TEXT" base="string" maxLength="2000"/>
</schema>
""",
}

# 分布式方言与 RANGE 分区的专用表：覆盖 yyyymmdd 字符串、dateTime、timestamp、
# 整数主键与普通唯一索引（分片键约束告警）
DIST_FILES = {
    "tables/Dist.tables.xml": """<?xml version="1.0"?>
<schema id="DistTables" package="demo.dist">
  <restrictionType id="U_DATE8" base="dateString" maxLength="8"/>
  <table id="dist_order" name="dist_order">
    <fields>
      <field id="rec_id" type="Base.U_ID" primarykey="true" nullable="false"/>
      <field id="acct_no" type="Base.U_NAME" primarykey="true" nullable="false"/>
      <field id="acct_date" type="DistTables.U_DATE8" nullable="false"/>
      <field id="create_time" type="dateTime" nullable="false"/>
      <field id="upd_stamp" type="timestamp"/>
      <field id="amt" type="amount"/>
    </fields>
    <indexes>
      <index id="uq_acct" type="unique" fields="acct_no"/>
      <index id="idx_date" type="index" fields="acct_date"/>
    </indexes>
  </table>
</schema>
""",
}


class DdlGenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for relative, content in WORKSPACE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.db = self.root / "models.db"
        scan_workspace(self.root, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_supported_base_type_mappings_match_aps_generator(self):
        self.assertEqual(TYPE_BASE["mysql"]["string"], "varchar")
        self.assertEqual(TYPE_BASE["mysql"]["long"], "bigint")
        self.assertEqual(TYPE_BASE["mysql"]["blob"], "blob")
        self.assertEqual(TYPE_BASE["oracle"]["int"], "number")
        self.assertEqual(TYPE_BASE["oracle"]["timeString17"], "timestamp")
        self.assertEqual(TYPE_BASE["postgresql"]["boolean"], "boolean")
        self.assertEqual(TYPE_BASE["postgresql"]["blob"], "bytea")
        self.assertEqual(DEFAULT_FACETS["mysql"]["amount"],
                         {"maxLength": 20, "fractionDigits": 2})
        self.assertEqual(DEFAULT_FACETS["postgresql"]["decimal"],
                         {"maxLength": 20, "fractionDigits": 2})

    def _generate(self, dialect):
        conn = connect(self.db, read_only=True)
        try:
            return generate_all_ddl(conn, DdlGenConfig(dialect=dialect))
        finally:
            conn.close()

    def test_enum_subset_field_resolves_to_owner_restriction(self):
        # 枚举子集（<subenum>）作为字段类型：与 ddl 命令同一回溯语义
        enum_schema = self.root / "datatype/DemoEnums.e_schema.xml"
        enum_schema.write_text('''<schema id="DemoEnums" package="demo.enum">
          <restrictionType id="E_UNIT" base="string" maxLength="1" longname="期限单位">
            <subenum id="E_UNIT_CZZQ" enums="N,D,W"/>
          </restrictionType>
        </schema>''', encoding="utf-8")
        table = self.root / "tables/EnumRef.tables.xml"
        table.write_text('''<schema id="EnumRef" package="demo.tables">
  <table id="unit" name="unit">
    <fields><field id="reset_prd" type="DemoEnums.E_UNIT.E_UNIT_CZZQ" nullable="true"/></fields>
  </table>
</schema>''', encoding="utf-8")
        scan_workspace(self.root, self.db)
        report = self._generate("mysql")
        self.assertFalse(any("E_UNIT" in e for e in report.errors))
        self.assertIn("`reset_prd` varchar(1)", report.sql)

    def test_unresolved_dependency_type_is_reported_before_import(self):
        report = self._generate("mysql")
        self.assertTrue(any("KBaseType.U_LONG_TEXT" in e for e in report.errors),
                        "missing dependency type must be reported as an error")
        # Field with the missing type is skipped, remaining columns still generated.
        self.assertNotIn("`memo`", report.sql)
        self.assertIn("`name`", report.sql)

    def test_import_jars_resolves_dependency_types(self):
        jar_path = self.root / "kbase.jar"
        with zipfile.ZipFile(jar_path, "w") as archive:
            for name, content in JAR_FILES.items():
                archive.writestr(name, content)
        result = import_jar_models(self.db, [jar_path])
        self.assertEqual(1, result["imported_files"])
        self.assertEqual(0, result["failed_files"])

        report = self._generate("mysql")
        self.assertEqual([], report.errors)
        self.assertEqual(1, report.tables_generated)
        self.assertEqual(1, report.tables_skipped_abstract)
        self.assertIn("create table `demo_user`", report.sql)
        # enum default '#O' resolves to the enumeration value
        self.assertIn("DEFAULT 'O'", report.sql)
        # varchar(2000) >= text threshold 1000 becomes text on mysql
        self.assertIn("text", report.sql)
        self.assertIn("alter table `demo_user` add constraint pk_demo_user", report.sql)
        self.assertIn("create index idx_demo_name", report.sql)
        self.assertIn("ksys_liusdy", report.sql)  # mysql sequence registry convention

    def test_oracle_and_postgresql_dialects(self):
        jar_path = self.root / "kbase.jar"
        with zipfile.ZipFile(jar_path, "w") as archive:
            for name, content in JAR_FILES.items():
                archive.writestr(name, content)
        import_jar_models(self.db, [jar_path])

        oracle = self._generate("oracle")
        self.assertEqual([], oracle.errors)
        self.assertIn("varchar2(40)", oracle.sql)
        self.assertIn("comment on table demo_user", oracle.sql)
        self.assertIn("create sequence demo_user_seq", oracle.sql)
        # varchar(2000) stays varchar2 on oracle (<=4000)
        self.assertIn("varchar2(2000)", oracle.sql)

        pg = self._generate("postgresql")
        self.assertEqual([], pg.errors)
        self.assertIn("create sequence demo_user_seq", pg.sql)
        self.assertIn("comment on column demo_user.status", pg.sql)
        # varchar(2000) >= threshold becomes text on postgresql
        self.assertIn("text", pg.sql)

    def test_imported_jar_files_excluded_from_workspace_sync(self):
        from apsgraph.scanner import workspace_status

        jar_path = self.root / "kbase.jar"
        with zipfile.ZipFile(jar_path, "w") as archive:
            for name, content in JAR_FILES.items():
                archive.writestr(name, content)
        import_jar_models(self.db, [jar_path])

        status = workspace_status(self.root, self.db)
        self.assertTrue(status.up_to_date,
                        "jar-imported files must not appear as workspace deletions/additions")


class DistributedDdlTest(unittest.TestCase):
    """tdsql/goldendb 方言、表分片类型与 RANGE 分区（按日）的回归测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for relative, content in {**WORKSPACE_FILES, **DIST_FILES}.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.db = self.root / "models.db"
        scan_workspace(self.root, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _generate(self, **overrides):
        cfg_kwargs = {"dialect": "mysql"}
        cfg_kwargs.update(overrides)
        conn = connect(self.db, read_only=True)
        try:
            return generate_all_ddl(conn, DdlGenConfig(**cfg_kwargs), ["DistTables.dist_order"])
        finally:
            conn.close()

    def test_distributed_dialects_share_mysql_type_mappings(self):
        self.assertIs(TYPE_BASE["tdsql"], TYPE_BASE["mysql"])
        self.assertIs(TYPE_BASE["goldendb"], TYPE_BASE["mysql"])
        self.assertIs(DEFAULT_FACETS["tdsql"], DEFAULT_FACETS["mysql"])
        self.assertIs(LENGTH_TYPES["goldendb"], LENGTH_TYPES["mysql"])
        for dialect in ("tdsql", "goldendb"):
            report = self._generate(dialect=dialect)
            self.assertEqual([], report.errors)
            self.assertIn("create table `dist_order`", report.sql)
            self.assertIn("ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", report.sql)
            self.assertIn("`acct_no` varchar(40)", report.sql)  # 映射与 mysql 一致
            self.assertIn("alter table `dist_order` add constraint pk_dist_order", report.sql)

    def test_tdsql_distribution_clauses(self):
        shard = self._generate(dialect="tdsql", shard_type="shard", shard_key="acct_no")
        self.assertEqual([], shard.errors)
        self.assertIn(" shardkey=acct_no", shard.sql)
        broadcast = self._generate(dialect="tdsql", shard_type="broadcast")
        self.assertIn(" shardkey=noshardkey_allset", broadcast.sql)
        normal = self._generate(dialect="tdsql", shard_type="normal")
        self.assertNotIn("shardkey", normal.sql)

    def test_goldendb_distribution_clauses(self):
        shard = self._generate(dialect="goldendb", shard_type="shard", shard_key="acct_no",
                               node_groups="g1,g2")
        self.assertIn(" DISTRIBUTED BY HASH(acct_no) (g1,g2)", shard.sql)
        # 节点组括号可选：缺省时只输出 HASH(列)
        bare = self._generate(dialect="goldendb", shard_type="shard", shard_key="acct_no")
        self.assertIn(" DISTRIBUTED BY HASH(acct_no)", bare.sql)
        broadcast = self._generate(dialect="goldendb", shard_type="broadcast",
                                   node_groups="g1,g2,g3,g4")
        self.assertIn(" DISTRIBUTED BY DUPLICATE(g1,g2,g3,g4)", broadcast.sql)
        normal = self._generate(dialect="goldendb", shard_type="normal", node_groups="g1,g2")
        self.assertIn(" DISTRIBUTED BY DUPLICATE(g1)", normal.sql)  # 单节点存储取第一组
        plain = self._generate(dialect="goldendb", shard_type="normal")
        self.assertNotIn("DISTRIBUTED BY", plain.sql)

    def test_distribution_config_fail_closed(self):
        with self.assertRaises(ValueError):
            self._generate(dialect="mysql", shard_type="shard", shard_key="acct_no")
        with self.assertRaises(ValueError):
            self._generate(dialect="tdsql", shard_type="shard")  # 缺分片键
        with self.assertRaises(ValueError):
            self._generate(dialect="tdsql", shard_type="grid")
        with self.assertRaises(ValueError):
            self._generate(dialect="goldendb", shard_type="broadcast")  # 缺节点组
        # 分片键列不存在：fail-closed 报错且不产出该表 DDL
        report = self._generate(dialect="tdsql", shard_type="shard", shard_key="missing_col")
        self.assertEqual("", report.sql)
        self.assertTrue(any("shard key column not found" in e for e in report.errors))

    def test_range_partition_daily_definitions(self):
        # yyyymmdd 字符串列：RANGE COLUMNS 直接比较；分区名=上界-1 天
        report = self._generate(dialect="mysql", create_partition=True,
                                partition_key="acct_date", partition_type="range",
                                partition_start="20260920", partition_end="20260921")
        self.assertEqual([], report.errors)
        self.assertIn(
            "PARTITION BY RANGE COLUMNS (`acct_date`) "
            "(PARTITION p20260920 VALUES LESS THAN ('20260921'), "
            "PARTITION p20260921 VALUES LESS THAN ('20260922'))",
            report.sql)
        self.assertNotIn("pmax", report.sql)  # 指定终止日期时不追加 MAXVALUE
        # 分区键自动追加进主键（MySQL 分区表要求）
        self.assertIn("primary key (`rec_id`, `acct_no`, `acct_date`)", report.sql)

    def test_range_partition_maxvalue_fallback(self):
        report = self._generate(dialect="tdsql", create_partition=True,
                                partition_key="acct_date", partition_start="20260920")
        self.assertIn(
            "PARTITION p20260920 VALUES LESS THAN ('20260921'), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE)",
            report.sql)

    def test_range_partition_conversion_functions(self):
        to_days = self._generate(dialect="mysql", create_partition=True,
                                 partition_key="create_time", partition_start="20260920",
                                 partition_end="20260920")
        self.assertIn(
            "PARTITION BY RANGE (`create_time`) "
            "(PARTITION p20260920 VALUES LESS THAN (TO_DAYS('20260921')))",
            to_days.sql)
        unix = self._generate(dialect="mysql", create_partition=True,
                              partition_key="upd_stamp", partition_start="20260920")
        self.assertIn(
            "PARTITION BY RANGE (`upd_stamp`) "
            "(PARTITION p20260920 VALUES LESS THAN (UNIX_TIMESTAMP('20260921')), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))",
            unix.sql)
        passthrough = self._generate(dialect="mysql", create_partition=True,
                                     partition_key="rec_id", partition_start="20260920")
        self.assertIn(
            "PARTITION BY RANGE (`rec_id`) "
            "(PARTITION p20260920 VALUES LESS THAN (20260921), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))",
            passthrough.sql)

    def test_partition_config_fail_closed(self):
        with self.assertRaises(ValueError):
            self._generate(dialect="mysql", create_partition=True, partition_key="",
                           partition_start="20260920")
        with self.assertRaises(ValueError):
            self._generate(dialect="mysql", create_partition=True, partition_key="acct_date",
                           partition_type="list", partition_start="20260920")
        with self.assertRaises(ValueError):
            self._generate(dialect="mysql", create_partition=True, partition_key="acct_date",
                           partition_start="2026-09-20")  # 非 yyyymmdd
        with self.assertRaises(ValueError):
            self._generate(dialect="mysql", create_partition=True, partition_key="acct_date",
                           partition_start="20260231")  # 非法日历日期
        with self.assertRaises(ValueError):
            self._generate(dialect="mysql", create_partition=True, partition_key="acct_date",
                           partition_start="20260920", partition_end="20260901")
        # 分区键列不存在：报错且不产出该表 DDL
        report = self._generate(dialect="mysql", create_partition=True, partition_key="nope",
                                partition_start="20260920")
        self.assertEqual("", report.sql)
        self.assertTrue(any("partition key column not found" in e for e in report.errors))
        # 不受推荐的分区键类型（decimal）生成时给出警告
        decimal_key = self._generate(dialect="mysql", create_partition=True,
                                     partition_key="amt", partition_start="20260920")
        self.assertTrue(any("primitive type 'amount'" in w for w in decimal_key.warnings))

    def test_oracle_partition_clauses(self):
        # dateTime → oracle date：TO_DATE 界值；分区子句在 tablespace 之前；无反引号
        oracle = self._generate(dialect="oracle", create_partition=True,
                                partition_key="create_time", partition_start="20260919",
                                partition_end="20260920")
        self.assertEqual([], oracle.errors)
        self.assertIn(
            ") PARTITION BY RANGE (create_time) "
            "(PARTITION p20260919 VALUES LESS THAN (TO_DATE('20260920','YYYYMMDD')), "
            "PARTITION p20260920 VALUES LESS THAN (TO_DATE('20260921','YYYYMMDD')));",
            oracle.sql)
        timestamp_key = self._generate(dialect="oracle", create_partition=True,
                                       partition_key="upd_stamp", partition_start="20260919")
        self.assertIn(
            "PARTITION BY RANGE (upd_stamp) "
            "(PARTITION p20260919 VALUES LESS THAN (TO_TIMESTAMP('20260920','YYYYMMDD')), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))",
            timestamp_key.sql)
        # dateString → oracle varchar2：字符串字面量直接比较（yyyymmdd 字典序即日期序）
        string_key = self._generate(dialect="oracle", create_partition=True,
                                    partition_key="acct_date", partition_start="20260919")
        self.assertIn(
            "PARTITION BY RANGE (acct_date) "
            "(PARTITION p20260919 VALUES LESS THAN ('20260920'), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))",
            string_key.sql)
        # long → oracle number：整数直接比较；分区键追加进主键
        number_key = self._generate(dialect="oracle", create_partition=True,
                                    partition_key="rec_id", partition_start="20260919",
                                    partition_end="20260919")
        self.assertIn(
            "PARTITION BY RANGE (rec_id) "
            "(PARTITION p20260919 VALUES LESS THAN (20260920))",
            number_key.sql)
        # rec_id 已在主键中，不重复追加
        self.assertIn("add constraint pk_dist_order primary key (rec_id, acct_no)",
                      number_key.sql)

    def test_oracle_virtual_table_cannot_be_partitioned(self):
        virtual_schema = self.root / "tables/Virtual.tables.xml"
        virtual_schema.write_text('''<schema id="Virtual" package="demo.dist">
  <table id="virt_tab" name="virt_tab" virtual="true">
    <fields><field id="id" type="Base.U_ID" primarykey="true" nullable="false"/></fields>
  </table>
</schema>''', encoding="utf-8")
        scan_workspace(self.root, self.db)
        conn = connect(self.db, read_only=True)
        try:
            report = generate_all_ddl(conn, DdlGenConfig(
                dialect="oracle", create_partition=True, partition_key="id",
                partition_start="20260919"), ["Virtual.virt_tab"])
        finally:
            conn.close()
        self.assertEqual("", report.sql)
        self.assertTrue(any("cannot be partitioned" in e for e in report.errors))

    def test_postgresql_partition_statements(self):
        # PG：表尾仅输出分区头，分区定义为独立 PARTITION OF 语句
        pg = self._generate(dialect="postgresql", create_partition=True,
                            partition_key="acct_date", partition_start="20260919",
                            partition_end="20260920")
        self.assertEqual([], pg.errors)
        self.assertIn(") PARTITION BY RANGE (acct_date);", pg.sql)
        self.assertIn(
            "create table dist_order_p20260919 partition of dist_order "
            "for values from ('20260919') to ('20260920');",
            pg.sql)
        self.assertIn(
            "create table dist_order_p20260920 partition of dist_order "
            "for values from ('20260920') to ('20260921');",
            pg.sql)
        self.assertNotIn("pmax", pg.sql)
        self.assertNotIn("partition of dist_order_p20260919", pg.sql)  # 分区表名不嵌套
        # dateTime → PG timestamp：界值带 00:00:00 与横杠日期格式
        ts_key = self._generate(dialect="postgresql", create_partition=True,
                                partition_key="create_time", partition_start="20260919")
        self.assertIn(
            "for values from ('2026-09-19 00:00:00') to ('2026-09-20 00:00:00');",
            ts_key.sql)
        # 终止日期留空：DEFAULT 分区承担 MAXVALUE 兜底语义
        fallback = self._generate(dialect="postgresql", create_partition=True,
                                  partition_key="acct_date", partition_start="20260919")
        self.assertIn(
            "create table dist_order_pmax partition of dist_order default;",
            fallback.sql)
        # 分区键追加进主键（PG 分区表主键必须包含分区键）
        self.assertIn("primary key (rec_id, acct_no, acct_date)", pg.sql)

    def test_shard_key_unique_index_warnings(self):
        # 分片键不在唯一索引/主键中：TDSQL 逐条告警，GoldenDB 告警主键缺失
        tdsql = self._generate(dialect="tdsql", shard_type="shard", shard_key="amt")
        self.assertIn("primary key does not include shard key amt", " ".join(tdsql.warnings))
        self.assertIn("unique index uq_acct does not include shard key amt",
                      " ".join(tdsql.warnings))
        goldendb = self._generate(dialect="goldendb", shard_type="shard", shard_key="amt")
        self.assertIn("shard key amt is not part of the primary key",
                      " ".join(goldendb.warnings))
        # 分片键在唯一索引与主键中：不产生分片键告警
        clean = self._generate(dialect="tdsql", shard_type="shard", shard_key="acct_no")
        self.assertEqual([], [w for w in clean.warnings if "shard key" in w])
        clean_goldendb = self._generate(dialect="goldendb", shard_type="shard",
                                        shard_key="acct_no")
        self.assertEqual([], [w for w in clean_goldendb.warnings if "shard key" in w])


if __name__ == "__main__":
    unittest.main()
