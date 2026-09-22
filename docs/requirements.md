# APSGraph 需求文档

> 版本：0.45.2
> 更新时间：2026-09-22  
> 文档定位：本文件是 APSGraph 的长期需求基线，汇总产品定位、用户需求、功能需求、非功能需求与演进需求。新增需求必须先更新本文，再进入设计与实现。

---

## 1. 产品定位

APSGraph 是面向 APS 元数据模型的只读分析工具，把工作空间中的 XML 模型解析为 SQLite 关系图，为影响分析、DDL 生成、数据库差异审计、CodeGraph 桥接、能力分类和技术文档导出提供统一索引。

APSGraph 不修改业务源码，不执行 DDL，不写入 CodeGraph 数据库。

## 2. 用户与场景

| 用户 | 主要诉求 |
|---|---|
| 业务/平台研发 | 快速定位模型定义、字段引用和变更影响 |
| 架构师 | 获得模型、生成代码与功能能力的全局视图 |
| DBA / 数据库工程师 | 从模型生成多方言 DDL，并审计模型与实际库差异 |
| 迁移/运维工程师 | 为 dbm2 提供模型校准索引，维护扫描任务和产物 |
| AI 编码代理 | 通过 CLI / SQLite 索引 / CodeGraph 桥接理解模型影响面 |

## 3. 总体需求

### R1 打包与命令化

- 项目必须提供标准 Python 包配置和 `apsgraph` CLI 命令。
- 核心功能不得依赖第三方 Python 运行时库。
- Excel 导出作为可选 extra：`apsgraph[excel]`。
- `requirements.txt` 不作为核心安装入口；依赖声明以 `pyproject.toml` 为准。
- CLI 支持 `--version` 查看版本，支持 `options` 查看默认参数、当前版本和 workspace 生效规则。

### R2 默认路径规则

- 默认 workspace 是当前工作目录。
- 默认数据库为 `<workspace>/.apsgraph/apsgraph.db`。
- 默认缓存目录为 `<workspace>/.apsgraph`。
- 全局 workspace 注册表为 `~/.apsgraph/registry.json`（可用环境变量 `APSGRAPH_HOME` 重定向注册表目录，用于测试与 CI 隔离）。
- 用户可通过 `--workspace`、`--db` 覆盖默认值。

### R3 XML 模型索引

- 扫描 workspace 中被识别的 APS XML 模型文件。
- 将模型、字段、服务、交易、批量步骤等对象写入 SQLite V2 索引。
- 建立 `TYPE_REF`、`DICT_REF`、`EXTENDS`、`CONTAINS` 等关系边。
- 只扫描源文件，排除 `target`、`.git`、`.apsgraph`、`.codegraph`、`node_modules` 等生成或工具目录。
- 支持全量 `scan` 与基于 SHA-256 的增量 `sync`。
- 解析错误默认记录并继续；指定 `--fail-on-parse-error` 时失败退出。
- 普通扫描中 unresolved 引用是警告，不阻断扫描。

### R4 外部索引复用

- 支持通过 `scan --external-db DB` 合并其他 APSGraph XML 扫描生成的 SQLite 索引。
- workspace 定义优先于外部索引中的同名模型。
- 外部索引仅作为只读输入，不执行构建或依赖解析。

### R5 安全发布

- SQLite 索引通过 staging 数据库构建并原子替换。
- XML 解析失败时按配置记录或终止，失败不会替换既有索引。
- 不修改业务源码，不执行 DDL，不写入 CodeGraph 数据库。

### R14 查询与影响分析

- `search`：基于 SQLite FTS5 模糊搜索模型 `id`、`full_id`、中文名称和描述，并返回 `kind`、匹配的 `full_id`、中文名称、描述及文件路径。
- `stats`：查看索引统计。
- `show`：查看模型详情。
- `refs`：查询入向、出向或双向引用。
- `impact`：反向追溯变更影响。
- 查询输出必须附带证据路径和属性。

### R15 DDL 与数据库审计

