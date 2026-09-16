import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from apsgraph.scanner import scan_workspace
from apsgraph.store import connect
from apsgraph.workbench import (
    WorkbenchServer,
    child_nodes,
    enum_groups,
    node_detail,
    search_group,
    serve_workbench,
    top_groups,
    validate_ddl_sql,
)


FIXTURE_FILES = {
    "datatype/Base.u_schema.xml": """<?xml version="1.0"?>
<schema id="Base" package="demo.datatype">
  <restrictionType id="U_NAME" base="string" maxLength="40"/>
  <restrictionType id="U_STATUS" base="string" maxLength="1">
    <enumeration id="A" value="A" longname="有效"/>
    <enumeration id="I" value="I" longname="无效"/>
  </restrictionType>
</schema>
""",
    "dict/DemoDict.d_schema.xml": """<?xml version="1.0"?>
<schema id="DemoDict" package="demo.dict">
  <complexType id="CustomerInfo" dict="true" longname="客户信息">
    <element id="name" type="Base.U_NAME"/>
    <element id="status" type="Base.U_STATUS"/>
  </complexType>
</schema>
""",
    "tables/Demo.tables.xml": """<?xml version="1.0"?>
<schema id="DemoTables" package="demo.tables">
  <table id="audit" name="audit" longname="公共审计字段">
    <fields><field id="created_at" type="string" nullable="false"/></fields>
  </table>
  <table id="base_cols" name="base_cols" longname="公共基础字段">
    <fields><field id="org_id" type="string"/></fields>
  </table>
  <table id="demo_user" name="demo_user" longname="用户表" extension=" DemoTables.audit DemoTables.base_cols ">
    <fields>
      <field id="id" type="Base.U_NAME" primarykey="true" nullable="false"/>
      <field id="status" type="Base.U_STATUS" ref="DemoDict.CustomerInfo.name"/>
    </fields>
    <indexes><index id="idx_status" type="index" fields="status"/></indexes>
    <odbindexes><index id="odb_id" type="unique" fields="id" operate="selectOne deleteOne"/></odbindexes>
    <dbSequence id="seq_demo"/>
  </table>
</schema>
""",
    "svc/DemoSvc.serviceType.xml": """<?xml version="1.0"?>
<serviceType id="DemoSvc" longname="演示服务" package="demo.svc">
  <service id="openAccount" longname="开户">
    <interface>
      <input><fields><field id="custName" type="Base.U_NAME"/></fields></input>
      <output><fields><field id="result" type="string"/></fields></output>
    </interface>
  </service>
</serviceType>
""",
    "ctype/DemoCType.c_schema.xml": """<?xml version="1.0"?>
<schema id="DemoCType" package="demo.ctype">
  <complexType id="CustPojo" longname="客户POJO">
    <element id="extra" type="Base.U_NAME"/>
  </complexType>
</schema>
""",
    "tran/demoTran.flowtrans.xml": """<?xml version="1.0"?>
<flowtran id="demoTran" longname="演示交易" package="demo.tran">
  <interface>
    <input><fields><field id="in1" type="string"/></fields></input>
    <output><fields><field id="out1" type="string"/></fields></output>
  </interface>
  <flow>
    <method id="chk" method="checkInput" longname="输入检查"/>
    <case id="byType" longname="按类型分支">
      <when id="whenA" longname="类型A" test="type == 'A'">
        <service id="step_open" serviceName="DemoSvc.openAccount" longname="调用开户"/>
      </when>
    </case>
    <service id="step_tran" transactionId="missingTran"/>
  </flow>
</flowtran>
""",
    "batch/demoBatch.batch_tran.xml": """<?xml version="1.0"?>
<batch_transaction id="demoBatch" longname="演示批量" package="demo.batch">
  <fields><field id="batchIn" type="string" fixedValue="X"/></fields>
</batch_transaction>
""",
    "batch/step.batchStep.xml": """<?xml version="1.0"?>
<batchStepGroup id="demoStep" longname="演示批量步骤" package="demo.batch"/>
""",
    # 真实工程 .error.xml 为 errorConf 根，errors 分组下挂 error 明细
    "err/MdError.error.xml": """<?xml version="1.0"?>
<errorConf id="MdError" longname="介质错误码定义">
  <errors id="Cuce" longname="客户凭证错误信息">
    <error id="E0002" type="error" message="凭证种类不存在">
      <parameter id="vchr_catg" type="Base.U_NAME" longname="凭证种类"/>
    </error>
    <error id="E0003" type="error" message="密码错误次数已超限"/>
  </errors>
</errorConf>
""",
}


