import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from apsgraph.registry import touch_workspace, upsert_workspace
from apsgraph.scanner import scan_workspace
from apsgraph.store import connect
from apsgraph.workbench import (
    WorkbenchServer,
    _bind_workbench_server,
    _serve_workbench_server,
    child_nodes,
    close_workbenches,
    enum_groups,
    list_workbenches,
    node_detail,
    register_workbench,
    search_group,
    serve_workbench,
    top_groups,
    unregister_workbench,
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
    "batchfile/apdemor.file_batch_tran.xml": """<?xml version="1.0"?>
<file_batch_transaction id="apdemor" longname="文件读批批量测试" kind="read">
  <fileTemplate id="apdemor" longname="文件来盘测试">
    <body id="Body" longname="文件体">
      <field id="name" type="Base.U_NAME" ref="DemoDict.CustomerInfo.name"/>
    </body>
  </fileTemplate>
</file_batch_transaction>
""",
    "namedsql/StPrc.nsql.xml": """<?xml version="1.0"?>
<sqls id="StPrc" longname="结算相关SQL">
  <update id="upd_demo" method="update" longname="更新演示">
    <parameterMap class="java.util.Map">
      <parameter id="org_num" type="Base.U_NAME" property="org_num" longname="机构号"/>
    </parameterMap>
    <sql>UPDATE kstb_demo SET org_num = #org_num#</sql>
    <sql type="oracle">UPDATE kstb_demo SET org_num = #org_num#</sql>
  </update>
  <dynamicSelect id="dyn_demo" method="selectPage" longname="动态查询演示">
    <dynamicSql type="mysql">
      <str test="org_num!=null"><![CDATA[select * from kstb_demo_mysql]]></str>
    </dynamicSql>
    <dynamicSql>
      <str test="org_num!=null"><![CDATA[select * from kstb_demo where org_num = #org_num#]]></str>
    </dynamicSql>
  </dynamicSelect>
</sqls>
""",
    "sharding/aplt.sharding.xml": """<?xml version="1.0"?>
<ShardingStrategy id="aplt" displayName="平台分表实现">
  <strategies>
    <strategy id="ApltStrategy" name="默认分片策略" clazzImpl="demo.ApltShardingStrategy"/>
  </strategies>
</ShardingStrategy>
""",
    "broken/Bad.tables.xml": "<schema id=\"Bad\"><table",
    "const/CfConst.constant.xml": """<?xml version="1.0"?>
<constantConf id="CfConst" longname="客户副本常量定义">
  <constants id="Busi" longname="业务种类名称">
    <constant id="CONST_CUST_BTCH" message="CFTEMP" description="客户信息批量同步-回盘文件"/>
  </constants>
</constantConf>
""",
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

        # 常量页：按 id/fullid/message 过滤 constantConf>constants>constant
        consts = search_group(self.conn, "constant", query="CONST_CUST_BTCH", dimension="id")
        self.assertEqual(1, consts["total"])
        self.assertEqual("CfConst.Busi.CONST_CUST_BTCH", consts["results"][0]["full_id"])
        self.assertEqual("CONSTANT", consts["results"][0]["kind"])
        self.assertEqual(1, search_group(self.conn, "constant", query="CFTEMP", dimension="desc")["total"])

        # 文件批量 / 命名SQL / 分片 页
        fbt = search_group(self.conn, "file_batch", query="apdemor", dimension="id")
        self.assertEqual(1, fbt["total"])
        self.assertEqual("FILE_BATCH_TRANSACTION", fbt["results"][0]["kind"])
        fbt_detail = node_detail(self.conn, fbt["results"][0]["stable_id"])["detail"]
        self.assertEqual(["name"], [f["raw_id"] for f in fbt_detail["input"]])

        nsql = search_group(self.conn, "nsql", query="StPrc", dimension="id")
        self.assertEqual(1, nsql["total"])
        self.assertEqual("StPrc", nsql["results"][0]["full_id"])
        self.assertEqual("SQL_GROUP", nsql["results"][0]["kind"])

        # 命名SQL页：数据项粒度 NAMED_SQL（如 ApBatchFileSqls.upd_tb_file_tran_req）
        items = search_group(self.conn, "nsql_item", query="upd_demo", dimension="id")
        self.assertEqual(1, items["total"])
        self.assertEqual("StPrc.upd_demo", items["results"][0]["full_id"])
        self.assertEqual("NAMED_SQL", items["results"][0]["kind"])
        item_detail = node_detail(self.conn, items["results"][0]["stable_id"],
                                  source_root=self.root)["detail"]
        self.assertEqual(["NONE", "oracle"], [s["type"] for s in item_detail["sqls"]])
        nsql_detail = node_detail(self.conn, nsql["results"][0]["stable_id"])["detail"]
        self.assertEqual(["upd_demo", "dyn_demo"], [s["raw_id"] for s in nsql_detail["statements"]])
        stmt_detail = node_detail(self.conn, nsql_detail["statements"][0]["stable_id"])["detail"]
        self.assertEqual(["org_num"], [p["raw_id"] for p in stmt_detail["parameters"]])
        # SQL 文本从源文件按需解析，按数据库类型展示且 NONE 默认置顶
        sqls = node_detail(self.conn, nsql_detail["statements"][0]["stable_id"],
                           source_root=self.root)["detail"]["sqls"]
        self.assertEqual(["NONE", "oracle"], [item["type"] for item in sqls])
        self.assertIn("UPDATE kstb_demo", sqls[0]["text"])

        # 动态SQL：按元素自身 CDATA 原文本展示（NONE）
        dyn = search_group(self.conn, "nsql_item", query="dyn_demo", dimension="id")
        self.assertEqual(1, dyn["total"])
        dyn_sqls = node_detail(self.conn, dyn["results"][0]["stable_id"],
                               source_root=self.root)["detail"]["sqls"]
        self.assertEqual(["mysql", "NONE"], [s["type"] for s in dyn_sqls])
        # 动态SQL原样展示原始 XML 节点（含 str/test 等子节点）
        self.assertIn("<dynamicSql type=\"mysql\">", dyn_sqls[0]["text"])
        self.assertIn('test="org_num!=null"', dyn_sqls[0]["text"])
        self.assertIn("kstb_demo_mysql", dyn_sqls[0]["text"])
        self.assertIn("select * from kstb_demo where", dyn_sqls[1]["text"])
        self.assertEqual([], node_detail(self.conn, nsql_detail["statements"][0]["stable_id"])["detail"]["sqls"])

        sharding = search_group(self.conn, "sharding", query="aplt", dimension="id")
        self.assertEqual(1, sharding["total"])
        self.assertEqual("SHARDINGSTRATEGY", sharding["results"][0]["kind"])
        sh_detail = node_detail(self.conn, sharding["results"][0]["stable_id"])["detail"]
        self.assertEqual(["ApltStrategy"], [s["raw_id"] for s in sh_detail["strategies"]])

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

    def test_source_xml_fragment_for_every_model(self):
        # 表节点：原始 XML 片段含自身与子结构
        table = search_group(self.conn, "table", query="demo_user", dimension="id")
        frag = node_detail(self.conn, table["results"][0]["stable_id"],
                           source_root=self.root)["xml_fragment"]
        self.assertEqual("tables/Demo.tables.xml", frag["path"])
        self.assertIn('<table id="demo_user"', frag["xml"])
        self.assertIn("<odbindexes>", frag["xml"])

        # 字段/枚举值等任意节点均可定位
        enums = search_group(self.conn, "enum", query="有效")
        frag2 = node_detail(self.conn, enums["results"][0]["stable_id"],
                            source_root=self.root)["xml_fragment"]
        self.assertIn('id="A"', frag2["xml"])

        # 不给 source_root 时不出现在载荷中（按需读取，不落库）
        none_frag = node_detail(self.conn, table["results"][0]["stable_id"])["xml_fragment"]
        self.assertIsNone(none_frag)

        # 文件级节点（数据字典文件/命名SQL文件/错误码文件）不展示 XML 片段
        dicts = search_group(self.conn, "dictionary")
        self.assertIsNone(node_detail(self.conn, dicts["results"][0]["stable_id"],
                                      source_root=self.root)["xml_fragment"])

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

        status, body = self._get("/api/parse-failures")
        self.assertEqual(200, status)
        failures = json.loads(body)
        self.assertEqual(1, failures["total"])
        self.assertEqual("broken/Bad.tables.xml", failures["results"][0]["path"])
        self.assertIn("error_message", failures["results"][0])
        status, body = self._get("/api/parse-failures?q=nomatch")
        self.assertEqual(0, json.loads(body)["total"])

        status, body = self._get("/api/dashboard")
        self.assertEqual(200, status)
        dash = json.loads(body)
        self.assertGreater(dash["db_size"], 0)
        self.assertEqual(1, dash["stats"]["parse_failed"])
        self.assertIn("nodes", dash["stats"])
        self.assertEqual(3, dash["counts"]["table"])  # audit / base_cols / demo_user
        self.assertIn("enum", dash["counts"])
        self.assertIn("nsql_item", dash["counts"])

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

    def test_second_bind_on_same_port_fails_closed(self):
        # 回归：Windows 的 SO_REUSEADDR 曾允许两个实例同时"成功"绑定同一端口
        # 且不报任何冲突；重复绑定必须报错（fail-closed）
        with self.assertRaises(ValueError):
            _bind_workbench_server(self.db, self.server.server_port)

    def test_serve_workbench_rejects_missing_db(self):
        with self.assertRaises(FileNotFoundError):
            serve_workbench(self.root / "nope.db", open_browser=False)


# 选一个在测试环境不可能存活的 pid：Linux 默认 pid_max 上限 4194303，
# Windows tasklist 对不存在的 pid 返回空结果。
DEAD_PID = 4194303


class WorkbenchRegistryTest(WorkbenchTestBase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = Path(self.tmp.name) / "registry.json"

    def _entry(self, **overrides):
        entry = {"port": 9100, "pid": DEAD_PID, "url": "http://127.0.0.1:9100/",
                 "db": "demo.db", "workspace": "demo",
                 "started_at": "2026-09-17T00:00:00+08:00"}
        entry.update(overrides)
        return entry

    def _read_registry(self):
        return json.loads(self.registry.read_text(encoding="utf-8"))["instances"]

    def _start_server(self):
        root, db = self.build_index()
        server = WorkbenchServer(db, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if predicate():
                    return True
            except (OSError, ValueError):
                pass  # 注册表尚未落盘，等待下一次轮询
            time.sleep(0.05)
        return False

    def test_register_and_unregister_round_trip(self):
        register_workbench(self._entry(), path=self.registry)
        register_workbench(self._entry(port=9101), path=self.registry)
        self.assertEqual([9100, 9101], sorted(e["port"] for e in self._read_registry()))

        # 同端口重复注册时替换原条目而不是叠加
        register_workbench(self._entry(pid=1), path=self.registry)
        self.assertEqual(2, len(self._read_registry()))

        unregister_workbench(9100, path=self.registry)
        self.assertEqual([9101], [e["port"] for e in self._read_registry()])

    def test_list_reports_live_instance_and_prunes_stale(self):
        server = self._start_server()
        register_workbench(self._entry(port=server.server_port, pid=os.getpid(),
                                       url=f"http://127.0.0.1:{server.server_port}/"),
                           path=self.registry)
        # pid 已死的条目与 pid 存活但端口没有 workbench 服务的条目都算过期
        register_workbench(self._entry(port=9399, pid=DEAD_PID), path=self.registry)
        register_workbench(self._entry(port=9398, pid=os.getpid()), path=self.registry)

        listing = list_workbenches(path=self.registry)
        self.assertEqual([server.server_port], [e["port"] for e in listing["instances"]])
        self.assertEqual(2, listing["pruned_stale"])
        # 注册表已自愈，仅剩存活实例
        self.assertEqual([server.server_port], [e["port"] for e in self._read_registry()])

    def test_close_stops_live_instance_and_removes_entry(self):
        server = self._start_server()
        register_workbench(self._entry(port=server.server_port, pid=os.getpid()),
                           path=self.registry)
        with mock.patch("apsgraph.workbench._terminate_pid") as terminate:
            result = close_workbenches(port=server.server_port, path=self.registry)
        self.assertEqual([server.server_port], [e["port"] for e in result["closed"]])
        terminate.assert_called_once_with(os.getpid())
        self.assertEqual([], self._read_registry())

    def test_close_reports_already_stopped_and_prunes(self):
        register_workbench(self._entry(port=9100, pid=DEAD_PID), path=self.registry)
        with mock.patch("apsgraph.workbench._terminate_pid") as terminate:
            result = close_workbenches(port=9100, path=self.registry)
        self.assertEqual([9100], [e["port"] for e in result["already_stopped"]])
        terminate.assert_not_called()
        self.assertEqual([], self._read_registry())

    def test_close_never_kills_process_when_port_not_serving_workbench(self):
        # pid 存活但端口无 workbench 响应（pid 复用/端口被占用）：只清理条目，不终止进程
        register_workbench(self._entry(port=9398, pid=os.getpid()), path=self.registry)
        with mock.patch("apsgraph.workbench._terminate_pid") as terminate:
            result = close_workbenches(port=9398, path=self.registry)
        self.assertEqual([9398], [e["port"] for e in result["stale"]])
        terminate.assert_not_called()
        self.assertEqual([], self._read_registry())

    def test_close_missing_port_and_all_semantics(self):
        result = close_workbenches(port=8321, path=self.registry)
        self.assertEqual([8321], result["not_found"])

        # --all 在注册表为空时返回空结果而不是 not_found
        result = close_workbenches(close_all=True, path=self.registry)
        self.assertEqual([], result["not_found"])

        # close_all 停止所有实例并清空注册表
        server = self._start_server()
        register_workbench(self._entry(port=server.server_port, pid=os.getpid()),
                           path=self.registry)
        register_workbench(self._entry(port=9100, pid=DEAD_PID), path=self.registry)
        with mock.patch("apsgraph.workbench._terminate_pid") as terminate:
            result = close_workbenches(close_all=True, path=self.registry)
        self.assertEqual(1, len(result["closed"]))
        self.assertEqual(1, len(result["already_stopped"]))
        self.assertEqual([], self._read_registry())

    def test_serve_workbench_port_zero_registers_and_unregisters(self):
        root, db = self.build_index()
        server = _bind_workbench_server(db, 0)
        self.assertGreater(server.server_port, 0)  # 端口 0 → 实际绑定随机可用端口
        self.addCleanup(server.server_close)
        messages = []
        # patch registry_path 使 serve 线程内的注册/注销落到测试注册表
        with mock.patch("apsgraph.workbench.registry_path", return_value=self.registry):
            thread = threading.Thread(
                target=_serve_workbench_server, args=(server, db),
                kwargs={"open_browser": False, "progress": messages.append}, daemon=True)
            thread.start()

            expected_port = server.server_port
            self.assertTrue(self._wait_for(
                lambda: any(e.get("port") == expected_port for e in self._read_registry())))
            entry = next(e for e in self._read_registry() if e["port"] == expected_port)
            self.assertEqual(os.getpid(), entry["pid"])
            self.assertEqual(f"http://127.0.0.1:{expected_port}/", entry["url"])
            self.assertTrue(any(f":{expected_port}/" in m for m in messages))

            server.shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(self._wait_for(lambda: not self._read_registry()))
            self.assertEqual([], self._read_registry())


class WorkbenchMultiWorkspaceHttpTest(unittest.TestCase):
    """多 workspace 全局模式：?ws= 路由、默认选中、失效标注与 fail-closed。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.registry = self.tmp / "apsgraph-home" / "registry.json"
        self.workspaces = {}
        for name, table in (("alpha", "table_a"), ("beta", "table_b")):
            root = self.tmp / name
            model = root / "tables/Demo.tables.xml"
            model.parent.mkdir(parents=True)
            model.write_text(
                '<schema id="Demo" package="p"><table id="%s" name="%s"><fields/></table></schema>'
                % (table, table), encoding="utf-8")
            db = root / "index.db"
            scan_workspace(root, db)
            upsert_workspace(root, db, path=self.registry)
            self.workspaces[name] = root
        # beta 最后被使用：裸跑默认选中它（避免同秒扫描的时间戳并列）
        touch_workspace(self.workspaces["beta"], path=self.registry)
        # 默认选中逻辑会探测 cwd；测试统一切到无索引的空目录，保证确定性
        cwd = self.tmp / "no-cwd-index"
        cwd.mkdir()
        self.old_cwd = Path.cwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, self.old_cwd)

        self.server = WorkbenchServer(db_path=None, port=0, registry=self.registry)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _get(self, path):
        request = urllib.request.Request(self.base_url + path)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_workspaces_endpoint_lists_entries_and_default(self):
        status, body = self._get("/api/workspaces")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("multi", payload["mode"])
        self.assertEqual({"alpha", "beta"}, {item["name"] for item in payload["workspaces"]})
        self.assertTrue(all(item["available"] for item in payload["workspaces"]))
        self.assertEqual(str(self.workspaces["beta"].resolve()), payload["default"])

    def test_search_routes_to_selected_workspace(self):
        alpha, beta = self.workspaces["alpha"], self.workspaces["beta"]
        status, body = self._get(
            "/api/search?group=table&q=table_a&field=id&ws=" + urllib.request.quote(str(alpha)))
        self.assertEqual(200, status)
        self.assertEqual(1, json.loads(body)["total"])

        # 同一查询切到 beta：索引不同，查不到 alpha 的表
        status, body = self._get(
            "/api/search?group=table&q=table_a&field=id&ws=" + urllib.request.quote(str(beta)))
        self.assertEqual(200, status)
        self.assertEqual(0, json.loads(body)["total"])

        # ws 也接受注册名
        status, body = self._get("/api/search?group=table&q=table_b&field=id&ws=beta")
        self.assertEqual(200, status)
        self.assertEqual(1, json.loads(body)["total"])

    def test_requests_without_ws_use_server_default(self):
        status, body = self._get("/api/search?group=table&field=id")
        self.assertEqual(200, status)
        payload = json.loads(body)
        # 默认 workspace 是 beta（最后使用），只能查到 table_b
        self.assertEqual(1, payload["total"])
        self.assertEqual("table_b", payload["results"][0]["raw_id"])

    def test_dashboard_request_touches_last_used(self):
        from apsgraph.registry import load_registry

        alpha = self.workspaces["alpha"]
        status, _ = self._get("/api/dashboard?ws=" + urllib.request.quote(str(alpha)))
        self.assertEqual(200, status)
        entry = load_registry(self.registry)["workspaces"][0]
        self.assertEqual("alpha", entry["name"])
        self.assertTrue(entry["lastUsedAt"])

    def test_stale_workspace_listed_but_fails_closed_on_use(self):
        gamma = self.tmp / "gamma"
        model = gamma / "tables/Demo.tables.xml"
        model.parent.mkdir(parents=True)
        model.write_text(
            '<schema id="Demo" package="p"><table id="table_g" name="table_g"><fields/></table></schema>',
            encoding="utf-8")
        db = gamma / "index.db"
        scan_workspace(gamma, db)
        upsert_workspace(gamma, db, path=self.registry)
        db.unlink()  # 索引事后被删除

        status, body = self._get("/api/workspaces")
        payload = json.loads(body)
        gamma_entry = next(item for item in payload["workspaces"] if item["name"] == "gamma")
        self.assertFalse(gamma_entry["available"])

        status, body = self._get("/api/dashboard?ws=" + urllib.request.quote(str(gamma)))
        self.assertEqual(400, status)
        self.assertIn("index database does not exist", json.loads(body)["error"])
        self.assertIn("apsgraph scan", json.loads(body)["error"])

    def test_unknown_ws_token_rejected(self):
        status, body = self._get("/api/dashboard?ws=nope")
        self.assertEqual(400, status)
        self.assertIn("unknown workspace: nope", json.loads(body)["error"])

    def test_ambiguous_name_rejected(self):
        from apsgraph.registry import load_registry, save_registry

        payload = load_registry(self.registry)
        payload["workspaces"][0]["name"] = "dup"
        payload["workspaces"][1]["name"] = "dup"
        save_registry(payload, self.registry)
        status, body = self._get("/api/dashboard?ws=dup")
        self.assertEqual(400, status)
        self.assertIn("ambiguous workspace name: dup", json.loads(body)["error"])

    def test_single_db_mode_reports_single(self):
        server = WorkbenchServer(self.workspaces["alpha"] / "index.db", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/workspaces")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual({"mode": "single"}, payload)

    def test_serve_workbench_multi_fails_closed_without_registry_or_cwd_index(self):
        # 隔离机器状态：真实 ~/.apsgraph 可能已有可用条目，必须指向空注册表
        empty_home = self.tmp / "empty-home"
        with mock.patch.dict(os.environ, {"APSGRAPH_HOME": str(empty_home)}):
            with self.assertRaises(FileNotFoundError):
                serve_workbench(db_path=None, open_browser=False)

    def test_serve_workbench_rejects_unknown_workspace_token(self):
        empty_home = self.tmp / "empty-home"
        with mock.patch.dict(os.environ, {"APSGRAPH_HOME": str(empty_home)}):
            with self.assertRaises(ValueError):
                serve_workbench(db_path=None, open_browser=False, workspace="nope")


if __name__ == "__main__":
    unittest.main()
