# APSGraph 使用说明

> 版本：0.38.0
> 更新时间：2026-09-17
> 项目地址：https://github.com/NeverMoreLyh/aps-model-tools

---

## 1. 项目简介

APSGraph 是一套面向 APS 元数据模型的分析工具集，提供从源码仓库扫描 XML 模型、构建紧凑 SQLite 关系索引、增量同步、影响分析、DDL 生成、模型与数据库差异对比、CodeGraph 桥接、能力分类审计等能力。

**核心特征**：
- **只读扫描**：不修改源码仓库任何文件
- **SQLite V2 索引**：紧凑二进制关系图，支持跨文件引用解析
- **增量同步**：基于 SHA-256 文件指纹，仅处理变更文件
- **多方言 DDL**：MySQL / Oracle / PostgreSQL 建表脚本生成
- **Fail-closed**：DDL 生成是实验性预览，永不自动执行

---

## 2. 环境要求

| 组件 | 版本 |
|---|---|
| Python | ≥ 3.9 |
| 操作系统 | macOS / Linux / Windows |
| 核心依赖 | 无第三方运行时依赖（纯标准库） |
| 可选 | `sqlglot`（db-diff 与 workbench DDL 校验需要）；`openpyxl`（xlsx-export 需要） |

---

## 3. 安装

### 3.1 install.sh 一键安装（推荐）

仓库根目录提供 `install.sh`，自动构建 wheel 并以 pip 安装（自动适配 PEP 668 等环境限制）：

```bash
./install.sh
```

### 3.2 pip 安装

```bash
# 从构建好的 wheel 安装
python3 -m pip wheel . --no-deps -w dist
python3 -m pip install --force-reinstall dist/apsgraph-<VERSION>-py3-none-any.whl

# 开发模式
pip install -e .

# 如需 Excel 导出
pip install -e ".[excel]"
```

### 3.3 从 Git 仓库安装

```bash
pip install git+https://github.com/NeverMoreLyh/aps-model-tools.git
```

### 3.4 直接运行（无需安装）

```bash
export PYTHONPATH=src
python3 -m apsgraph --help
```

安装后可用 `apsgraph` 命令：

```bash
apsgraph --help
```

---

## 4. 命令一览

| 命令 | 说明 |
|---|---|
| `scan` | 全量扫描工作空间，构建/重建 SQLite V2 索引 |
| `sync` | 增量同步源码变更到已有索引 |
| `options` | 查看当前版本、默认参数与 workspace 生效规则 |
| `status` | 查看工作空间与索引的同步状态 |
| `stats` | 查看索引统计信息 |
| `show` | 查看指定模型的完整属性和直接关系 |
| `search` | 使用 SQLite FTS5 模糊搜索 ID、full_id、中文名称和描述 |
| `refs` | 查询模型的引用关系（入/出/双向，可指定深度） |
| `impact` | 变更影响分析（反向追溯受影响节点） |
| `ddl` | 单表 DDL 预览（实验性） |
| `ddl-gen` | 批量生成 MySQL/Oracle/PostgreSQL DDL |
| `db-diff` | 模型与实际数据库 schema 差异对比 |
| `bridge` | 模型 → 生成 Java → CodeGraph 消费者桥接 |
| `classify` | 按功能能力审计模型与 Java 包分类 |
| `doc-export` | 导出 Markdown 模型文档 |
| `xlsx-export` | 导出 Excel 模型文档 |
| `serve-mcp` | 以 stdio MCP 服务暴露元数据工具 |
| `workbench` | 启动本地浏览器查询工作台（只读，127.0.0.1；`--port 0` 随机端口） |
| `workbench list` | 列出正在运行的 workbench 实例（端口、PID、工作区、索引） |
| `workbench close` | 按端口或全部停止运行中的 workbench 实例 |

---

## 5. 命令详细说明

### 5.0 options — 查看版本与参数默认值

```bash
apsgraph --version
apsgraph options
apsgraph options --workspace /path/to/workspace
```

`--version` 输出当前安装版本。`options` 是只读命令，输出 JSON，包含：