- 支持单表 `ddl` 预览和批量 `ddl-gen`。
- 支持 MySQL、Oracle、PostgreSQL、TDSQL、GoldenDB 方言；TDSQL/GoldenDB 为 MySQL 家族分布式方言。
- 分布式方言支持表分片类型选择（分片表/广播表/复制表）：分片表需选择分片键，分片键选择需提示字段的主键与唯一索引归属；TDSQL 要求唯一索引（含主键）包含分片键、GoldenDB 要求分片键包含在主键中，违反时以警告提示（不阻塞生成）。
- 支持 RANGE 按日分区表生成（全部方言；PostgreSQL 分区定义为独立 `PARTITION OF` 语句，Oracle 界值用 `TO_DATE`/`TO_TIMESTAMP`）：分区键按类型与方言自动选择转换函数（MySQL 家族日期 `TO_DAYS`、时间戳 `UNIX_TIMESTAMP`、yyyymmdd 字符串 `RANGE COLUMNS`、整数直接列），分区定义由起始/终止日期（yyyymmdd）生成，终止日期留空以 MAXVALUE（PG 为 `default` 分区）兜底；弹窗支持快捷推算——输入基准日期、粒度（D/M/Y）与前置/后置分区数，自动推算并填充起止日期；分片/分区选择仅作用于本次生成，不写回元数据。
- DDL 只生成，不执行。
- `db-diff` 支持模型与实际数据库 schema 差异对比。
- 在线连接密码优先从环境变量读取，输出中脱敏。

### R16 CodeGraph 桥接

- 通过只读 CodeGraph 索引查找 APS 模型对应的生成 Java 类和消费者。
- 输出模型、生成类、调用方之间的桥接关系和证据。
- 不写 CodeGraph 数据库。

### R17 能力分类与文档导出

- `classify` 基于 ID、描述、路径、Java 包名、类名等证据进行能力分类。
- `doc-export` 输出 Markdown 文档。
- `xlsx-export` 输出 Excel 文档，需要可选依赖 `openpyxl`。
- 启发式分类结果必须允许人工复核。

### R19 查询工作台

