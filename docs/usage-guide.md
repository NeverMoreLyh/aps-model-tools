# APS Model Tools 使用说明

> 版本：0.3.0
> 更新时间：2026-08-19
> 项目地址：https://github.com/NeverMoreLyh/aps-model-tools

---

## 1. 项目简介

APS Model Tools 是一套面向 APS 元数据模型的分析工具集，提供从源码仓库扫描 XML 模型、构建紧凑 SQLite 关系索引、增量同步、影响分析、DDL 生成、模型与数据库差异对比、CodeGraph 桥接、能力分类审计等能力。

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
| 依赖 | 无第三方运行时依赖（纯标准库） |
| 可选 | sqlglot（db-diff 解析实际库 schema 时需要） |

---

## 3. 安装

```bash
# 方式一：pip 安装（推荐）
pip install -e .

# 方式二：直接运行（无需安装）
export PYTHONPATH=src
python3 -m aps_model_tools --help
```

安装后可用 `aps-model` 命令：

```bash
aps-model --help
```

---

## 4. 命令一览

| 命令 | 说明 |
|---|---|
| `scan` | 全量扫描工作空间，构建/重建 SQLite V2 索引 |
| `sync` | 增量同步源码变更到已有索引 |
| `status` | 查看工作空间与索引的同步状态 |
| `import-jars` | 从 Maven 依赖 JAR 导入框架基础模型 |
| `stats` | 查看索引统计信息 |
| `show` | 查看指定模型的完整属性和直接关系 |
| `refs` | 查询模型的引用关系（入/出/双向，可指定深度） |
| `impact` | 变更影响分析（反向追溯受影响节点） |
| `ddl` | 单表 DDL 预览（实验性） |
| `ddl-gen` | 批量生成 MySQL/Oracle/PostgreSQL DDL |
| `db-diff` | 模型与实际数据库 schema 差异对比 |
| `bridge` | 模型 → 生成 Java → CodeGraph 消费者桥接 |
| `classify` | 按功能能力审计模型与 Java 包分类 |

---

## 5. 命令详细说明

### 5.1 scan — 全量扫描构建索引

```bash
aps-model scan \
  --workspace /path/to/v8.7-all \
  --db .data/v87-models.db
```

| 参数 | 说明 |
|---|---|
| `--workspace` | APS 源码仓库根目录（必填） |
| `--db` | SQLite 索引输出路径（必填） |
| `--fail-on-parse-error` | 遇到解析错误时立即终止（默认跳过并记录） |

**扫描的文件类型**（27 种 XML 后缀）：
`.tables.xml`、`.parms.xml`、`.flowtrans.xml`、`.nsql.xml`、`.batchStep.xml`、`.batchgroup.xml`、`.serviceType.xml`、`.serviceImpl.xml`、`.sharding.xml`、`.workflow.xml` 等。

**自动排除目录**：`.git`、`target`、`.idea`、`.codegraph`、`.hermes`、`node_modules`。

### 5.2 sync — 增量同步

```bash
aps-model sync \
  --workspace /path/to/v8.7-all \
  --db .data/v87-models.db
```

对比文件归一化相对路径和 SHA-256 哈希，在一个事务内处理新增/修改/删除文件，然后重新绑定跨文件引用。

### 5.3 status — 同步状态

```bash
aps-model status \
  --workspace /path/to/v8.7-all \
  --db .data/v87-models.db
```

返回工作空间与索引的文件差异统计。

### 5.4 import-jars — 导入框架基础模型

```bash
aps-model import-jars \
  --db .data/v87-models.db \
  --jar /path/to/aps-foundation.jar \
  --jar /path/to/aps-common.jar
```

从 Maven 依赖 JAR 中提取 APS 模型 XML 导入索引，用于补充框架基础模型。可重复 `--jar` 指定多个。

### 5.5 stats — 索引统计

```bash
aps-model stats --db .data/v87-models.db
```