class WorkbenchTestBase(unittest.TestCase):
    def build_index(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for relative, content in FIXTURE_FILES.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        db = root / "models.db"
        scan_workspace(root, db)
        return root, db


class WorkbenchQueryTest(WorkbenchTestBase):
    def setUp(self):
        self.root, self.db = self.build_index()
        self.conn = connect(self.db, read_only=True)
        self.addCleanup(self.conn.close)

    def test_search_by_each_dimension(self):
        by_id = search_group(self.conn, "table", query="demo_user", dimension="id")
        self.assertEqual(1, by_id["total"])
        self.assertEqual("demo_user", by_id["results"][0]["raw_id"])

        by_full = search_group(self.conn, "table", query="DemoTables.demo_user", dimension="fullid")
        self.assertEqual(1, by_full["total"])

        by_longname = search_group(self.conn, "table", query="用户", dimension="longname")
        self.assertEqual(1, by_longname["total"])
        self.assertEqual("用户表", by_longname["results"][0]["chinese_name"])

        # description dimension: no table carries a description; the search must
        # not silently fall back to other columns.
        by_desc = search_group(self.conn, "table", query="用户表", dimension="desc")
        self.assertEqual(0, by_desc["total"])

        all_dims = search_group(self.conn, "table", query="demo_user")
        self.assertEqual(1, all_dims["total"])

    def test_substring_like_search_with_escaped_wildcards(self):
        # 部分英文子串（含下划线）必须按字面匹配，不能当通配符
        substring = search_group(self.conn, "table", query="o_user", dimension="id")
        self.assertEqual(1, substring["total"])
        self.assertEqual("demo_user", substring["results"][0]["raw_id"])

    def test_group_kind_filters(self):
        tables = search_group(self.conn, "table")
        self.assertGreaterEqual(tables["total"], 1)
        self.assertTrue(all(item["kind"] == "TABLE" for item in tables["results"]))

        enums = search_group(self.conn, "enum", query="有效")
        self.assertEqual(1, enums["total"])
        self.assertEqual("有效", enums["results"][0]["chinese_name"])
        self.assertEqual("Base.U_STATUS", enums["results"][0]["owner_full_id"])

        operations = search_group(self.conn, "service_operation", query="openAccount", dimension="id")
        self.assertEqual(1, operations["total"])
        self.assertEqual("DemoSvc.openAccount", operations["results"][0]["full_id"])

        # 字典数据项只收字典文件（.d_schema.xml 且父为 DICTIONARY）的 element
        elements = search_group(self.conn, "dict_element", query="CustomerInfo.name", dimension="fullid")
        self.assertEqual(1, elements["total"])
        self.assertEqual("DemoDict.CustomerInfo.name", elements["results"][0]["full_id"])
        self.assertEqual(0, search_group(self.conn, "dict_element", query="extra", dimension="id")["total"])

        complex_types = search_group(self.conn, "complex_type", query="CustPojo", dimension="id")
        self.assertEqual(1, complex_types["total"])
        self.assertEqual("DemoCType.CustPojo", complex_types["results"][0]["full_id"])

    def test_dictionary_and_error_code_split_by_suffix(self):
        dictionaries = search_group(self.conn, "dictionary")
        self.assertEqual(1, dictionaries["total"])
        self.assertEqual("DemoDict.CustomerInfo", dictionaries["results"][0]["full_id"])
        self.assertTrue(dictionaries["results"][0]["file_path"].endswith(".d_schema.xml"))

        # 真实工程 .error.xml 为 errorConf（kind ERRORCONF）
        errors = search_group(self.conn, "error_code")
        self.assertEqual(1, errors["total"])
        self.assertEqual("MdError", errors["results"][0]["full_id"])
        self.assertEqual("ERRORCONF", errors["results"][0]["kind"])
        self.assertTrue(errors["results"][0]["file_path"].endswith(".error.xml"))

        # 错误码按 message 模糊搜索（desc 维度包含 message）
        by_message = search_group(self.conn, "error_code", query="密码错误", dimension="desc")
        self.assertEqual(1, by_message["total"])

        # 错误码数据项页：按 GnError.GnError.E0001 这类 full_id 粒度查询 ERROR 节点
        items = search_group(self.conn, "error_item", query="E0002", dimension="id")
        self.assertEqual(1, items["total"])
        self.assertEqual("MdError.Cuce.E0002", items["results"][0]["full_id"])
        self.assertEqual("ERROR", items["results"][0]["kind"])
        self.assertEqual(1, search_group(self.conn, "error_item", query="不存在", dimension="desc")["total"])

        # fullid 维度按 "." 分段层级匹配：省略中间分组段仍可定位
        hierarchical = search_group(self.conn, "error_item",
                                    query="MdError.E0002", dimension="fullid")
        self.assertEqual(1, hierarchical["total"])
        self.assertEqual("MdError.Cuce.E0002", hierarchical["results"][0]["full_id"])

    def test_browse_mode_and_pagination(self):
        page1 = search_group(self.conn, "table", page=1, page_size=1)
        self.assertEqual(3, page1["total"])  # audit / base_cols / demo_user
        self.assertEqual(1, len(page1["results"]))
        self.assertEqual("TABLE", page1["results"][0]["kind"])

    def test_batch_kind_toggle(self):
        main_batch = search_group(self.conn, "batch")
        self.assertEqual(1, main_batch["total"])
        self.assertEqual("demoBatch", main_batch["results"][0]["raw_id"])

        steps = search_group(self.conn, "batch", kinds=["BATCH_STEP", "BATCH_GROUP"])
        self.assertEqual(1, steps["total"])
        self.assertEqual("BATCH_GROUP", steps["results"][0]["kind"])

    def test_top_groups_and_top_search(self):
        groups = {item["kind"]: item["count"] for item in top_groups(self.conn)}
        self.assertIn("SCHEMA", groups)
        self.assertIn("SERVICE_TYPE", groups)
        self.assertIn("TRANSACTION", groups)
        self.assertIn("BATCH_TRANSACTION", groups)
        self.assertIn("ERRORCONF", groups)

        schemas = search_group(self.conn, "top", root_kind="SCHEMA")
        self.assertEqual(4, schemas["total"])  # Base / DemoDict / DemoTables / DemoCType

        every_top = search_group(self.conn, "top")
        self.assertEqual(sum(groups.values()), every_top["total"])

    def test_table_detail_structures(self):
        table = search_group(self.conn, "table", query="demo_user", dimension="id")
        detail = node_detail(self.conn, table["results"][0]["stable_id"])
        structured = detail["detail"]
        field_ids = {field["raw_id"] for field in structured["fields"]}
        self.assertEqual({"id", "status"}, field_ids)
        # 公共字段表按 extension 引用顺序展示，且携带其自身字段
        extensions = structured["extensions"]
        self.assertEqual(["DemoTables.audit", "DemoTables.base_cols"],
                         [item["full_id"] for item in extensions])
        self.assertTrue(all(item["resolved"] for item in extensions))
        self.assertEqual(["created_at"], [f["raw_id"] for f in extensions[0]["fields"]])
        self.assertEqual(["org_id"], [f["raw_id"] for f in extensions[1]["fields"]])
        self.assertEqual(["idx_status"], [idx["raw_id"] for idx in structured["indexes"]])
        self.assertEqual(["odb_id"], [idx["raw_id"] for idx in structured["odbindexes"]])
        self.assertEqual(["seq_demo"], [seq["raw_id"] for seq in structured["sequences"]])

    def test_service_detail_input_output(self):
        service = search_group(self.conn, "service", query="DemoSvc", dimension="id")
        detail = node_detail(self.conn, service["results"][0]["stable_id"])["detail"]
        self.assertEqual(1, len(detail["operations"]))
        operation = detail["operations"][0]
        self.assertEqual(["custName"], [f["raw_id"] for f in operation["input"]])
        self.assertEqual(["result"], [f["raw_id"] for f in operation["output"]])

        # 服务页直接查服务操作（fullId = 服务文件.服务id）
        operation_detail = node_detail(self.conn, "DemoSvc.openAccount")["detail"]
        self.assertEqual(["custName"], [f["raw_id"] for f in operation_detail["input"]])
        self.assertEqual(["result"], [f["raw_id"] for f in operation_detail["output"]])

    def test_transaction_detail_flow_tree_and_branches(self):
        tran = search_group(self.conn, "transaction", query="demoTran", dimension="id")
        payload = node_detail(self.conn, tran["results"][0]["stable_id"])
        detail = payload["detail"]
        self.assertEqual(["in1"], [f["raw_id"] for f in detail["input"]])
        self.assertEqual(["out1"], [f["raw_id"] for f in detail["output"]])

        steps = detail["flow_steps"]
        self.assertEqual(["method", "case", "service"], [step["xml_tag"] for step in steps])
        self.assertEqual("checkInput", steps[0]["label"])
        self.assertEqual("missingTran", steps[2]["label"])
        self.assertIsNone(steps[2]["resolved_target"])

        case = steps[1]
        self.assertEqual("按类型分支", case["label"])
        when = case["children"][0]
        self.assertEqual("when", when["xml_tag"])
        self.assertEqual("type == 'A'", when["test"])
        branch_service = when["children"][0]
        self.assertEqual("DemoSvc.openAccount", branch_service["label"])
        self.assertIsNotNone(branch_service["resolved_target"])

    def test_error_code_and_dictionary_detail(self):
        errors = search_group(self.conn, "error_code")
        detail = node_detail(self.conn, errors["results"][0]["stable_id"])["detail"]
        self.assertEqual(1, len(detail["groups"]))
        group = detail["groups"][0]
        self.assertEqual("Cuce", group["node"]["raw_id"])
        self.assertEqual(["E0002", "E0003"], [item["raw_id"] for item in group["errors"]])
        self.assertEqual("凭证种类不存在", group["errors"][0]["properties"]["message"])
        self.assertEqual("vchr_catg", group["errors"][0]["parameters"])

        dicts = search_group(self.conn, "dictionary")
        dict_detail = node_detail(self.conn, dicts["results"][0]["stable_id"])["detail"]
        self.assertEqual(["name", "status"], [item["raw_id"] for item in dict_detail["elements"]])

    def test_enum_master_and_restriction_detail(self):
        enums = enum_groups(self.conn)
        self.assertEqual(1, enums["total"])
        owner = enums["results"][0]
        self.assertEqual("Base.U_STATUS", owner["full_id"])
        self.assertEqual(2, owner["value_count"])

        matched = enum_groups(self.conn, query="U_STATUS")
        self.assertEqual(1, matched["total"])

        detail = node_detail(self.conn, owner["stable_id"])["detail"]
        self.assertEqual(["A", "I"], [item["raw_id"] for item in detail["enum_values"]])

    def test_child_nodes_tree(self):
        schemas = search_group(self.conn, "top", root_kind="SCHEMA")
        demo_tables = next(item for item in schemas["results"] if item["full_id"] == "DemoTables")
        children = child_nodes(self.conn, demo_tables["stable_id"])
        self.assertEqual(3, len(children))
        demo_user = next(item for item in children if item["full_id"] == "DemoTables.demo_user")
        self.assertTrue(demo_user["has_children"])

    def test_base_type_group_queries_u_schema(self):
        catalog = search_group(self.conn, "base_type", query="U_STATUS", dimension="id")
        self.assertEqual(1, catalog["total"])
        item = catalog["results"][0]
        self.assertEqual("RESTRICTION_TYPE", item["kind"])
        self.assertEqual("Base.U_STATUS", item["full_id"])
        self.assertTrue(item["file_path"].endswith(".u_schema.xml"))
        # 详情携带 base/maxLength 属性与枚举值
        detail = node_detail(self.conn, item["stable_id"])["detail"]
        self.assertEqual(["A", "I"], [e["raw_id"] for e in detail["enum_values"]])


class WorkbenchHttpTest(WorkbenchTestBase):
    def setUp(self):
        self.root, self.db = self.build_index()
        self.server = WorkbenchServer(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _get(self, path, method="GET"):
        request = urllib.request.Request(self.base_url + path, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_static_assets_served(self):
        status, body = self._get("/")
        self.assertEqual(200, status)
        self.assertIn(b"APSGraph", body)
        status, body = self._get("/app.js")
        self.assertEqual(200, status)
        self.assertIn("runSearch".encode(), body)
        status, body = self._get("/app.css")
        self.assertEqual(200, status)
        status, body = self._get("/mermaid.min.js")
        self.assertEqual(200, status)
        self.assertIn(b"mermaid", body)

    def test_static_path_traversal_blocked(self):
        status, _ = self._get("/..%2fcli.py")
        self.assertEqual(404, status)
        status, _ = self._get("/missing.html")
        self.assertEqual(404, status)

    def test_api_endpoints(self):
        status, body = self._get("/api/stats")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertIn("stats", payload)
        self.assertIn("top_groups", payload)

        status, body = self._get("/api/search?group=table&q=demo_user&field=id")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual(1, payload["total"])

        status, body = self._get("/api/search?group=base_type&q=U_STATUS&field=id")
        self.assertEqual(200, status)
        self.assertEqual(1, json.loads(body)["total"])

        status, body = self._get("/api/enums?q=U_STATUS")
        self.assertEqual(200, status)
        enums = json.loads(body)
        self.assertEqual(1, enums["total"])
        self.assertEqual(2, enums["results"][0]["value_count"])

        status, body = self._get("/api/node?id=" + urllib.request.quote("MdError"))
        self.assertEqual(200, status)
        detail = json.loads(body)
        self.assertEqual("ERRORCONF", detail["node"]["kind"])
        self.assertEqual(["E0002", "E0003"],
                         [item["raw_id"] for item in detail["detail"]["groups"][0]["errors"]])

        status, body = self._get("/api/node?id=" + urllib.request.quote("MdError.Cuce.E0002"))
        self.assertEqual(200, status)
        error_detail = json.loads(body)
        self.assertEqual("ERROR", error_detail["node"]["kind"])
        self.assertEqual(["vchr_catg"], [item["raw_id"] for item in error_detail["detail"]["parameters"]])

        status, body = self._get("/api/node?id=DemoSvc.openAccount")
        self.assertEqual(200, status)
        self.assertEqual("SERVICE_OPERATION", json.loads(body)["node"]["kind"])

        status, body = self._get("/api/children?id=DemoTables")
        self.assertEqual(200, status)
        children = json.loads(body)["children"]
        self.assertEqual(["DemoTables.audit", "DemoTables.base_cols", "DemoTables.demo_user"],
                         [item["full_id"] for item in children])

        # DDL 预览：只生成不执行，支持三种方言
        status, body = self._get("/api/search?group=table&q=demo_user&field=id")
        stable_id = json.loads(body)["results"][0]["stable_id"]
        for dialect in ("mysql", "oracle", "postgresql"):
            status, body = self._get(f"/api/ddl?id={urllib.request.quote(stable_id)}&dialect={dialect}")
            self.assertEqual(200, status)
            payload = json.loads(body)
            self.assertEqual(dialect, payload["dialect"])
            self.assertIn("create", payload["sql"].lower())
            self.assertIn("validation", payload)
            try:
                import sqlglot  # noqa: F401
                self.assertTrue(payload["validation"]["valid"])
            except ImportError:
                self.assertFalse(payload["validation"]["available"])
        status, body = self._get("/api/ddl?id=demo_user&dialect=db2")
        self.assertEqual(400, status)
        status, body = self._get(f"/api/ddl?id={urllib.request.quote('DemoSvc.openAccount')}&dialect=mysql")
        self.assertEqual(400, status)  # 仅表节点支持 DDL 预览

        status, _ = self._get("/api/unknown")
        self.assertEqual(404, status)
        status, _ = self._get("/api/node")
        self.assertEqual(400, status)
        status, _ = self._get("/api/search?group=table", method="POST")
        self.assertEqual(405, status)

    def test_ddl_validation_with_sqlglot(self):
        try:
            import sqlglot  # noqa: F401
        except ImportError:
            self.skipTest("sqlglot optional dependency not installed")
        result = validate_ddl_sql("create table demo (id varchar(10) not null);", "mysql")
        self.assertTrue(result["available"])
        self.assertTrue(result["valid"])
        self.assertEqual(1, result["statements"])

        bad = validate_ddl_sql("create table demo (id varchar(10)", "mysql")
        self.assertTrue(bad["available"])
        self.assertFalse(bad["valid"])
        self.assertTrue(bad["errors"])

        skipped = validate_ddl_sql("  ", "mysql")
        self.assertFalse(skipped["available"])
        self.assertIsNone(skipped["valid"])

        with self.assertRaises(ValueError):
            validate_ddl_sql("select 1", "db2")

    def test_serve_workbench_rejects_missing_db(self):
        with self.assertRaises(FileNotFoundError):
            serve_workbench(self.root / "nope.db", open_browser=False)


if __name__ == "__main__":
    unittest.main()