1. 当前 `version`；
2. 当前版本内置默认值：workspace、数据库和缓存目录；
3. 指定 workspace 的外部索引规则。

该命令不会创建索引或缓存目录。完整命令行参数仍可通过 `apsgraph --help` 或 `apsgraph COMMAND --help` 查看。

### 5.1 scan — 全量扫描构建索引

```bash
apsgraph scan
```

| 参数 | 说明 |
|---|---|
| `--workspace` | APS 源码仓库根目录（默认当前目录） |
| `--db` | SQLite 索引输出路径（默认 `.apsgraph/apsgraph.db`） |
| `--fail-on-parse-error` | 遇到解析错误时立即终止（默认跳过并记录） |
| `--external-db` | 合并其他 XML 扫描生成的 APS SQLite 索引，可重复 |
| `--show-warning` | 扫描汇总后在 stderr 列出未解析引用警告明细（默认只显示数量） |

普通 `scan` 只解析 workspace XML 并写入 SQLite；未解析引用不会导致失败，缺失模型名仅以警告数量计入汇总（需要明细时用 `--show-warning` 在 stderr 列出）。使用 `--external-db` 时可合并其他 XML 扫描生成的 SQLite 索引。扫描器不会把 error 描述、SQL/Java primitive、`class` / `resultClass` Java 类名当作 APS 模型引用，并会把空格分隔的多值 `extension` 拆成多条 EXTENDS 边。

扫描和同步的进度以单行进度条写入 stderr（原地刷新，不刷屏），完成后输出一条汇总信息（文件解析结果、节点/边/未解析引用与警告数、耗时）；`scan` 的 stdout 不再输出 JSON，汇总即最终输出；其他命令的 JSON 仍只写 stdout。

**扫描的文件类型**（27 种 XML 后缀）：
`.tables.xml`、`.parms.xml`、`.flowtrans.xml`、`.nsql.xml`、`.batchStep.xml`、`.batchgroup.xml`、`.serviceType.xml`、`.serviceImpl.xml`、`.sharding.xml`、`.workflow.xml` 等。

**自动排除目录**：`.git`、`target`、`.idea`、`.codegraph`、`.hermes`、`node_modules`。

### 5.2 sync — 增量同步

```bash
apsgraph sync
```

对比文件归一化相对路径和 SHA-256 哈希，在一个事务内处理新增/修改/删除文件，然后重新绑定跨文件引用。

### 5.3 status — 同步状态

```bash
apsgraph status
```

返回工作空间与索引的文件差异统计。

### 5.4 外部索引复用

```bash
apsgraph scan --external-db /path/to/shared-index/.apsgraph/apsgraph.db
```

外部索引中的模型使用 `external-db:<数据库>!<模型文件>` 逻辑路径；workspace 同名 `full_id` 优先，`sync` 不会删除外部模型。

### 5.5 stats — 索引统计

```bash
apsgraph stats
```

返回节点数、边数、文件数、未解析引用数等统计信息。

### 5.6 show — 查看模型详情

```bash
apsgraph show SysDbTable.kapp_sundry_busi
```

返回指定模型的完整属性（kind、full_id、properties）和一级关系。

### 5.7 search — 模糊搜索模型

当不知道精确 ID 时，可搜索模型 ID、`full_id`、中文名称或描述。中文支持短语和部分匹配：

```bash
apsgraph search "账户类型"
apsgraph search "开户" --limit 20
apsgraph search "account_type" --db .apsgraph/apsgraph.db
```

`search` 和 `find` 均支持范围过滤：

```bash
apsgraph search "编码" --kind FIELD --project dept-parent --module dept-bcs --path '*/src/main/resources/tables/**' --limit 20
apsgraph find 'account_type' --kind TABLE --file 'Account.tables.xml'
```

可用过滤项：`--kind`（可重复）、`--project`、`--module`、`--path`（SQLite glob）、`--file`、`--owner`、`--top-level`。


### 5.8 refs — 引用关系查询

```bash
apsgraph refs BpDict.A.addr --direction both --depth 2
```

| 参数 | 说明 |
|---|---|
| `query` | 模型查询标识（full_id 或 raw_id） |
| `--direction` | `in`（谁引用了我）/ `out`（我引用了谁）/ `both`（默认） |
| `--depth` | 遍历深度（默认 1，最小 1） |

