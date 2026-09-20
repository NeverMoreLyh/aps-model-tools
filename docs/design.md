# APSGraph 设计文档

> 版本：0.42.0
> 更新时间：2026-09-20  
> 文档定位：说明 APSGraph 的关键架构、模块设计、数据模型、算法、并发模型、性能设计和安全边界。

---

## 1. 设计目标

1. 把 APS XML 转换为可查询、可追溯的关系图；
2. 让 workspace 模型与外部 SQLite 索引在同一个查询视图中复用；
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
   scanner     store     queries
      |          |         |
   SQLite staging DB       |
      |                    |
 atomic publish            |
      v                    |
 .apsgraph/apsgraph.db     |
      |                    |
 registry upsert           |
      v                    |
 ~/.apsgraph/registry.json |
      |                    |
 workbench multi-workspace |
      |                    |
 impact / ddl / diff / bridge / classify / export
```

### 2.1 分层

| 层 | 职责 |
|---|---|
| CLI 层 | 参数解析、默认值、JSON 输出、stderr 日志 |
| Scanner 层 | XML 发现、解析、节点与边生成、增量同步 |
| Store 层 | SQLite schema、连接、只读查询 |
| Registry 层 | 全局 workspace 注册表读写、workspace 令牌解析 |
| Analysis 层 | refs、impact、DDL、db-diff、bridge、classify |
| Export 层 | Markdown、Excel 文档输出 |

## 3. 关键模块

### 3.1 `apsgraph.scanner`

- 发现受支持的 APS XML 后缀。
- 使用 `xml.etree.ElementTree` 解析。
- 按文件、节点、边三层写入 staging DB。
- 模型节点保存 `stable_id`、`full_id`、`kind`、`owner_node_id`、`file_id`、`xml_tag`、原始属性 JSON。
- 引用边保存来源节点、目标节点、raw target、关系类型、证据文件、证据值、置信度。
- 支持外部索引以 `external-db:<db>!<path>` 逻辑路径合并。
- 对非模型属性做语义过滤，降低 unresolved 噪声。

### 3.2 `apsgraph.registry`

全局 workspace 注册表（`~/.apsgraph/registry.json`，`APSGRAPH_HOME` 可重定向目录；与实例注册表 `workbench-registry.json` 独立）：

- 文件结构 `{"version": 1, "workspaces": [...]}`，条目字段 `id`（`entry_id`：解析路径的 SHA-1 前 8 位十六进制，确定性短 id，写入时落盘、读取时对旧条目即时计算兜底）、`name`（显示名，默认目录 basename，允许重名）、`workspacePath`（唯一键，resolve 后的绝对路径）、`dbPath`、`lastScanAt`、`lastUsedAt`（`%Y-%m-%d %H:%M:%S` 本地时间）。
- 写路径：`scan` 成功后 `upsert_workspace`（按 resolved workspacePath upsert，重复扫描刷新而非新增）；workbench 服务已注册 workspace 时 `touch_workspace` 刷新 `lastUsedAt`。写采用临时文件 + `os.replace` 原子替换；进程内并发（workbench 多线程 touch 与 scan upsert）由模块级锁保护，跨进程按最后写者胜（最坏只丢一次时间戳刷新）。
- 读路径：`load_registry` 对缺失文件返回空注册表，对损坏/结构非法文件 fail-closed 抛错，绝不静默重建；`workspace_overview` 输出带 `available`（索引文件存在性）标注、按最近使用排序的列表。
- 解析路径：`_match_entry(token)` 的注册表内匹配顺序为 id → name（重名命中多个时报错列带 id 候选）→ 解析后路径；`resolve_workspace(token)` 在此之上追加宽松规则（真实目录且 `<dir>/.apsgraph/apsgraph.db` 存在则只读直开、不注册）；`default_selection(cwd)` 实现裸跑默认选中（已注册 cwd → 未注册 cwd 索引 → 上次使用 → 最近扫描，全无时 fail-closed）。

### 3.3 `apsgraph.store`

- SQLite schema 初始化与版本校验。
- 只读连接和写连接隔离。
- 提供模型查询和引用遍历原语。
- 拒绝非 APS 索引数据库和跨 workspace 索引。

### 3.4 `apsgraph.workspace_maintenance`

`apsgraph workspace` 维护命令族的实现层：目标解析、批处理框架与各动作。

- 目标解析 `select_targets`：`--all` 展开全部注册条目（空注册表报错）；`--workspace` 走注册表内精确匹配（复用 `registry._match_entry`：name 精确、重名报错列候选、路径精确），**不适用** workbench 的未注册目录宽松直开——注册表写路径绝不触碰未注册条目。
- 输出层：workspace 族默认输出人类可读表格（`_render_table` 以 `unicodedata.east_asian_width` 做全角字符终端列宽对齐，错误信息在 NOTE 列截断），`--json` 输出机器可读载荷；逐条结果带 `id` 字段。
- 批处理框架 `_run_batch`：逐 workspace 执行动作函数，单点失败（ValueError/OSError/sqlite3.Error，如索引损坏、database is locked）记录为 `state: "error"` 后继续；报告型结果（`missing`/`stale`/`fresh`/`ok`）是正常输出不算失败。CLI 层按 `ok` 标志决定退出码（任一 error → 2）。
- 各动作均为既有原语的薄包装：`status` 复用 `workspace_status`（change-set 判定 stale）；`sync` 复用 `sync_workspace`；`rebuild` 复用 `scan_workspace`（staging + 原子替换）并在成功后 `upsert_workspace` 刷新注册条目；`check` 经 `store.connect` 只读打开（继承 APS 索引身份与 schema 版本校验）后执行 `PRAGMA integrity_check`；`vacuum` 先校验索引身份再对原文件执行 VACUUM（autocommit 连接；SQLite 事务性保证中断安全，被占用时按失败记录跳过）。`remove` 是注册表操作（`registry.remove_entry`），`--purge` 在删除条目前先 `shutil.rmtree` 该 workspace 的 `.apsgraph/` 缓存目录（先破坏性操作后注册表变更，失败无半成品状态）。

### 3.6 APS 类型解析与数据库列映射

字段必须沿 `type → RestrictionType/SubEnum → baseTypeObj → SimpleType` 递归解析，再选择目标数据库 renderer。完整的 MySQL/Oracle/PostgreSQL 映射、长度/精度、FTL 特殊修正和 APSGraph→dbm2 校准契约见 [`aps-type-database-mapping.md`](aps-type-database-mapping.md)。

### 3.7 APSGraph 与 dbm2 设计交叉验证

`aps-metadata-db-mapping-cross-validation.md` 记录 APS XML/SQLite 到 dbm2 `TableMeta`、`ColumnMeta`、`IndexMeta` 的完整转换链、当前实现差距和后续校准契约。

### 3.5 分析模块

| 模块 | 核心算法 |
| impact | 从目标节点反向 BFS，按深度限制收集影响路径 |
| refs | 正向 / 反向 / 双向图遍历 |
| ddl / ddlgen | 模型字段解析、类型映射（含枚举子集 `<subenum>` 回溯所属枚举类型）、继承展开、方言模板 |
| dbdiff | 模型 schema 与数据库 schema 规范化后对比 |
| bridge | APS full_id 与生成 Java / CodeGraph 节点匹配 |
| classify | 关键词与路径启发式分类 |
| workbench | FTS5 维度列过滤查询、kind 组过滤、owner 链递归 CTE 子结构装配、full_id/raw_id 引用跳转解析；`--port 0` 随机空闲端口，用户级实例注册表支撑多实例 list/close |

### 3.5.1 查询工作台（workbench）

`apsgraph workbench` 用标准库 `http.server.ThreadingHTTPServer` 在 `127.0.0.1` 提供只读查询页面与 JSON API（`/api/search`、`/api/enums`、`/api/node`、`/api/children`、`/api/ddl`、`/api/top-groups`、`/api/parse-failures`、`/api/dashboard`、`/api/stats`、`/api/workspaces`；`/api/node` 载荷含 `xml_fragment`——按来源路径按需读取源 XML 并以 full_id 链定位节点，无 full_id 的容器节点沿 owner 链回溯加容器标签链定位，原始片段不写入 SQLite；文件级节点（SQL_GROUP/DICTIONARY/ERRORCONF）跳过片段展示）：

- 多 workspace 路由：显式 `--db` 为单索引模式（`_SingleDbRouter`，行为与历史版本一致）；否则进入多 workspace 模式（`WorkspaceRouter`），每个 API 请求按 `?ws=` 令牌经 `resolve_workspace` 路由到对应索引，令牌缺省时依次回退启动参数 `--workspace`、`default_selection`；`/api/workspaces` 返回注册表概览（含失效标注）与服务端默认选中项。服务端不缓存注册表——workbench 运行中新 scan 的 workspace 无需重启即可切换。dashboard 请求会触发已注册 workspace 的 `lastUsedAt` 刷新（失败仅 stderr 警告）；解析到的索引文件不存在时 fail-closed 返回 400 并提示重新 `scan`（列表仍展示该条目，前端灰显标注"失效"）。前端在侧栏品牌区下方常驻 workspace 下拉选择器（总览页也可见），点开下拉时重新拉取列表，所有 API 调用自动携带当前 `ws`；注册表工作区的 `source_root` 直接取注册的 workspace 路径，未注册宽松直开时取目录本身，显式 `--db` 模式保持"db 位于 `.apsgraph` 下取其上级"的推断。

- 总览页 `/api/dashboard` 汇总索引统计（文件/解析状态/节点/边/未解析）、SQLite 文件大小与各页面记录数（复用各分组的空关键字计数），前端以卡片呈现并支持点击跳转；
- 查询分组由 `KIND_GROUPS` 定义：枚举值（ENUM_VALUE）、数据字典（DICTIONARY，`.d_schema.xml`）、错误码（真实工程 `.error.xml` 为 `errorConf` 根，kind ERRORCONF，详情按 `errors>error` 分组展示 message 明细并支持按 message 搜索）、复合类型、字典数据项（ELEMENT 且父为 DICTIONARY、来源 `.d_schema.xml`，粒度 `BpDict.B.btch_grp_num`；排除复合类型的 element）、表、服务文件（SERVICE_TYPE）、服务（SERVICE_OPERATION，fullId 形如 `ApBatchFileService.smtbat`）、交易、批量交易、文件批量（FILE_BATCH_TRANSACTION，`.file_batch_tran.xml`）、命名SQL文件（SQL_GROUP，`.nsql.xml`，详情展示 NAMED_SQL 语句列表）、命名SQL（NAMED_SQL，粒度 `ApBatchFileSqls.upd_tb_file_tran_req`；NAMED_SQL 详情展示 parameter 子节点，并按需从源 XML 解析 SQL 文本按数据库类型展示——静态语句取 `<sql type="...">` 子元素，动态语句（dynamicSelect/dynamicSql）按 MyBatis mapper 机制原样展示每个 `<dynamicSql type="...">` 原始 XML 节点（含 str/test 等子节点）（无 type 为 NONE 且置顶；工作区由 db 路径推断——db 位于 `.apsgraph` 下时取其上级，源文件不可达时显示提示））、分片（SHARDINGSTRATEGY，`.sharding.xml`，详情展示 strategy 列表）、基础类型（RESTRICTION_TYPE，来源 `.u_schema.xml`）、常量（CONSTANT，来源 `.constant.xml`）；前端左侧菜单一二级聚合（一级为交易/服务/表/数据字典/枚举类型/基础类型，其余归入可展开的“其他”组，默认收起）；顶层模型页按 `owner_node_id IS NULL` 过滤并可按 kind 再过滤。
- 模糊搜索为子串匹配（SQL LIKE，`%`/`_` 转义为字面值），维度 `id/fullid/longname/desc` 分别命中 `raw_id`/`full_id` 与 `properties_json` 中的 `longname/name`/`description/desc/remark/message`（desc 维度包含 message 以支持错误码搜索）；fullid 维度在关键字含 `.` 时按分段层级匹配（`GnError.E0001` 命中 `GnError.Genl.E0001`）；空关键字退化为分页浏览。
- 枚举页为主从布局：`/api/enums` 按 ENUM_VALUE 的 owner（restrictionType 等）聚合出枚举列表（含枚举值数量），详情展示全部枚举值。
- 详情装配用递归 CTE 沿 `owner_node_id` 收集子结构：表的字段/公共字段表（EXTENDS 引用按序展开各公共表字段）/索引/ODB 索引/序列，服务操作的输入输出（`input`/`output` 容器为无 id 穿透节点，需在子树中定位），交易的输入输出与 flow 编排树——真实 FlowTran 的 flow 混合 `method` 直调步骤与 `case>when>service` 分支（when 带 `test` 表达式），服务端递归构建 flow 树、前端用 mermaid 渲染纵向流程图（TB 方向；`case` 为菱形判断节点，各 `when` 分支从 case 分叉并以条件名命名，分支内服务纵向串联，分支出口汇合到下一个步骤或结束节点，渲染失败时降级为缩进列表），`serviceName`/`transactionId` 以 full_id/raw_id 兜底解析为可跳转目标；错误码按 `errors` 分组展示 `error` 明细（表头 `id,类型,错误码,参数,message`，`参数` 为该错误的 parameter 子节点 id 列表）；另有错误码数据项分组（kind ERROR，详情展示 parameter 子节点）。
- 多实例支持：`WorkbenchServer` 默认绑定端口 0，由操作系统分配随机空闲端口（URL 与实例注册表均记录实际端口），显式 `--port N` 可固定端口；绑定失败时以 JSON 错误退出（fail-closed，不自动降级）。Windows 上必须禁用 `SO_REUSEADDR`（`allow_reuse_address=False`，仅非 Windows 保留）——Windows 的 `SO_REUSEADDR` 允许重复绑定同一活动端口，两个实例会同时"成功"监听且不报任何冲突，请求被随机分流；POSIX 保留该选项仅用于快速重启绕过 TIME_WAIT。每个实例启动后把 `{port, pid, url, db, workspace, started_at}` 原子写入（临时文件 + `os.replace`）用户级注册表 `~/.apsgraph/workbench-registry.json`（环境变量 `APSGRAPH_WORKBENCH_REGISTRY` 可覆盖，测试与多用户隔离用），正常退出（Ctrl+C；POSIX 下 SIGTERM 经信号处理器转为 KeyboardInterrupt）时注销自身条目。
- `workbench list`：读取注册表，对每个条目做双重存活校验——POSIX `os.kill(pid, 0)` / Windows `tasklist`（Windows 的 `os.kill` 非 CTRL 信号一律 TerminateProcess，不可用于探测）加 `/api/stats` HTTP 探测；存活实例按端口排序输出 JSON，失效条目就地清理并计入 `pruned_stale`（注册表自愈）。
- `workbench close`：先探测目标端口是否仍在响应 workbench `/api/stats`（确认注册表条目仍对应本工具实例，防止 pid 复用误杀无关进程），确认后 `os.kill(pid, SIGTERM)` 终止（Windows 映射为 TerminateProcess，只读服务无状态可刷写），随后从注册表移除条目；pid 已死按 `already_stopped` 清理、端口不再服务按 `stale` 清理（不杀进程）、终止抛错按 `failed` 保留条目、指定端口不存在按 `not_found` 返回退出码 2；`--all` 迭代全部条目。`close` 侧自行删除注册表条目，因此 Windows 上进程被硬终止也不会残留过期记录。
- 前端为 `workbench_static/` 下的单页应用（vanilla HTML/JS/CSS），随 wheel 以 package-data 分发；mermaid 流程图库（mermaid@10.9.1 minified）同样打包进 `workbench_static/`，离线可用，加载失败时 flow 自动降级为缩进列表；流程图画布默认 0.6 缩放，提供工具栏（放大/缩小/重置/全屏）、滚轮缩放与拖拽平移（CSS transform 实现，pointer capture 拖拽），全屏为 fixed 弹层并提供“✕ 关闭全屏”按钮；左侧菜单可收起，结果列表默认 1/4 宽并由分隔条拖拽调节；结果分页（每页 50）、子孙树懒加载；结果列表只展示 `fullId/id：中文名`，详情属性过滤带命名空间的 XML 属性（如 `xsi:noNamespaceSchemaLocation`）。`/api/ddl` 复用 `ddlgen.generate_all_ddl` 对单个 TABLE 节点生成 MySQL/Oracle/PostgreSQL 建表语句（只生成、不执行，与 `ddl-gen` 同一实现），并用 `validate_ddl_sql` 以 sqlglot 按对应方言解析校验（可选依赖，惰性导入，未安装时返回跳过而非失败），前端在表详情提供弹窗、校验结论展示与一键复制；搜索栏位于中栏顶部，直接过滤中间结果列表；详情表格列按元数据模型对象定义（输入输出/数据项为 `字典ID,字段,中文名,类型,必填,多值,默认值,固定值,描述,别名`，表字段为 `字典ID,字段,DbName,中文名,类型,可为空,默认值,描述,是否主键`；数据项行的 `字典ID` 在 ref 缺省时回退为节点自身 full_id），值符合模型 fullId 形态（正则 `^[A-Z]\w*(\.\w+)+$`，排除 Java 包名等小写开头值）的属性一律渲染为节点跳转链接（复用 `/api/node` 的 full_id/raw_id 兜底解析）；详情面板维护跳转历史栈并提供返回上一级按钮，页面切换时清空。

### 3.6 原始 XML 与语义节点索引

扫描不采用“只保留顶层模型”或“每个 DOM 标签全部建节点”两种极端方案，而采用文件完整证据与语义投影分离：

```text
model_files（文件元数据、hash、解析状态）
  └── nodes（顶层模型 + 有业务语义的嵌套节点）
        └── edges（包含、引用、调用、读写和证据）
