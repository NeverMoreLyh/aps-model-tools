"""Regression tests for db-diff (metadata model vs actual database schema comparison)."""
import unittest

from apsgraph.dbdiff import (
    ActualColumn, ActualIndex, ActualTable,
    ExpectedColumn, ExpectedIndex, ExpectedTable,
    canonicalize_dtype, compare_table, normalize_default, run_diff,
)


def _exp_table():
    return ExpectedTable(
        name="demo_user",
        columns=[
            ExpectedColumn("id", "bigint", "BIGINT(16)", length=16, nullable=False),
            ExpectedColumn("name", "string", "VARCHAR(40)", length=40, nullable=False, comment="姓名"),
            ExpectedColumn("amount", "decimal", "DECIMAL(20,2)", precision=20, scale=2),
            ExpectedColumn("status", "string", "CHAR(1)", length=1, default="O"),
            ExpectedColumn("memo", "text", "TEXT"),
        ],
        primary_key=("id",),
        indexes=[ExpectedIndex("idx_demo_name", False, ("name",)),
                 ExpectedIndex("uk_demo_status", True, ("status", "amount"))],
        comment="演示用户表",
    )


def _act_ok():
    return ActualTable(
        name="demo_user",
        columns=[
            ActualColumn("id", "bigint", "bigint", length=16, nullable=False, ordinal=1),
            ActualColumn("name", "string", "varchar", length=40, nullable=False, comment="姓名", ordinal=2),
            ActualColumn("amount", "decimal", "decimal", precision=20, scale=2, ordinal=3),
            ActualColumn("status", "string", "char", length=1, default="O", ordinal=4),
            ActualColumn("memo", "text", "text", ordinal=5),
        ],
        primary_key=("id",),
        indexes=[ActualIndex("idx_demo_name", False, ("name",)),
                 ActualIndex("uk_demo_status", True, ("status", "amount"))],
        comment="演示用户表",
    )


class CanonicalizeTest(unittest.TestCase):
    def test_families(self):
        self.assertEqual("string", canonicalize_dtype("varchar(40)", "mysql")["family"])
        self.assertEqual("text", canonicalize_dtype("text", "mysql")["family"])
        self.assertEqual("text", canonicalize_dtype("clob", "oracle")["family"])
        self.assertEqual("binary", canonicalize_dtype("bytea", "postgresql")["family"])
        self.assertEqual("bool", canonicalize_dtype("boolean", "postgresql")["family"])
        self.assertEqual("bool", canonicalize_dtype("tinyint(1)", "mysql")["family"])
        self.assertEqual("int", canonicalize_dtype("int(11)", "mysql")["family"])
        self.assertEqual("bigint", canonicalize_dtype("bigint(20)", "mysql")["family"])
        self.assertEqual("decimal", canonicalize_dtype("number(20,2)", "oracle")["family"])
        self.assertEqual("datetime", canonicalize_dtype("datetime", "mysql")["family"])
        self.assertEqual("date", canonicalize_dtype("date", "oracle")["family"])
        self.assertEqual("timestamp", canonicalize_dtype("timestamp", "oracle")["family"])

    def test_normalize_default(self):
        self.assertEqual("O", normalize_default("'O'"))
        self.assertEqual("CURRENT_TIMESTAMP", normalize_default("SYSDATE"))
        self.assertEqual("CURRENT_TIMESTAMP", normalize_default("CURRENT_TIMESTAMP"))
        self.assertIsNone(normalize_default(None))
        self.assertIsNone(normalize_default("NULL"))