### 5.9 impact — 变更影响分析

```bash
apsgraph impact BpDict.A.addr --depth 3
```

从目标模型反向追溯受影响节点，按关系类型汇总，输出受影响节点列表、影响路径和未解析引用。

### 5.10 ddl — 单表 DDL 预览

```bash
apsgraph ddl kapp_sundry_busi --dialect mysql ```

生成单张表的建表 DDL（实验性，fail-closed，不自动执行）。

### 5.11 ddl-gen — 批量 DDL 生成

```bash
apsgraph ddl-gen --dialect mysql --output output/schema.sql --report output/report.json
```

| 参数 | 说明 |
|---|---|
| `--dialect` | `mysql` / `oracle` / `postgresql`（默认 mysql） |
| `--tables` | 可选，指定表名列表；默认生成全部 TABLE 节点 |
| `--output` | 输出 SQL 文件路径 |
| `--report` | 输出 JSON 报告（警告/错误/统计） |
| `--text-threshold` | varchar 长度 ≥ 此值时转为 text/clob（默认 1000） |
| `--db-ratio` | byCharacter=true 类型的长度倍率（默认 1.0） |
| `--auto-increment` | MySQL 专用：前置 surrogate id bigint AUTO_INCREMENT 列 |
| `--username` | Oracle：生成 public synonym 的 schema 名 |
| `--charset` | MySQL 表字符集（默认 utf8mb4） |
| `--table-space` | Oracle 表空间子句 |
| `--index-space` | Oracle 索引表空间子句 |

### 5.11 db-diff — 模型与实际数据库差异对比

```bash
# 在线模式（直连 MySQL）
apsgraph db-diff --dialect mysql --dsn "mysql://user:***@host:3306/db" --output-json output/diff.json --output-md output/diff.md

# 离线模式（从 JSON 导出读取）
apsgraph db-diff --actual-json output/actual-schema.json --output-json output/diff.json --output-md output/diff.md
```

| 参数 | 说明 |
|---|---|
| `--dsn` | 实际数据库 DSN（与 `--actual-json` 互斥） |
| `--dsn-env` | 从环境变量读取 DSN（避免命令行暴露密码） |
| `--actual-json` | 离线模式：从 JSON 文件读取实际 schema |
| `--dump-actual` | 将读取的实际 schema 导出为 JSON |
| `--tables` | 限制模型侧只比较指定表 |
| `--strict-extra-tables` | 数据库有但模型没有的表视为 ERROR（默认 WARNING） |
| `--output-json` | JSON 报告输出路径 |
| `--output-md` | Markdown 报告输出路径 |

**严重级别**：
- **ERROR**：表缺失/多余、列缺失/多余、类型族不匹配、精度/标度缩小、可空性不匹配、主键不匹配、索引缺失/多余/类型或列不匹配
- **WARNING**：列顺序、注释不匹配、精度/标度扩大、索引重名同列、大小写差异

### 5.12 bridge — 模型到 Java/CodeGraph 桥接

```bash
apsgraph bridge SysParmTable.kapb_txn_log --workspace /path/to/v8.7-all --codegraph ap-parent=/path/to/.codegraph/codegraph.db --output output/bridge.json
```

验证 `target/gen` 生成代码（包名、生成符号、`@ConfigType` 注解证据），然后只读查询 CodeGraph SQLite 索引找到 Java 消费者。

| 参数 | 说明 |
|---|---|
| `query` | 模型查询标识 |
| `--workspace` | APS 源码仓库根目录 |
| `--codegraph` | `仓库名=CodeGraph数据库路径`，可重复指定多个仓库 |
| `--output` | JSON 报告输出路径 |

### 5.13 classify — 能力分类审计

```bash
apsgraph classify --workspace /path/to/v8.7-all --output output/audit.md --json-output output/audit.json
```

基于模型 ID、描述、路径、包名、类名等证据，将 APS 模型和 Java 包按 34 种功能能力分类。

---

### 5.14 doc-export — Markdown 文档导出