```

`nodes` 继续保存 `owner_node_id` 层级，并优先索引具备稳定 id、可被引用、需要 UI 查询或参与关系分析的对象，例如 `TABLE`、`FIELD`、`INDEX`、`TRANSACTION`、`SERVICE`、`NAMED_SQL`、`PARAMETER`、`FLOW_NODE`。无业务语义的 XML 容器标签不单独建节点，但其属性保留在所属节点的 `properties_json` 中。

索引只保存 XML 文件元数据、语义节点和关系证据，不复制原始 XML 内容。

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
| `model_search` | 由节点标识和描述属性构成的 SQLite FTS5 搜索索引 |
| `edges` | 节点关系、raw target、证据文件、证据值、置信度 |
| `scan_state` | workspace、scanner 版本、同步状态 |

### 4.2 ID 设计

- `stable_id`：按 `model:{kind}:{workspace 相对路径}#{语义 ID}` 生成；普通节点不依赖 ordinal，只有同一文件、同一 kind、同一语义 ID 重复时才追加确定性的 `~1`、`~2` 消歧后缀。
- `full_id`：面向模型引用解析，如 `Base.U_NAME`。
- workspace 文件使用相对路径。
- 归档 XML 使用 `jar:<archive>!<entry>`。
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

## 5. 扫描与索引设计