返回节点数、边数、文件数、未解析引用数等统计信息。

### 5.6 show — 查看模型详情

```bash
aps-model show SysDbTable.kapp_sundry_busi --db .data/v87-models.db
```

返回指定模型的完整属性（kind、full_id、properties）和一级关系。

### 5.7 refs — 引用关系查询

```bash
aps-model refs BpDict.A.addr \
  --db .data/v87-models.db \
  --direction both \
  --depth 2
```

| 参数 | 说明 |
|---|---|
| `query` | 模型查询标识（full_id 或 raw_id） |
| `--direction` | `in`（谁引用了我）/ `out`（我引用了谁）/ `both`（默认） |
| `--depth` | 遍历深度（默认 1，最小 1） |

### 5.8 impact — 变更影响分析

```bash
aps-model impact BpDict.A.addr \
  --db .data/v87-models.db \
  --depth 3
```

从目标模型反向追溯受影响节点，按关系类型汇总，输出受影响节点列表、影响路径和未解析引用。

### 5.9 ddl — 单表 DDL 预览

```bash
aps-model ddl kapp_sundry_busi \
  --dialect mysql \
  --db .data/v87-models.db
```

生成单张表的建表 DDL（实验性，fail-closed，不自动执行）。

### 5.10 ddl-gen — 批量 DDL 生成

```bash
aps-model ddl-gen \
  --db .data/v87-models.db \
  --dialect mysql \
  --output output/schema.sql \
  --report output/report.json
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
aps-model db-diff \
  --db .data/v87-models.db \
  --dialect mysql \
  --dsn "mysql://user:***@host:3306/db" \
  --output-json output/diff.json \
  --output-md output/diff.md

# 离线模式（从 JSON 导出读取）
aps-model db-diff \
  --db .data/v87-models.db \
  --actual-json output/actual-schema.json \
  --output-json output/diff.json \
  --output-md output/diff.md
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
aps-model bridge SysParmTable.kapb_txn_log \
  --db .data/v87-models.db \
  --workspace /path/to/v8.7-all \
  --codegraph ap-parent=/path/to/.codegraph/codegraph.db \
  --output output/bridge.json
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
aps-model classify \
  --db .data/v87-models.db \
  --workspace /path/to/v8.7-all \
  --output output/audit.md \
  --json-output output/audit.json
```

基于模型 ID、描述、路径、包名、类名等证据，将 APS 模型和 Java 包按 34 种功能能力分类。

---

## 6. 典型工作流

### 6.1 首次构建索引

```bash
# 1. 全量扫描
aps-model scan --workspace /path/to/v8.7-all --db .data/v87-models.db

# 2. 导入框架基础模型（可选）
aps-model import-jars --db .data/v87-models.db --jar /path/to/aps-foundation.jar

# 3. 验证统计
aps-model stats --db .data/v87-models.db
```

### 6.2 日常增量同步

```bash
# 同步源码变更
aps-model sync --workspace /path/to/v8.7-all --db .data/v87-models.db

# 检查状态
aps-model status --workspace /path/to/v8.7-all --db .data/v87-models.db
```

### 6.3 变更影响评估

```bash
# 分析某字段变更的影响范围
aps-model impact BpDict.A.addr --db .data/v87-models.db --depth 3
```

### 6.4 生成 DDL 脚本

```bash
# 生成全量 MySQL DDL
aps-model ddl-gen --db .data/v87-models.db --dialect mysql --output schema.sql

# 生成 Oracle DDL（含表空间）
aps-model ddl-gen --db .data/v87-models.db --dialect oracle \
  --username APPS --table-space USERS --output schema_oracle.sql
```

### 6.5 模型与数据库差异审计

```bash
# 对比模型与实际 MySQL 库
aps-model db-diff \
  --db .data/v87-models.db \
  --dialect mysql \
  --dsn-env MYSQL_DSN \
  --output-md output/diff.md
```

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