```bash
# 导出全部模型文档到文件
apsgraph doc-export --db .apsgraph/apsgraph.db --output aps-models.md

# 只导出表模型
apsgraph doc-export --type table --output aps-tables.md

# 按模型查询过滤
apsgraph doc-export --type table --tables CustomerInfo AccountInfo --output selected.md
```

`--type` 支持 `all`、`table`、`dict`、`schema`、`trans`、`nsql`、`service`。

### 5.15 xlsx-export — Excel 文档导出

```bash
# 需要安装可选依赖：pip install apsgraph[excel]
apsgraph xlsx-export --db .apsgraph/apsgraph.db --output-dir docs-xlsx

# 指定类型
apsgraph xlsx-export --output-dir docs-xlsx --types table dict

# 指定项目
apsgraph xlsx-export --output-dir docs-xlsx --projects ap-parent
```

可选类型包括 `table`、`table_list`、`dict`、`dict_ref`、`enum`、`trans`、`nsql`、`service`、`service_v2`、`params`、`error_code`、`batch_tran`。

## 6. 典型工作流

### 6.1 首次构建索引

```bash
# 1. 全量扫描
apsgraph scan --workspace /path/to/v8.7-all

# 2. 验证统计
apsgraph stats
```

### 6.2 日常增量同步

```bash
# 同步源码变更
apsgraph sync --workspace /path/to/v8.7-all

# 检查状态
apsgraph status --workspace /path/to/v8.7-all
```

### 6.3 变更影响评估

```bash
# 分析某字段变更的影响范围
apsgraph impact BpDict.A.addr --depth 3
```

### 6.4 生成 DDL 脚本

```bash
# 生成全量 MySQL DDL
apsgraph ddl-gen --dialect mysql --output schema.sql

# 生成 Oracle DDL（含表空间）
apsgraph ddl-gen --dialect oracle --username APPS --table-space USERS --output schema_oracle.sql
```

### 6.5 模型与数据库差异审计

```bash
# 对比模型与实际 MySQL 库
apsgraph db-diff --dialect mysql --dsn-env MYSQL_DSN --output-md output/diff.md
```

### 6.6 真实工程验收测试（非单元测试）

每次代码变更后，除 Python 单元测试外，必须以真实 `/Users/joshua/code/v8.7-all` 执行：

```bash
python3 scripts/acceptance_v87.py \\
  --workspace /Users/joshua/code/v8.7-all \\
  --report /Users/joshua/Documents/YYYY-MM-DD/apsgraph-v87-acceptance.json
```

该测试会从真实 XML 重建临时 SQLite 索引，测试所有公开 CLI，并记录每个命令的退出码、耗时和输出；每种节点类型最多随机抽取 20 个不同 `raw_id`，少于 20 个的类型全部覆盖，并逐一调用真实 `search` CLI；同时使用 `rg` 直接检索原始 XML，对全部抽样词、引用目标和类型 ID 做交叉验证。在 `db-diff` 离线模式下，真实 v8.7-all 当前存在模型自身的未解析扩展、类型或索引问题时，命令可以按契约返回非零并生成 ERROR/WARNING 报告；验收集会校验报告结构和错误发现，不会把真实模型错误伪装成通过。

### 6.7 随机覆盖测试集

抽样规则和结果说明见 `/Users/joshua/Documents/YYYY-MM-DD/apsgraph-v87-random-acceptance.md`。

### 6.8 Metadata Graph MCP

启动 stdio MCP：

```bash
apsgraph serve-mcp \
  --db /path/to/.apsgraph/apsgraph.db \
  --workspace /path/to/workspace
```

客户端支持 MCP roots 协议时可零参数启动（工作区从客户端根目录自动解析，索引取 `<root>/.apsgraph/apsgraph.db`）：

```bash
apsgraph serve-mcp
```

路径解析顺序：显式参数 > MCP 客户端 roots > 当前工作目录。

MCP 工具：

```text
search_metadata
find_entity
find_references
get_dependencies
get_impact
find_unresolved_references
get_entity_source
```

MCP 范围参数：