- `workbench` 在本地浏览器提供元数据查询工作台：只读、仅绑定 `127.0.0.1`、纯标准库实现、不依赖第三方运行时库。
- 支持同时启动多个工作区的 workbench 实例：默认由操作系统分配随机空闲端口（等价 `--port 0`），实际端口在启动日志 URL 中展示并记录到实例注册表；`--port N` 显式指定固定端口，端口不可绑定时必须报错（Windows 禁用 `SO_REUSEADDR` 重复绑定同一活动端口）。
- 提供跨工作区的实例管理命令：`workbench list` 列出正在运行的实例（端口、PID、URL、索引、工作区、启动时间，自动清理过期条目），`workbench close --port N | --all` 停止指定或全部实例；实例注册表位于用户主目录（`~/.apsgraph/workbench-registry.json`，可用 `APSGRAPH_WORKBENCH_REGISTRY` 覆盖），close 只终止仍在响应 workbench API 的进程，不得误杀无关进程。两个命令与 workspace 命令族一致：默认人类可读输出（list 为对齐表格，close 为逐实例单行报告），`--json` 输出机器可读格式。
- 支持按顶层模型查询并展示递归包含的子模型树（懒加载）；支持表、服务文件（SERVICE_TYPE）、服务（SERVICE_OPERATION，按 `服务文件.服务id` 维度搜索）、交易、批量交易、文件批量（FILE_BATCH_TRANSACTION）、命名SQL文件（SQL_GROUP，详情展示语句列表）、命名SQL（NAMED_SQL，粒度 `ApBatchFileSqls.upd_tb_file_tran_req`，详情展示 parameter 参数与按数据库类型区分的 SQL 语句文本（动态SQL按 MyBatis mapper 机制原样展示 `<dynamicSql>` 原始 XML 节点；无 type 的默认为 NONE 并置顶））、分片（SHARDINGSTRATEGY，详情展示分片策略）、复合类型、数据字典（仅字典文件 `.d_schema.xml` 中 DICTIONARY 下的 element，粒度 `BpDict.B.btch_grp_num`，不含复合类型的 element）、错误码（kind ERROR，粒度 `GnError.Genl.E0001`；fullId 维度支持按 `.` 分段层级筛选）、枚举类型、基础类型、错误码文件、常量（`.constant.xml` 中 constantConf>constants>constant，粒度 `CfConst.Busi.CONST_XXX`，支持按 message/description 模糊搜索）的独立查询页；基础类型页查询 `.u_schema.xml` 中定义的 RESTRICTION_TYPE（如 `ApBaseType.U_ADDR`），详情展示属性与枚举值。
- 枚举类型页为主从布局：列表展示枚举 FullId 与枚举值数量，详情展示枚举值明细。
- 错误码页查询对象为 `.error.xml` 的 `errorConf`（kind ERRORCONF），详情按 `errors` 分组展示 error 明细（ID、类型、message），并支持按 message 模糊搜索。
- 搜索栏（维度下拉、关键字、查询）位于中栏顶部，过滤直接作用于中间结果列表。
- 表详情提供“生成 DDL”按钮：弹窗选择 MySQL / Oracle / PostgreSQL / TDSQL / GoldenDB 后生成建表语句并支持一键复制；选择 TDSQL / GoldenDB 时提供表分片类型选择，选择分片表后提供分片键下拉（字段选项标注主键/唯一索引归属，存在未含分片键的唯一索引时显示警示），GoldenDB 提供节点组输入框（默认 `g1,g2,g3,g4`）；提供“创建分区表”勾选（全部方言），勾选后选择分区键（按类型标注转换函数）、输入起始/终止日期（yyyymmdd，按日生成分区，终止留空以 MAXVALUE 兜底），并支持基准日期+粒度（D/M/Y）+前置/后置分区数的一键推算起止日期；弹窗选择仅作用于本次生成，不写回模型；DDL 只生成、不执行。
- 生成的 DDL 必须用 sqlglot 按对应方言做语法校验并展示结论；sqlglot 为可选依赖，未安装时必须明确提示跳过而不是静默通过。
- 各元数据模型的详情面板必须在“引用（出）”前展示“原始 XML 片段”：通过索引记录的来源路径按需读取源 XML 定位节点并原样展示（不写入 SQLite）；文件级节点（命名SQL文件、数据字典文件、错误码文件）的片段与源文件几乎相同，不展示。
- 总览页（dashboard）展示各元数据类型的数量卡片（点击跳转对应查询页），以及当前 SQLite 文件大小、文件数、解析成功/失败数、节点数、边数与未解析引用数。
- 每个查询页提供查询输入框和查询类型（维度），支持按 `id`、`fullId`、`longname`、`desc` 单维度或全部维度的子串模糊搜索（部分英文标识符与短中文子串均可命中），结果分页展示；结果列表只展示 `fullId/id：中文名`，完整属性在详情面板展示（过滤 `xsi:` 等命名空间属性）。
- 详情面板按类型结构化展示，表格列按元数据模型对象定义：交易/服务的输入输出、复合类型与数据字典的数据项按 `字典ID, 字段, 中文名, 类型, 必填, 多值, 默认值, 固定值, 描述, 别名` 展示；表的字段按 `字典ID, 字段, DbName, 中文名, 类型, 可为空, 默认值, 描述, 是否主键` 展示。表在字段与物理索引之间按引用顺序展示其引用的公共字段表（可多个，含各公共表字段明细，标题链接可跳转，未解析引用灰色显示）；另展示物理索引、ODB 索引、序列；服务文件展示各服务操作的输入/输出；服务展示其输入/输出；交易展示输入、输出与流程编排——流程编排用 mermaid 渲染纵向流程图——`method` 直调步骤按序串联；`case` 为菱形判断节点，各 `when` 分支从 case 分叉并以条件名命名，分支内服务纵向串联，分支出口汇合到下一步骤；渲染不可用时降级为列表；批量交易展示输入字段、批量步骤与步骤组；错误码文件按分组展示明细，表头为 `id, 类型, 错误码, 参数, message`（错误码列为完整 fullId，参数为该错误码的 parameter 子节点）。数据字典/复合类型详情的数据项行 `字典ID` 在 ref 缺省时展示自身 fullId（如 `BpDict.A.addr`）。
- 凡值符合模型 fullId 形态（大写开头点分）的属性，包括字段表格中的 `类型`/`字典ID` 列与详情属性区，必须渲染为可点击链接并跳转到目标详情（限制类型/枚举、复合类型、表、字典数据项等）；详情面板提供“返回上一级”按钮，沿跳转历史逐级返回，切换页面时历史清空。
- 已解析引用必须可点击跳转到目标详情；未解析引用显示原始目标。
- 布局交互：左侧菜单一二级聚合，一级菜单为交易、服务、表、字典数据项、枚举类型、基础类型，其余页面（含文件批量、命名SQL文件、命名SQL、分片）统一归入可展开的“其他”分组（默认收起）；左侧菜单可收起/展开；结果列表默认占窗口宽度 1/4 且可通过分隔条拖拽调节；mermaid 流程图画布默认 0.6 缩放，支持放大/缩小/重置/全屏、滚轮缩放与拖拽平移；全屏弹层必须提供关闭按钮。
- 索引不存在时 fail-closed 报错提示先执行 `scan`；工作台不得提供任何写能力。

