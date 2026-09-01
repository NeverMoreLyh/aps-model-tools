# APSGraph 设计文档

> 版本：0.18.0
> 更新时间：2026-08-23  
> 文档定位：说明 APSGraph 的关键架构、模块设计、数据模型、算法、并发模型、性能设计和安全边界。

---

## 1. 设计目标

1. 把 APS XML 转换为可查询、可追溯的关系图；
2. 让 workspace 模型与依赖 JAR 模型在同一个索引中解析；
3. 保证失败时不破坏既有产物；
4. 在大型银行核心 workspace 上保持可接受的构建和查询性能；
5. 保持核心工具零第三方运行时依赖；
6. 输出机器可读 JSON，便于 AI 代理和 CI 集成。

## 2. 总体架构

```text
+---------------------------+
| apsgraph CLI              |
+---------------------------+
      |          |         |
   scanner     maven     queries
      |          |         |
   SQLite staging DB       |
      |                    |
 atomic publish            |
      v                    |
 .apsgraph/apsgraph.db     |
      |                    |
 impact / ddl / diff / bridge / classify / export
```

### 2.1 分层

| 层 | 职责 |
|---|---|
| CLI 层 | 参数解析、默认值、JSON 输出、stderr 日志 |
| Maven 层 | 项目发现、拓扑排序、构建、依赖解析、JAR 筛选 |
| Scanner 层 | XML 发现、解析、节点与边生成、增量同步 |
| Store 层 | SQLite schema、连接、只读查询 |
| Analysis 层 | refs、impact、DDL、db-diff、bridge、classify |
| Export 层 | Markdown、Excel 文档输出 |

## 3. 关键模块

### 3.1 `apsgraph.scanner`

- 发现受支持的 APS XML 后缀。
- 使用 `xml.etree.ElementTree` 解析。
- 按文件、节点、边三层写入 staging DB。
- 模型节点保存 `stable_id`、`full_id`、`kind`、`owner_node_id`、`file_id`、`xml_tag`、原始属性 JSON。
- 引用边保存来源节点、目标节点、raw target、关系类型、证据文件、证据值、置信度。
- 支持 JAR 内 XML 以 `jar:<jar>!<entry>` 逻辑路径导入。
- 支持外部索引以 `external-db:<db>!<path>` 逻辑路径合并。
- 对非模型属性做语义过滤，降低 unresolved 噪声。

### 3.2 `apsgraph.maven`

- 发现 workspace 中全部 POM。
- 识别项目、aggregator、reactor、parent。
- 计算 workspace 内依赖和构建单元。
- build 阶段完成后再进入 dependency 阶段。
- 支持全量模式和 framework 模式。
- 根据 POM 标记筛选业务 JAR。
- 生成 dependency manifest。
- 控制 staging DB 的原子发布。

### 3.3 `apsgraph.store`

- SQLite schema 初始化与版本校验。
- 只读连接和写连接隔离。
- 提供模型查询和引用遍历原语。
- 可选以 `xml_documents` 保存完整本地 XML，文件级关联 `file_id`。
- 拒绝非 APS 索引数据库和跨 workspace 索引。

### 3.6 APS 类型解析与数据库列映射

字段必须沿 `type → RestrictionType/SubEnum → baseTypeObj → SimpleType` 递归解析，再选择目标数据库 renderer。完整的 MySQL/Oracle/PostgreSQL 映射、长度/精度、FTL 特殊修正和 APSGraph→dbm2 校准契约见 [`aps-type-database-mapping.md`](aps-type-database-mapping.md)。

### 3.7 APSGraph 与 dbm2 设计交叉验证

`aps-metadata-db-mapping-cross-validation.md` 记录 APS XML/SQLite 到 dbm2 `TableMeta`、`ColumnMeta`、`IndexMeta` 的完整转换链、当前实现差距和后续校准契约。

### 3.5 分析模块

| 模块 | 核心算法 |
| impact | 从目标节点反向 BFS，按深度限制收集影响路径 |
| refs | 正向 / 反向 / 双向图遍历 |
| ddl / ddlgen | 模型字段解析、类型映射、继承展开、方言模板 |
| dbdiff | 模型 schema 与数据库 schema 规范化后对比 |
| bridge | APS full_id 与生成 Java / CodeGraph 节点匹配 |
| classify | 关键词与路径启发式分类 |

### 3.6 原始 XML 与语义节点索引