```json
{
  "query": "编码",
  "kinds": ["FIELD", "ELEMENT"],
  "project": "dept-parent",
  "module": "dept-bcs",
  "path": "*/src/main/resources/tables/**",
  "top_level": false,
  "limit": 20
}
```

MCP 客户端配置、全部工具的参数与结果约定、调用示例、Agent 路由指引（业务工程 AGENTS.md 推荐片段）详见 `docs/mcp-guide.md`。

MCP 功能、性能与正确性验证（含与 `rg` 直接检索原始 XML 的交叉验证）：

```bash
python3 scripts/verify_mcp.py \
  --workspace /path/to/workspace \
  --db /path/to/workspace/.apsgraph/apsgraph.db \
  --samples 20 \
  --report /tmp/apsgraph-mcp-verify.json
```

### 6.9 查询工作台（workbench）

对已扫描的工作区启动本地 Web 查询工作台：

```bash
apsgraph workbench                          # 默认 127.0.0.1:8321 并自动打开浏览器
apsgraph workbench --db /path/to/.apsgraph/apsgraph.db
apsgraph workbench --port 9000 --no-browser
apsgraph workbench --port 0                 # 随机绑定一个空闲端口，可同时启动多个工作区
```

- 纯标准库实现，索引以只读模式打开，服务器只绑定 `127.0.0.1`，无任何写操作。
- 索引不存在时命令报错并提示先执行 `apsgraph scan`；运行日志输出到 stderr。
- `--port 0` 表示由操作系统分配一个随机空闲端口；实际端口以启动日志中的 URL 为准。
- 端口被占用时报错退出（不自动降级换端口），可用 `--port 0` 或显式指定其他端口。

多实例管理与实例注册表：

```bash
apsgraph workbench list                     # 列出正在运行的实例（JSON）
apsgraph workbench close --port 8321        # 按端口停止一个实例
apsgraph workbench close --all              # 停止当前用户的全部实例
```

- 每个实例启动时把自己的端口、PID、URL、索引路径、工作区路径与启动时间记录到用户级实例注册表 `~/.apsgraph/workbench-registry.json`（可用环境变量 `APSGRAPH_WORKBENCH_REGISTRY` 覆盖），正常退出（Ctrl+C 或 SIGTERM）时自动移除。
- `workbench list` 输出 JSON：存活实例按端口排序，同时探测 PID 存活且 `/api/stats` 可响应；过期条目（进程已退出或端口不再服务 workbench）在列出时自动清理并计入 `pruned_stale`。
- `workbench close` 只终止当前仍在响应 workbench API 的进程：pid 已失效的条目按 `already_stopped` 清理；pid 存活但端口已不再服务 workbench（pid 被复用或端口被其他程序占用）的条目按 `stale` 清理且不会误杀无关进程；指定端口不存在于注册表时返回 `not_found` 并以退出码 2 结束。
- 注册表为用户级共享，因此可在任意目录下查看和关闭其他文件夹中启动的 workbench 实例。

页面与查询能力：

