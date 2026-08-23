# APSGraph 产品白皮书

> 版本：0.11.0  
> 更新时间：2026-08-23  
> 文档定位：面向产品、架构和管理视角，介绍 APSGraph 的问题域、产品定位、架构设计、功能地图、价值与演进路线。

---

## 1. 问题背景

银行核心与业务系统大量依赖 APS XML 元数据描述表、类型、字典、服务、交易、批量任务等。随着系统规模扩大，出现以下问题：

1. 模型分散在多个仓库和依赖 JAR 中，人工检索困难；
2. 模型之间引用关系不透明，变更影响评估成本高；
3. 模型、生成 Java、实际数据库之间缺少统一视图；
4. 框架依赖模型和业务模型分离，纯源码扫描容易产生缺失引用；
5. 缺少面向研发、DBA、架构师和 AI 代理的统一查询入口。

APSGraph 的目标是把这些分散元数据变成可查询、可追溯、可审计的只读关系图。

## 2. 产品定位

APSGraph 是 APS 元数据图的构建与查询基础设施。

它不是：

- 代码生成器；
- DDL 执行器；
- CodeGraph 替代品；
- 数据库迁移平台；
- 业务源码修改工具。

它是：

- APS XML / 依赖 JAR 模型索引器；
- 模型影响分析工具；
- DDL 预览和差异审计辅助工具；
- CodeGraph 与业务模型的桥接层；
- AI 代理可调用的命令行知识工具。

## 3. 产品原则

| 原则 | 说明 |
|---|---|
| 只读安全 | 不改业务源码，不执行 DDL，不写 CodeGraph |
| 证据驱动 | 每个关系可追踪到文件和属性 |
| Fail-closed | 关键失败不发布半成品索引 |
| 机器可读 | stdout JSON，适合 CI 和 AI 代理 |
| 轻量可用 | 核心功能零第三方运行时依赖 |
| 可组合 | SQLite 单文件索引可被 dbm2、CodeGraph、分析脚本复用 |

## 4. 总体架构

```text
+------------------------------------------------------------------+
| Users                                                             |
| Developers / DBA / Architects / Migration Engineers / AI Agents   |
+-------------------------------+----------------------------------+
                                |
                         apsgraph CLI
                                |
        +-----------------------+-----------------------+
        |                       |                       |
   Workspace scanner      Maven builder           Analysis tools
        |                       |                       |
   APS XML files       POM dependency graph      impact / refs / show
   Dependency JARs     JDK policy                ddl / db-diff
   External indexes    Business marker             bridge / classify
        |                       |                       |
        +-----------+-----------+                       |
                    v                                   |
             SQLite V2 Metadata Graph                    |
                    |                                   |
                    +-----------------------------------+
```

## 5. 架构分层

| 层 | 组成 | 职责 |
|---|---|---|
| 接入层 | CLI、JSON、stderr 日志 | 参数解析、默认值、人机输出分离 |
| 采集层 | scanner、Maven builder、JAR importer | 获取 workspace 与依赖模型 |
| 存储层 | SQLite V2 | 文件、节点、边、状态与证据 |
| 分析层 | refs、impact、DDL、diff | 图查询、影响分析、生成与审计 |
| 集成层 | CodeGraph bridge、dbm2 calibration | 与研发和迁移生态协作 |
| 呈现层 | JSON、Markdown、Excel | 面向机器与人消费 |

## 6. 功能地图

### 6.1 索引与同步

| 功能 | 命令 | 说明 |
|---|---|---|
| 全量扫描 | `scan` | 扫描 workspace XML 并构建索引 |
| 增量同步 | `sync` | 基于文件 hash 更新变更 |
| 状态检查 | `status` | 查看新增、修改、删除、未变更 |
| 统计 | `stats` | 输出索引规模和解析状态 |
| 参数查看 | `options` | 输出版本、默认值和 workspace 规则 |

### 6.2 Maven 依赖能力