扫描不采用“只保留顶层模型”或“每个 DOM 标签全部建节点”两种极端方案，而采用文件完整证据与语义投影分离：

```text
model_files（文件元数据、hash、解析状态）
  ├── xml_documents（可选，--embed-xml 时保存完整原始 XML）
  └── nodes（顶层模型 + 有业务语义的嵌套节点）
        └── edges（包含、引用、调用、读写和证据）
```

`nodes` 继续保存 `owner_node_id` 层级，并优先索引具备稳定 id、可被引用、需要 UI 查询或参与关系分析的对象，例如 `TABLE`、`FIELD`、`INDEX`、`TRANSACTION`、`SERVICE`、`NAMED_SQL`、`PARAMETER`、`FLOW_NODE`。无业务语义的 XML 容器标签不单独建节点，但其属性保留在所属节点的 `properties_json` 中。

`apsgraph scan --embed-xml` 在成功解析的本地 XML 文件上写入 `xml_documents(file_id, content, content_encoding, content_hash, content_size)`；默认扫描仍只保存路径和 hash，避免索引体积无条件膨胀。`apsgraph sync --embed-xml` 对新增/修改文件更新嵌入内容，删除文件依靠外键级联清理。

归档 JAR 和 `external-db:` 逻辑路径当前不嵌入原始 XML；UI 应显示“外部证据不可用”，不得将属性 JSON 伪装为 XML 正文。

### FlowTran 投影

FlowTran 详情页依赖语义节点和关系，而不是顶层 `properties_json` 的字符串拼接：

```text
TRANSACTION
  ├── INPUT/OUTPUT_INTERFACE / PARAMETER
  ├── DATA_MAPPING
  ├── FLOW_NODE / SERVICE_CALL / TRANSACTION_CALL
  └── exception/route nodes
```

具体 XML tag 到 `kind` 的版本差异应由版本化投影规则配置承载；稳定 id、owner、源码路径、hash 和通用关系算法由代码实现。正式启用专用 Tab 前，必须以目标 workspace 的实际 `kind`、`xml_tag`、属性和关系结果校准。

### 4.1 核心表

| 表 | 说明 |
|---|---|
| `model_files` | 文件路径、后缀、hash、解析状态、错误信息 |
| `nodes` | 模型节点、类型、ID、owner、文件、XML tag、属性 |
| `edges` | 节点关系、raw target、证据文件、证据值、置信度 |
| `scan_state` | workspace、scanner 版本、同步状态 |

### 4.2 ID 设计

- `stable_id`：面向内部稳定身份，包含逻辑路径、序号、full_id 和 kind。
- `full_id`：面向模型引用解析，如 `Base.U_NAME`。
- workspace 文件使用相对路径。
- JAR 模型使用 `jar:<jar>!<entry>`。
- external index 使用 `external-db:<db>!<path>`。

### 4.3 关系设计

| 关系 | 语义 |
|---|---|
| `CONTAINS` | 结构包含 |
| `TYPE_REF` | 类型引用 |
| `DICT_REF` | 字典引用 |
| `EXTENDS` | 模型继承，支持多父拆分 |
| `IMPLEMENTS` | 服务实现 |
| `CALLS_SERVICE` | 服务调用 |
| `CALLS_TRANSACTION` | 交易调用 |

引用解析优先通过 `raw_target == nodes.full_id` 精确匹配。匹配唯一节点才写入 `to_node_id`；未匹配或歧义均保留为 unresolved。

## 5. Maven workspace 设计

### 5.1 全量模式

流程：

```text
discover POMs
-> identify reactors/build units
-> topological sort
-> mvn install -DskipTests
-> dependency:list
-> dependency:copy-dependencies
-> coordinate conflict check
-> business marker check
-> scan workspace XML
-> import JAR XML
-> atomic publish
```

### 5.2 Framework 模式

目标是在不需要生成所有业务模块代码的情况下获得框架 JAR XML：

1. 找到本地 parent 链最高边界，例如 `ap-parent`；
2. 只对该边界执行 `mvn -N install`；
3. 解析该边界依赖；
4. 导入业务标记 JAR 中的 XML；
5. 与 workspace XML 合并生成索引。

### 5.3 Parent 边界算法

- 从项目向上遍历 parent；
- parent 在 workspace 内则继续上溯；
- 遇到外部 parent 时停止；
- 返回最后一个本地 parent 作为分析边界；
- 中间 parent / aggregator 不重复做依赖分析。

### 5.4 冲突与去重