- `scanner` 递归发现受支持的 XML 文件，解析语义节点、属性和关系并写入 SQLite。
- `scan` 通过 staging 数据库完成全量重建，`sync` 依据路径和 SHA-256 增量更新。
- `scan` 和 `sync` 将进度写入 stderr：逐文件解析以单行进度条（`\r` 原地刷新，含阶段、进度条、已处理数/总数、百分比与当前文件）呈现，其他阶段为普通单行日志；扫描/同步完成后在 stderr 输出一条汇总（文件解析结果、节点/边数量、未解析引用与警告数、耗时）。`scan` 完成后 stdout 不再输出 JSON，警告明细仅在使用 `--show-warning` 时输出到 stderr；其他命令的 JSON 结果仍只写 stdout。
- 外部 SQLite 索引通过 `--external-db` 合并，workspace 定义优先。

## 6. 并发与性能设计

### 6.1 扫描流程

```text
Discover XML  ->  Parse semantic nodes  ->  Resolve edges  ->  Publish SQLite
```

约束：

- SQLite 写入集中在单连接事务中，避免写冲突；
- 全量扫描使用 staging 数据库，成功后原子替换。

### 6.2 扫描性能

- 先按后缀过滤，避免解析无关 XML。
- 排除生成目录。
- workspace 文件按路径排序，保证确定性。
- 全量构建 staging DB，成功后原子替换。
- 增量同步使用 SHA-256 只重建变更、删除文件。

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