1. **总览（dashboard）**：默认首页，以卡片展示各类型页面的记录数（点击卡片跳转对应页面），并展示 SQLite 索引文件大小、文件数、解析成功/失败数、节点数、边数与未解析引用数（解析失败数大于 0 时高亮告警）。
2. **顶层模型**：按顶层模型类型（SCHEMA / SQL_GROUP / SERVICE_TYPE / TRANSACTION / BATCH_TRANSACTION 等）分组列出所有顶层节点；点击节点在详情面板展示递归包含的子模型树（逐层懒加载展开）。
3. **类型查询页**：表、服务文件（SERVICE_TYPE）、服务（SERVICE_OPERATION，按 `服务文件.服务id` 维度搜索，如 `ApBatchFileService.smtbat`）、交易、批量交易（页内可切换 FILE_BATCH_TRANSACTION / BATCH_STEP / BATCH_GROUP）、文件批量（FILE_BATCH_TRANSACTION）、命名SQL文件（SQL_GROUP）、命名SQL（NAMED_SQL，粒度 `ApBatchFileSqls.upd_tb_file_tran_req`，语句详情展示按数据库类型区分的 SQL 语句文本（静态语句取 `<sql>` 子元素，动态语句按 MyBatis mapper 机制原样展示 `<dynamicSql>` 原始 XML 节点；无 type 即 NONE 并默认置顶），另展示 parameter 参数）、分片（SHARDINGSTRATEGY）、复合类型、数据字典（仅收字典文件 `.d_schema.xml` 中 DICTIONARY 节点下的 element，粒度为 `BpDict.B.btch_grp_num` 这类 `字典文件id.子模块.id`；复合类型的 element 不在此页）、数据字典文件、枚举类型、基础类型、错误码文件、错误码（kind ERROR，粒度为 `GnError.Genl.E0001` 这类 fullId；fullId 维度支持按 `.` 分段的层级筛选，省略中间分组段仍可定位）、常量（`.constant.xml` 中 constantConf>constants>constant，粒度 `CfConst.Busi.CONST_XXX`，支持按 message/description 模糊搜索）、解析失败（列出 parse_status=PARSE_FAILED 的文件路径、后缀与错误信息，支持按路径/错误信息过滤；总览页的“解析失败”统计可直接点击跳转）。
4. **枚举类型页为主从布局**：列表展示枚举的 FullId 与枚举值数量，点击后右侧展示枚举详情与全部枚举值。
5. **错误码文件页**：查询对象为 `.error.xml` 的 `errorConf` 根（kind ERRORCONF），详情按 `errors` 分组展示 error 明细（错误码 ID、类型、message），并支持按 message 模糊搜索。
6. **基础类型**：查询 `.u_schema.xml` 中定义的基础类型（RESTRICTION_TYPE，如 `ApBaseType.U_ADDR`），支持按 `id` / `fullId` / `longname` / `desc` 模糊搜索；详情展示 `base`/`maxLength` 等属性及其枚举值。
7. **搜索**：搜索栏位于中栏顶部，维度下拉、关键字输入与查询按钮直接作用于中间结果列表；支持按 `id` / `fullId` / `longname` / `desc` 单维度或全部维度模糊搜索；搜索为子串匹配（SQL LIKE），部分英文标识符（如 `smtbat`、`openAcc`）与短中文子串均可命中，`%`/`_` 按字面处理；留空关键字则分页浏览全部。结果每页 50 条，支持翻页；结果列表只展示 `fullId/id：中文名`，其余信息在详情面板查看。
8. **详情面板**：按类型结构化展示，表格列按元数据模型对象定义——交易/服务的输入输出、复合类型与数据字典的数据项表头为 `字典ID, 字段, 中文名, 类型, 必填, 多值, 默认值, 固定值, 描述, 别名`；表的字段表头为 `字典ID, 字段, DbName, 中文名, 类型, 可为空, 默认值, 描述, 是否主键`。表另提供“生成 DDL”按钮：弹窗选择 MySQL / Oracle / PostgreSQL 后生成建表语句（只生成、不执行），支持一键复制；生成结果会用 sqlglot 按对应方言做 SQL 语法校验并显示校验结论（sqlglot 为可选依赖，未安装时提示跳过）；表若通过 extension 引用公共字段表，则在字段与物理索引之间按引用顺序逐个展示“公共字段表”小节（含各公共表的字段明细，标题可点击跳转，未解析引用灰色显示）；并展示物理索引、ODB 索引、序列；交易另展示流程编排（mermaid 纵向流程图：`method` 直调步骤按序串联；`case` 为菱形判断节点，各 `when` 分支从 case 分叉、以条件名命名并纵向串联各自的服务调用，分支结束后汇合到下一步骤；mermaid 随包内置、离线可用，加载失败时自动降级为缩进列表）；错误码按分组展示明细，表头为 `id, 类型, 错误码, 参数, message`（错误码列为完整 fullId，参数为该错误码的 parameter 子节点）。数据字典/复合类型详情的数据项表格中，`字典ID` 在 ref 缺省时展示数据项自身 fullId（如 `BpDict.A.addr`）；凡值符合模型 fullId 形态（大写开头点分，如 `BaseType.U_ADDR`、`BpDict.E.entp_scale`）的属性（含字段表格中的 `类型`/`字典ID` 列与详情属性区）均渲染为可点击链接跳转到目标详情；各元数据模型详情在“引用（出）”之前展示“原始 XML 片段”（文件级节点除外：命名SQL文件、数据字典文件、错误码文件不展示）：按索引记录的来源路径按需读取源 XML 并定位到该节点（不写入 SQLite），内容为该节点的原始 XML（含全部子结构）。已解析引用渲染为可点击链接跳转，未解析引用灰色显示原始目标。详情面板顶部提供“← 返回上一级”按钮，沿跳转历史逐级返回；切换左侧页面时历史清空。详情属性过滤 `xsi:` 等命名空间属性，避免撑宽页面。
9. **布局交互**：左侧菜单一二级聚合——一级菜单固定为交易、服务、表、字典数据项、枚举类型、基础类型，其余页面（顶层模型、服务文件、批量交易、复合类型、数据字典文件、错误码文件、错误码、常量，默认收起）统一归入可展开的“其他”分组（切到其中页面时自动展开）；左侧菜单可通过 ☰ 按钮收起/展开；结果列表默认占窗口宽度的 1/4，可通过中栏与详情面板之间的分隔条拖拽调整；mermaid 流程图画布默认按 0.6 缩放展示，支持工具栏放大/缩小/重置/全屏、滚轮缩放和鼠标拖拽平移；全屏模式下画布右上角提供“✕ 关闭全屏”按钮。

