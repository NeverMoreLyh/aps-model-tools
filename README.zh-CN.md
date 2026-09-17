# APSGraph

[English](README.md) | 简体中文

只读的 APS 元数据分析工具集：将 APS XML 模型扫描为紧凑的 SQLite 关系索引，在此基础上进行查询、影响分析、DDL 生成、数据库 schema 对比、文档导出、生成 Java/CodeGraph 桥接，并通过本地 Web 工作台（workbench）进行可视化浏览。

- **只读设计** —— 不修改业务源码仓库、不写入 CodeGraph 数据库、不执行生成的 DDL。
- **零强制运行时依赖** —— 核心 CLI 仅用 Python 3.9+ 标准库即可运行。
- **机器友好** —— 所有命令在 stdout 输出 JSON，人类可读日志走 stderr。

## 功能总览

| 领域 | 能力 |
|---|---|
| 索引 | 全量扫描（`scan`）与增量同步（`sync`）APS XML 模型（表、字典、枚举、服务、交易、批量、命名SQL、分片、错误码、常量、基础类型），写入 SQLite Schema V2（语义节点、关系边、FTS5 搜索索引；不存原始 XML，XML 片段按需从源文件读取） |
| 查询与分析 | 精确查找、模糊搜索（id/fullId/中文名/描述）、引用关系图（`refs`）、反向影响分析（`impact`）、工作区状态与索引统计 |
| DDL | 单表 DDL 预览（`ddl`）、MySQL/Oracle/PostgreSQL 全量 DDL 生成（`ddl-gen`）、模型与实际数据库 schema 差异对比（`db-diff`，支持 DSN 或离线 JSON） |
| 文档导出 | Markdown 导出（`doc-export`）与按项目聚合的 Excel 导出（`xlsx-export`，可选 `openpyxl`） |
| 桥接与审计 | APS 模型到生成 Java 符号与 CodeGraph 消费者的映射（`bridge`）；模型与 Java 包的功能能力启发式分类审计（`classify`） |
| MCP | 以 stdio MCP 服务向 AI 客户端暴露元数据图工具（`serve-mcp`） |
| 工作台 | 基于索引的本地只读 Web UI（`workbench`）：总览、分类型查询页、结构化详情、流程图、DDL 预览；支持随机端口多实例与 `list`/`close` 实例管理 |

## 架构

```text
APS 工作区（XML 模型）                只读
        │
        ▼
┌───────────────────────┐  scan / sync  ┌─────────────────────────────┐
│  scanner 扫描器        │ ────────────▶ │  SQLite 索引（Schema V2）    │
│  发现 → 解析 →         │               │  model_files · nodes ·      │
│  引用解析              │               │  edges · model_search(FTS5) │
└───────────────────────┘               │  scan_state                 │
                                        └──────────────┬──────────────┘
        ┌───────────────────────────────────────────────┼──────────────┐
        ▼                ▼               ▼              ▼              ▼
   CLI 命令          MCP（stdio）    Web 工作台       DDL/db-diff     文档导出
   search/find/refs  search、find、  仅 127.0.0.1，   各方言 DDL      markdown /
   impact/bridge…    impact、源码    只读、GET-only   + SQL 校验      xlsx
```

- **scanner**（`scanner.py`）—— 发现可识别的 APS XML 文件，解析为语义节点与关系，解析跨文件引用，支持合并外部 APS 索引（`--external-db`）。写入仅限专用索引路径。
- **store**（`store.py`）—— 只读连接辅助与 Schema V2 数据模型：文件、节点（含 stable id）、边、FTS5 搜索与扫描状态。
- **查询层**（`search_scope.py`、`impact.py`）—— 带作用域的精确/模糊搜索与递归反向影响分析。
- **生成器/分析器**（`ddlgen.py`、`dbdiff.py`、`bridge.py`、`classification.py`）—— 各方言 DDL 规则、ERROR/WARNING 结论的 schema 对比、带显式证据的 Java/CodeGraph 桥接、功能能力分类。
- **交互层**（`cli.py`、`mcp_server.py`、`workbench.py` + `workbench_static/`）—— CLI、stdio MCP 服务、自包含的本地 Web UI（原生 JS，内置 mermaid 离线可用）。
- **工作台实例注册表** —— 每个 workbench 实例把 `{port, pid, url, db, workspace, started_at}` 记录到 `~/.apsgraph/workbench-registry.json`，从而可以在任意目录下列出并关闭其他文件夹启动的实例（可用 `APSGRAPH_WORKBENCH_REGISTRY` 覆盖）。

## 安装

```bash
# 核心 CLI（仅标准库）
pip install .

# 包含 Excel 导出
pip install ".[excel]"

# 开发安装（含 Excel 导出）
pip install -e ".[excel]"
```

可选依赖：`sqlglot`（`db-diff` 与 workbench DDL 预览的 SQL 校验）、`openpyxl`（Excel 导出）。

## 快速开始