- 非法索引或跨 workspace 索引；
- 外部索引不可读或与目标索引冲突；
- 注册表文件损坏或结构非法（报错提示，不静默重建）；
- workbench 裸跑时注册表为空且当前目录无索引（提示先 `scan`）；
- workbench 选中 workspace 的索引文件不存在（返回错误提示重新 `scan`，注册条目不自动剔除）。

### 7.3 容忍场景

- 普通 workspace XML 解析错误：默认记录并继续。
- 普通 scan unresolved：警告并继续。
- 归档文件无受支持 XML 模型：记录 skipped，不算失败。

## 8. 安全设计

| 风险 | 设计 |
|---|---|
| 误改源码 | scanner 只读 |
| 误执行 DDL | DDL 只输出文件或 stdout |
| 数据库密码泄漏 | 支持 DSN 环境变量，输出脱敏 |
| 索引误替换 | staging + 原子发布 |
| CodeGraph 污染 | 只读连接 |
| 命令注入 | DB 参数通过参数化 SQL 传递 |
| workbench 暴露面 | 仅绑定 127.0.0.1、GET-only、索引只读打开、静态资源防路径穿越 |
| 全局注册表写入面 | `~/.apsgraph` 下仅实例注册表与 workspace 注册表两处用户级文件；workspace 注册表只由 `scan` upsert 与 workbench `lastUsedAt` 刷新写入，原子替换写，不承载任何索引或源码数据 |

## 9. 可观测性设计

- stdout：JSON，面向机器。
- stderr：阶段与项目进度，面向人。
- 错误证据：文件路径、XML tag、属性值。

## 10. 扩展设计

### 10.1 MCP 包装

MCP Server 可以复用 CLI 或 Python API，不重复实现业务：

| MCP tool | 底层能力 |
|---|---|
| `apsgraph_options` | options |
| `apsgraph_scan` | scan / external-db |
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