- Maven 坐标 key 为 `groupId:artifactId`。
- 同 key 多版本直接失败。
- 不同 group 相同 artifactId 允许共存。
- JAR 路径相同或坐标相同则去重。
- workspace 内依赖优先于外部依赖解析。

## 6. 并发与性能设计

### 6.1 阶段串行、阶段内并行

```text
Build phase  ->  Dependency phase  ->  Import phase
     |                  |                  |
  parallel jobs       parallel jobs      serialized DB writes
```

约束：

- build 与 dependency 阶段必须串行，避免尚未 install 的模块被依赖解析读取；
- 无依赖 build unit 可以并行；
- 无依赖项目 dependency list / copy 可以并行；
- SQLite 写入集中在单连接事务中，避免写冲突；
- Maven 子进程输出写独立日志，互不覆盖。

### 6.2 扫描性能

- 先按后缀过滤，避免解析无关 XML。
- 排除生成目录。
- workspace 文件按路径排序，保证确定性。
- 全量构建 staging DB，成功后原子替换。
- 增量同步使用 SHA-256 只重建变更、删除文件。
- JAR 解析先做后缀过滤，再读取内容。

### 6.3 查询性能

- `full_id`、`stable_id`、文件路径等高频字段建立索引。
- impact / refs 使用边表索引反向遍历。
- 查询工具尽量只选择必要列。
- 输出限制深度和分页规模，防止超大图拖垮终端。

### 6.4 性能指标

以银行核心 V8.7 规模 workspace 为参考：

| 操作 | 指标 |
|---|---|
| workspace XML 数 | 3600+ |
| 依赖清单 | 500+ |
| 索引节点 | 13万+ |
| 索引边 | 28万+ |
| framework scan | 十几秒级，依赖机器与 Maven 缓存 |
| 全量 scan | 分钟级 |
| stats / impact / refs | 秒级 |

性能优化必须优先保持确定性和证据完整性。

## 7. 可靠性设计

### 7.1 原子发布

```text
staging DB -> validate -> os.replace(staging, target)
```

任一步失败：

- 不调用 replace；
- 保留既有 target；
- 清理 staging；
- 返回非零退出码；
- 输出明确错误。

### 7.2 Fail-closed 场景

- Maven 构建失败；
- 同坐标多版本；
- reactor 内 JDK 冲突；
- 依赖 JAR XML 解析失败且用户要求失败；
- 非法索引或跨 workspace 索引；
- external index 与 include-deps 同时使用。

### 7.3 容忍场景

- 普通 workspace XML 解析错误：默认记录并继续。
- 普通 scan unresolved：警告并继续。
- 依赖 JAR 无模型：记录 skipped，不算失败。

## 8. 安全设计

| 风险 | 设计 |
|---|---|
| 误改源码 | scanner 只读 |
| 误执行 DDL | DDL 只输出文件或 stdout |
| 数据库密码泄漏 | 支持 DSN 环境变量，输出脱敏 |
| 索引误替换 | staging + 原子发布 |
| CodeGraph 污染 | 只读连接 |
| 命令注入 | Maven / DB 参数通过数组或参数化 SQL 传递 |
| 依赖误导入 | POM 业务标记过滤 |

## 9. 可观测性设计

- stdout：JSON，面向机器。
- stderr：阶段与项目进度，面向人。
- Maven build 日志：`.apsgraph/logs/maven/`。
- framework build 日志：`.apsgraph/logs/maven-framework/`。
- dependency 日志：`.apsgraph/logs/maven-dependencies/`。
- 依赖清单：`.apsgraph/dependency-manifest.json`。
- 错误证据：文件路径、XML tag、属性值。

## 10. 扩展设计

### 10.1 MCP 包装

MCP Server 可以复用 CLI 或 Python API，不重复实现业务：

| MCP tool | 底层能力 |
|---|---|
| `apsgraph_options` | options |
| `apsgraph_scan` | scan / include-deps / external-db |
| `apsgraph_status` | status |
| `apsgraph_refs` | refs |
| `apsgraph_impact` | impact |
| `apsgraph_show` | show |

MCP 层必须：

- 明确声明只读或会写 `.apsgraph` 的操作；
- 对长任务返回进度；
- 限制 workspace 路径范围；
- 不直接暴露任意 shell 命令。

### 10.2 后续索引版本

- 新增字段时保持向后兼容读取；
- scanner 版本变化时提示全量重建；
- V2 -> V3 需要迁移脚本和回滚策略。