```bash
cd /path/to/aps-workspace      # APS 模型工作区根目录
apsgraph scan                  # 在 .apsgraph/apsgraph.db 构建索引
apsgraph sync                  # 增量应用源码变更
apsgraph status                # 工作区/索引差异报告
apsgraph stats                 # 索引统计（JSON）
apsgraph options               # 版本、默认值、生效的工作区规则
```

## 命令一览

| 命令 | 用途 |
|---|---|
| `options` | 查看版本、默认参数与生效工作区规则 |
| `scan` | 从工作区 XML 全量（重）建 SQLite 索引 |
| `sync` | 按相对路径 + SHA-256 增量同步并重建引用 |
| `status` | 报告相对索引的新增/修改/删除模型 |
| `stats` | JSON 格式索引统计 |
| `show` / `find` / `search` | 节点详情；精确 id 查找；FTS5 模糊搜索 |
| `refs` | 入边/出边引用关系图（可指定深度） |
| `impact` | 反向依赖影响分析报告 |
| `ddl` | 单表 DDL 预览（实验性子集，绝不执行） |
| `ddl-gen` | 为全部或指定表生成 MySQL/Oracle/PostgreSQL DDL |
| `db-diff` | 模型 schema 与实际数据库（DSN）或 JSON 导出对比 |
| `bridge` | 模型到生成 Java 与 CodeGraph 消费者的映射 |
| `classify` | 模型与 Java 包的功能能力启发式审计 |
| `doc-export` | 导出 Markdown 模型文档 |
| `xlsx-export` | 按项目导出 Excel 模型文档 |
| `serve-mcp` | 以 stdio MCP 服务暴露元数据图 |
| `workbench` | 启动本地只读 Web 工作台并打开浏览器 |
| `workbench list` | 列出运行中的 workbench 实例（端口、PID、工作区、索引） |
| `workbench close` | 按 `--port N` 或 `--all` 停止实例 |

## 工作台（workbench）

```bash
apsgraph workbench                       # 127.0.0.1 上的随机空闲端口，并自动打开浏览器
apsgraph workbench --port 8321           # 需要稳定 URL 时显式指定固定端口
apsgraph workbench --db /other/.apsgraph/apsgraph.db --no-browser
```

服务器只绑定 `127.0.0.1` 并以只读模式打开索引，因此可以安全地为每个工作区各保留一个 workbench。每个实例默认由操作系统分配随机空闲端口，实际 URL 会输出到 stderr 并记录进实例注册表；显式指定的端口不可绑定时直接报错，不会被静默共用。

```bash
apsgraph workbench list                  # 列出运行中实例，过期条目自动清理
apsgraph workbench close --port 8321     # 停止一个实例
apsgraph workbench close --all           # 停止当前用户的全部实例
```

界面提供：总览页（各页面记录数卡片）、分类型查询页（表、服务、交易、批量、命名SQL、数据字典、枚举、基础类型、错误码、常量、分片、解析失败）、结构化详情（输入输出字段、公共字段表、流程编排图（离线 mermaid）、未解析引用高亮、原始 XML 片段（按需从源文件读取、不写入索引）），以及按表的 MySQL/Oracle/PostgreSQL DDL 预览（可选 sqlglot 校验）。

## 安全边界

- 业务源码仓库只读；索引仅写入工作区 `.apsgraph/` 目录（或显式 `--db` 指定路径）。
- 工作台仅绑定 `127.0.0.1`、只接受 GET、以只读模式打开 SQLite；非 GET 请求一律 `405`。
- 生成的 DDL 只打印或写文件 —— 绝不执行。
- CodeGraph 数据库以不可变只读方式打开；Maven 构建与 JDK 选择不在工具范围内。
- 未解析模型引用保持为警告；解析错误（`--fail-on-parse-error`）、legacy schema、外来工作区时索引替换一律 fail-closed。

## 文档地图

| 文档 | 用途 |
|---|---|
| [需求文档](docs/requirements.md) | 长期需求基线 |
| [设计文档](docs/design.md) | 架构、关键设计与性能 |
| [产品白皮书](docs/product-whitepaper.md) | 产品定位、架构与功能地图 |
| [使用说明](docs/usage-guide.md) | 安装与命令用法 |
| [运维说明](docs/operations-guide.md) | 发布、巡检、故障处理、性能与回滚 |
| [功能清单](docs/feature-matrix.md) | 详细能力状态 |
| [DDL 规则](docs/ddl-generation-rules.md) | 各方言 DDL 生成规则 |
| [APS 元模型规则](docs/aps-metamodel-rules.md) | 核心概念、UML、顶层/普通模型、XML 规则与 Demo |
| [APS 类型/数据库映射](docs/aps-type-database-mapping.md) | 基础类型递归解析与 MySQL/Oracle/PostgreSQL 列类型规则 |
| [MCP 指南](docs/mcp-guide.md) | MCP 服务与 AI 客户端配合使用 |