### R20 全局 workspace 注册

- `scan` 成功发布索引后自动把 workspace 注册到全局注册表 `~/.apsgraph/registry.json`（与实例注册表 `workbench-registry.json` 相互独立）；`--no-register` 可跳过注册。
- 注册表以 workspace 绝对路径为唯一键：重复扫描同一 workspace 是刷新（更新 `dbPath`、`lastScanAt`）而非新增；显示名 `name` 默认取目录 basename，允许重名。
- 注册表字段：`id`（workspace 解析路径的 SHA-1 前 8 位十六进制短 id，确定性——同一路径恒得同一 id，注册表重建后保持稳定，旧条目由读取方即时计算兜底）、`name`、`workspacePath`、`dbPath`、`lastScanAt`、`lastUsedAt`。
- 注册只发生在成功的 `scan`；`sync` 与失败的 `scan` 不修改注册表。
- 注册表写入采用临时文件 + 原子替换；注册表文件损坏或结构非法时 fail-closed 报错，不得静默重建。
- workbench 服务 workspace 时刷新被使用注册 workspace 的 `lastUsedAt`；该刷新失败只记录 stderr 警告，不中断浏览。
- 全局注册表目录可用环境变量 `APSGRAPH_HOME` 重定向（测试/CI 隔离）。

### R20a 多 workspace 查询工作台

- `workbench` 不带 `--db` 时进入多 workspace 模式：读取全局注册表，在同一个本地服务内提供所有已注册 workspace 的查询，UI 侧栏提供 workspace 下拉选择器（展示名称与最后扫描时间，索引已失效的条目标注"失效"并灰显）。
- 裸跑 `workbench` 的默认选中顺序：当前目录是已注册 workspace → 选中它；否则当前目录存在 `.apsgraph/apsgraph.db` → 选中当前目录；否则选中上次使用的（`lastUsedAt` 最新，其次 `lastScanAt` 最新）；注册表为空且当前目录无索引时 fail-closed 提示先 `scan`。
- `--workspace <id|名称|路径>` 启动直达：先按注册表 id 精确匹配，再按 name 精确匹配（重名命中多个时报错并列出带 id 的候选），再按解析后的 workspace 路径匹配；指向真实存在目录但未注册时，若 `<目录>/.apsgraph/apsgraph.db` 存在则按只读方式直接服务（不自动注册）；均未命中时报错并列出可用注册名（name(id) 形式）。
- 显式 `--db` 时保持单索引模式（优先级最高），不读 workspace 注册表；随机端口与实例注册行为与单索引模式一致。
- 服务端每个请求重新读取注册表：workbench 运行中新 `scan` 的 workspace 无需重启即可切换。
- 注册表中索引已失效的条目在列表中正常展示但标注失效；选中时 fail-closed 报错提示重新 `scan`，条目不自动剔除。
- 多 workspace 模式同样只读、仅绑定 `127.0.0.1`；除注册表 `lastUsedAt` 刷新外无任何写操作。

### R20b workspace 维护命令族

