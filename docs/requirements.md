# APSGraph 需求文档

> 版本：0.18.4
> 更新时间：2026-08-23  
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
- 用户可通过 `--workspace`、`--db`、`--cache-dir` 覆盖默认值。

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

- `stats`：查看索引统计。
- `show`：查看模型详情。
- `refs`：查询入向、出向或双向引用。
- `impact`：反向追溯变更影响。
- 查询输出必须附带证据路径和属性。

### R15 DDL 与数据库审计

- 支持单表 `ddl` 预览和批量 `ddl-gen`。
- 支持 MySQL、Oracle、PostgreSQL 方言。
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