| 功能 | 模式 |
|---|---|
| 全 workspace 构建 | `scan --include-deps` |
| 框架依赖模式 | `scan --include-deps --deps-mode framework` |
| 依赖源码索引复用 | `scan --external-db DB` |
| 已有索引刷新依赖 | `import-maven-deps` |
| 手工 JAR 导入 | `import-jars` |
| 项目排除 | `--exclude-project` / `.apsgraph.json` |
| 混合 JDK | workspace rules / `--jdk` / `--project-jdk` |

### 6.3 模型查询与影响分析

| 功能 | 命令 |
|---|---|
| 查看模型详情 | `show` |
| 查询引用 | `refs` |
| 反向影响分析 | `impact` |
| 未解析引用警告 | `scan` / `sync` 输出 |

### 6.4 DDL 与数据库审计

| 功能 | 命令 |
|---|---|
| 单表 DDL 预览 | `ddl` |
| 批量 DDL | `ddl-gen` |
| 模型与库差异 | `db-diff` |

支持 MySQL、Oracle、PostgreSQL 方言。所有 DDL 均只生成，不执行。

### 6.5 研发生态集成

| 功能 | 命令 |
|---|---|
| CodeGraph 桥接 | `bridge` |
| 功能能力分类 | `classify` |
| dbm2 校准 | SQLite 索引作为外部输入 |
| AI 代理集成 | CLI JSON / 未来 MCP Server |

### 6.6 文档导出

| 功能 | 命令 |
|---|---|
| Markdown 文档 | `doc-export` |
| Excel 文档 | `xlsx-export` |

## 7. 核心价值

### 7.1 降低变更风险

通过 `TYPE_REF`、`DICT_REF`、`EXTENDS` 等边反向追溯，快速回答“这个字段 / 类型 / 表改了会影响谁”。

### 7.2 缩短定位时间

把跨目录、跨 JAR 的模型统一到 SQLite `full_id` 检索，替代人工 grep。

### 7.3 提升依赖完整性

framework 模式只构建本地 parent 边界即可导入框架 JAR 模型，兼顾速度与完整性。

### 7.4 支持安全审计

模型、生成代码、数据库 schema、功能能力之间形成证据链，便于审计与复核。

### 7.5 支撑 AI 工程

JSON 输出和稳定 CLI 语义使 AI 代理可以在受控范围内读取模型知识，而不是自行猜测 XML 关系。

## 8. 典型工作流

### 8.1 研发影响评估

```bash
cd workspace
apsgraph scan
apsgraph impact BaseType.U_NAME --depth 3
```

### 8.2 框架依赖索引

```bash
cd workspace
apsgraph scan --include-deps --deps-mode framework
```

### 8.3 依赖源码索引复用

```bash
cd dependency-workspace
apsgraph scan

cd application-workspace
apsgraph scan --external-db /path/dependency-workspace/.apsgraph/apsgraph.db
```

### 8.4 DDL 审计

```bash
apsgraph ddl-gen --dialect mysql --output ddl/mysql.sql
apsgraph db-diff --offline schema.json --db .apsgraph/apsgraph.db
```

### 8.5 CodeGraph 桥接

```bash
apsgraph bridge DemoTables.demo_user \
  --codegraph repo=/path/repo db=/path/codegraph.db
```

## 9. 性能与规模

参考规模：

- workspace XML：3600+；
- Maven POM：148；
- 依赖清单：547；
- 索引节点：13万+；
- 索引边：28万+；
- framework scan：十几秒级；
- 查询：秒级。

实际耗时取决于磁盘、Maven 本地仓库缓存、JDK 启动和数据库规模。

## 10. 安全边界

| 对象 | 行为 |
|---|---|
| 业务源码 | 只读 |
| `.apsgraph` | 允许写索引、日志、缓存 |
| SQLite 索引 | staging 原子发布 |
| 数据库 | db-diff 只读 |
| DDL | 只生成 |
| CodeGraph | 只读 |

## 11. 演进路线

| 阶段 | 能力 |
|---|---|
| 当前 | CLI、四种依赖场景、SQLite 图、查询分析、DDL、桥接、导出 |
| 近期 | MCP Server、CI 基线告警、索引版本对比 |
| 中期 | Web 可视化、分片 DDL、更多数据库驱动 |
| 远期 | 模型生命周期管理和跨仓库全局元数据平台 |