---

## 7. SQLite 索引结构

索引使用 Schema V2，包含以下核心表：

| 表 | 说明 |
|---|---|
| `model_files` | 源文件记录（路径、哈希、解析状态、包名） |
| `nodes` | 模型节点（stable_id、kind、full_id、属性 JSON） |
| `edges` | 关系边（from→to、关系类型、置信度、证据） |
| `scan_state` | 扫描状态（工作空间路径、完成时间、扫描器版本） |

**节点类型**（kind）：TABLE、FIELD、SCHEMA、RESTRICTION_TYPE、ENUM_VALUE、DICTIONARY、ELEMENT、SQL_GROUP、NAMED_SQL、SERVICE_TYPE、SERVICE_IMPLEMENTATION、SERVICE_OPERATION、TRANSACTION、BATCH_TRANSACTION、BATCH_STEP、BATCH_GROUP、INDEX、SEQUENCE、SQL_PARAMETER、SQL_RESULT 等。

**关系类型**：TYPE_REF、DICT_REF、EXTENDS、IMPLEMENTS、CALLS_SERVICE、CALLS_TRANSACTION、USES_NAMED_SQL、READS_TABLE、WRITES_TABLE 等。

---

## 8. 已知限制

1. **DDL 生成是实验性**：fail-closed 模式，生成结果需人工审核，不自动执行
2. **db-diff PostgreSQL**：需要 psycopg 驱动，当前未实现在线连接，可用 `--actual-json` 离线模式
3. **bridge 动态引用**：运行时反射、动态代理等不在 CodeGraph 索引范围内的引用无法追踪
4. **classify 启发式**：基于关键词匹配，混合和未分类结果需要人工确认
5. **分片表 DDL**：当前不展开分片表（原模板生成 `表名_0..表名_N-1`），工具按逻辑表名生成
6. **自定义 DDL 片段**：`<ddls>` 标签的原生 SQL 片段不输出，遇到时以注释告警
7. **db-ratio**：原生由首选项注入，工具提供参数但默认 1.0

---

## 9. 文档保鲜与维护入口

APSGraph 长期维护五类核心文档：

| 文档 | 维护内容 |
|---|---|
| `docs/requirements.md` | 产品定位、用户需求、全部功能/非功能需求、演进需求 |
| `docs/design.md` | 关键架构、模块设计、数据模型、并发与性能设计 |
| `docs/product-whitepaper.md` | 产品定位、架构设计、功能地图、价值与路线 |
| `docs/usage-guide.md` | 安装、命令、参数、典型使用方式 |
| `docs/operations-guide.md` | 发布、巡检、日志、故障、性能、备份与回滚 |

新增或修改行为时，必须同步更新对应文档，并执行版本升级、测试、提交、wheel 构建和 CLI 验证。