class CompareTableTest(unittest.TestCase):
    def test_identical_schema_ok(self):
        diff = compare_table("demo_user", _exp_table(), _act_ok())
        self.assertEqual("OK", diff.status)
        self.assertEqual([], diff.errors)
        self.assertEqual([], diff.warnings)

    def test_missing_column_is_error(self):
        act = _act_ok()
        act.columns = [c for c in act.columns if c.name != "memo"]
        diff = compare_table("demo_user", _exp_table(), act)
        kinds = {i.kind for i in diff.errors}
        self.assertIn("column_missing", kinds)
        self.assertEqual("ERROR", diff.status)

    def test_extra_column_is_error(self):
        act = _act_ok()
        act.columns.append(ActualColumn("ghost", "string", "varchar", length=10, ordinal=9))
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("column_extra", {i.kind for i in diff.errors})

    def test_type_mismatch_is_error(self):
        act = _act_ok()
        for c in act.columns:
            if c.name == "name":
                c.family, c.dtype = "int", "int"
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("type_mismatch", {i.kind for i in diff.errors})

    def test_varchar_length_shrink_error_expand_warning(self):
        act = _act_ok()
        for c in act.columns:
            if c.name == "name":
                c.length = 20
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("type_mismatch", {i.kind for i in diff.errors})

        act2 = _act_ok()
        for c in act2.columns:
            if c.name == "name":
                c.length = 80
        diff2 = compare_table("demo_user", _exp_table(), act2)
        self.assertNotIn("type_mismatch", {i.kind for i in diff2.errors})
        self.assertIn("type_variant", {i.kind for i in diff2.warnings})

    def test_nullable_mismatch_is_error(self):
        act = _act_ok()
        for c in act.columns:
            if c.name == "name":
                c.nullable = True
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("nullable_mismatch", {i.kind for i in diff.errors})

    def test_default_mismatch_is_error(self):
        act = _act_ok()
        for c in act.columns:
            if c.name == "status":
                c.default = "D"
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("default_mismatch", {i.kind for i in diff.errors})

    def test_comment_mismatch_is_warning(self):
        act = _act_ok()
        for c in act.columns:
            if c.name == "name":
                c.comment = "客户姓名"
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("comment_mismatch", {i.kind for i in diff.warnings})
        self.assertEqual([], diff.errors)
        self.assertEqual("WARNING", diff.status)

    def test_column_order_is_warning_only(self):
        act = _act_ok()
        act.columns.reverse()
        for i, c in enumerate(act.columns):
            c.ordinal = i + 1
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("column_order", {i.kind for i in diff.warnings})
        self.assertEqual([], diff.errors)

    def test_primary_key_mismatch_is_error(self):
        act = _act_ok()
        act.primary_key = ("id", "name")
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("primary_key_mismatch", {i.kind for i in diff.errors})

    def test_index_missing_is_error(self):
        act = _act_ok()
        act.indexes = [i for i in act.indexes if i.name != "idx_demo_name"]
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("index_missing", {i.kind for i in diff.errors})

    def test_index_extra_is_error(self):
        act = _act_ok()
        act.indexes.append(ActualIndex("idx_ghost", False, ("memo",)))
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("index_extra", {i.kind for i in diff.errors})

    def test_index_renamed_same_columns_is_warning(self):
        act = _act_ok()
        act.indexes = [ActualIndex("idx_demo_name_v2", False, ("name",)),
                       ActualIndex("uk_demo_status", True, ("status", "amount"))]
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("index_renamed", {i.kind for i in diff.warnings})
        self.assertNotIn("index_missing", {i.kind for i in diff.errors})

    def test_index_column_mismatch_is_error(self):
        act = _act_ok()
        act.indexes = [ActualIndex("idx_demo_name", False, ("memo",)),
                       ActualIndex("uk_demo_status", True, ("status", "amount"))]
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("index_columns_mismatch", {i.kind for i in diff.errors})

    def test_index_uniqueness_mismatch_is_error(self):
        act = _act_ok()
        act.indexes = [ActualIndex("idx_demo_name", True, ("name",)),
                       ActualIndex("uk_demo_status", True, ("status", "amount"))]
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("index_type_mismatch", {i.kind for i in diff.errors})

    def test_table_comment_mismatch_is_warning(self):
        act = _act_ok()
        act.comment = "旧注释"
        diff = compare_table("demo_user", _exp_table(), act)
        self.assertIn("table_comment_mismatch", {i.kind for i in diff.warnings})


class RunDiffTest(unittest.TestCase):
    def test_missing_and_extra_tables(self):
        expected = {"demo_user": _exp_table(),
                    "demo_ghost": ExpectedTable(name="demo_ghost")}
        actual = {"demo_user": _act_ok(),
                  "demo_orphan": ActualTable(name="demo_orphan")}
        report = run_diff(expected, actual)
        statuses = {r.table: r.status for r in report["tables"]}
        self.assertEqual("MISSING_TABLE", statuses["demo_ghost"])
        self.assertEqual("EXTRA_TABLE", statuses["demo_orphan"])
        self.assertEqual("OK", statuses["demo_user"])
        self.assertGreater(report["summary"]["total_errors"], 0)

    def test_extra_table_strict_mode_promotes_to_error(self):
        report = run_diff({}, {"demo_orphan": ActualTable(name="demo_orphan")},
                          strict_extra_tables=True)
        r = report["tables"][0]
        self.assertTrue(any(i.severity == "ERROR" for i in r.errors))

    def test_actual_json_roundtrip(self):
        import tempfile
        from pathlib import Path

        from apsgraph.dbdiff import dump_actual_json, load_actual_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actual.json"
            dump_actual_json({"demo_user": _act_ok()}, path, "mysql")
            dialect, tables = load_actual_json(path)
            self.assertEqual("mysql", dialect)
            diff = compare_table("demo_user", _exp_table(), tables["demo_user"])
            self.assertEqual("OK", diff.status)


if __name__ == "__main__":
    unittest.main()