- 提供二级命令族 `apsgraph workspace <action>`，基于全局 workspace 注册表对注册 workspace 做条目管理与索引维护；命令目标必须显式：单目标 `--workspace <id|名称|路径>`（仅在注册表内匹配：id 精确、name 精确、重名报错列带 id 候选、路径精确匹配；不适用 workbench 的未注册目录宽松直开规则），批量 `--all`（注册表为空时报错）。
- `workspace list`（只读）：列出全部注册条目（name、workspacePath、dbPath、lastScanAt、lastUsedAt、索引可用性），按最近使用排序。
- `workspace remove --workspace <token>`（写注册表）：删除注册条目；`--purge` 显式声明时先删除该 workspace 的 `.apsgraph/` 缓存目录再删条目（索引文件是用户数据，未指定 `--purge` 时绝不触碰）；未注册 token 报错退出，不允许静默成功。
- `workspace status --all|--workspace`（只读）：逐 workspace 报告 `missing`（索引或源码目录缺失，附 detail）/ `stale`（源码有变更，附 added/modified/deleted，复用现有 change-set 逻辑）/ `fresh` 三态。
- `workspace sync --all|--workspace`（写索引）：批量增量同步，薄包装现有 `sync_workspace`（跨 workspace 校验与 SHA-256 增量语义继承）；索引缺失的条目按失败记录。
- `workspace check --all|--workspace`（只读）：索引健康检查——以只读方式打开（继承 APS 索引身份与 schema 版本校验）并执行 `PRAGMA integrity_check`，输出 `ok` / `missing` / `corrupt` 及 schema 版本。
- `workspace rebuild --all|--workspace`（写索引）：全量重建，薄包装现有 `scan_workspace`（staging + 原子替换继承），成功后刷新对应注册条目的 `lastScanAt` 与 `dbPath`。
- `workspace vacuum --all|--workspace`（写索引）：对索引原地执行 VACUUM（执行前先校验 APS 索引身份；SQLite 事务性保证中断不损坏索引）；数据库被占用（如正被 workbench 服务）时该 workspace 记为失败，不得阻塞其他 workspace。
- 批量动作统一 fail-soft：逐 workspace 执行，单点失败（含损坏、锁定、目录缺失导致的无法执行）记录进结果继续其余；任一 workspace 失败则退出码 2。报告型结果（missing/stale/fresh/ok）不算执行失败。
- 输出格式：本命令族默认输出人类可读的对齐表格（list/status/check/sync/rebuild/vacuum）或单行确认（remove，含失败 stderr 汇总行），`--json` 输出机器可读 JSON（`results` 数组 + `ok` 汇总标志 / `workspaces` / `removed`）；表格必须对中文全角字符做终端列宽对齐，错误信息在 NOTE 列截断展示。这是 stdout 机器可读 JSON 总约定中 workspace 族按需人类化的显式例外。
- 本命令族明确不提供：注册条目重命名、孤儿索引扫描、check 后自动修复（修复走 `rebuild`）。

### R18 版本与发布流程

每次行为或文档基线修改必须完成：

1. 升级版本号；
2. 更新文档；
3. 运行全量测试；
4. 提交 Git；
5. 构建 wheel；
6. 安装 CLI；
7. 验证 `apsgraph --version` 与 `options`；
8. 提交代码并推送到远端分支。

| 维度 | 需求 |
|---|---|
| 性能 | 大型 workspace 全量扫描应在分钟级完成；单次查询应在秒级返回 |
| 可扩展性 | 支持 3600+ workspace XML、500+ 依赖文件、10万+ 节点规模 |
| 可靠性 | fail-closed；失败不破坏现有索引 |
| 可观测性 | 阶段日志、项目日志、manifest、错误证据 |
| 安全性 | 只读源码；不执行 DDL；密码脱敏；拒绝非法索引 |
| 可维护性 | 五类核心文档长期维护，支持文档保鲜 |
| 兼容性 | Python 3.9+；核心功能零运行时依赖 |
| 确定性 | 输出排序稳定；相同输入生成稳定关系 |

## 5. 演进需求

| 编号 | 需求 | 状态 |
|---|---|---|
| E1 | MCP Server 包装，暴露 scan/status/impact/refs 等工具 | 规划中 |
| E2 | Web 可视化模型图与影响路径 | 规划中 |
| E3 | 模型索引版本对比 | 规划中 |
| E4 | CI 定时扫描与 unresolved 基线告警 | 规划中 |
| E5 | 分片表 DDL 展开 | 规划中 |
| E6 | 更多数据库 db-diff 驱动 | 规划中 |
