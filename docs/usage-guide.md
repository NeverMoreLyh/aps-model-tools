# APSGraph 使用说明

> 版本：0.18.3
> 更新时间：2026-08-23
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
| 可选 | `sqlglot`（db-diff 解析实际库 schema 时需要）；`openpyxl`（xlsx-export 需要） |

---

## 3. 安装

```bash
# 方式一：pip 安装（推荐）
pip install -e .

# 如需 Excel 导出
pip install -e ".[excel]"

# 方式二：直接运行（无需安装）
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
| `scan --include-deps` | 编译 Maven workspace，导入依赖 JAR 模型后原子发布索引 |
| `sync` | 增量同步源码变更到已有索引 |
| `options` | 查看当前版本、默认参数与 workspace 生效规则 |
| `status` | 查看工作空间与索引的同步状态 |
| `import-maven-deps` | 解析 Maven 依赖并替换已有索引中的 JAR 导入模型 |
| `import-jars` | 手工从指定 Maven 依赖 JAR 导入框架基础模型 |
| `stats` | 查看索引统计信息 |
| `show` | 查看指定模型的完整属性和直接关系 |
| `refs` | 查询模型的引用关系（入/出/双向，可指定深度） |
| `impact` | 变更影响分析（反向追溯受影响节点） |
| `ddl` | 单表 DDL 预览（实验性） |
| `ddl-gen` | 批量生成 MySQL/Oracle/PostgreSQL DDL |
| `db-diff` | 模型与实际数据库 schema 差异对比 |
| `bridge` | 模型 → 生成 Java → CodeGraph 消费者桥接 |
| `classify` | 按功能能力审计模型与 Java 包分类 |
| `doc-export` | 导出 Markdown 模型文档 |
| `xlsx-export` | 导出 Excel 模型文档 |

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
2. 当前版本内置默认值：workspace、数据库、缓存目录、Maven goal、测试跳过、依赖 scope、Maven 可执行文件、并行数和默认排除规则；
3. 指定 workspace 的生效排除规则与 JDK 策略。

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
| `--include-deps` | 先执行 Maven workspace 构建，再导入依赖 JAR 模型 |
| `--deps-mode` | `full`（默认）完整 workspace；`framework` 只处理最高本地 parent 边界 |
| `--external-db` | 合并依赖源码仓库生成的 APS SQLite 索引，可重复；不能与 `--include-deps` 同用 |
| `--maven-goal` | Maven goal，可重复；默认 `install` |
| `--skip-tests` / `--no-skip-tests` | 默认追加 `-DskipTests`，可改为执行测试 |
| `--deps-scope` | 依赖范围：`compile` / `runtime` / `test`，默认 `runtime` |
| `--maven` | Maven 可执行文件，默认 `mvn` |
| `--cache-dir` | workspace 相对缓存目录，默认 `.apsgraph` |
| `--jobs` | build / dependencies 阶段内部的并行 Maven 任务数，默认 `4` |
| `--exclude-project` | 跳过依赖分析的 project glob，可重复；匹配 artifactId 或项目相对路径 |
| `.apsgraph.json` | workspace 级项目规则文件，可配置 `excludeProjects` 或 `maven.excludeProjects` |
| `--no-default-project-excludes` | 关闭默认 `*dist` 排除规则 |
| `--jdk` | 全局 JDK profile 覆盖，优先级高于 workspace 规则 |
| `--project-jdk` | 项目/reactor glob 的 JDK profile 覆盖，格式 `PROJECT=PROFILE`，可重复 |
| `--java-home` | profile 的显式 `JAVA_HOME`，格式 `PROFILE=PATH`，可重复 |

普通 `scan` 不要求 Maven；未解析引用不会导致失败，JSON 的 `unresolved_models` 会列出缺失模型名，CLI 同时在 stderr 输出 warning。依赖 JAR 或外部索引导入完成后，`scan.unresolved` / `scan.unresolved_models` 会按最终数据库重新计算；`scan --include-deps` 额外返回 `workspace_unresolved_before_dependency_import` 用于对比导入前状态。扫描器不会把 error 描述、SQL/Java primitive、`class` / `resultClass` Java 类名当作 APS 模型引用，并会把空格分隔的多值 `extension` 拆成多条 EXTENDS 边。四种典型场景为：`scan --include-deps` 完整构建并解析全部依赖；`scan --include-deps --deps-mode framework` 只构建/解析最高本地 parent 边界；`scan --external-db DB` 复用依赖源码索引；普通 `scan` 只处理 workspace XML。`scan --include-deps` 的流程为：发现全部 `pom.xml`（排除 `target` 等目录）→ 识别 Maven aggregator/reactor 根项目并按 workspace 内依赖关系拓扑排序 → 按依赖约束执行默认 `mvn -B -DskipTests install`（有依赖关系的 reactor 保持先后顺序，独立 reactor 可由 `--jobs` 并行）→ 全部构建成功后并行执行各运行模块的 `dependency:list` 与 `dependency:copy-dependencies` → 检查依赖 POM 的业务模块标记并过滤 JAR → 扫描 workspace XML 并导入业务依赖 JAR XML → 原子替换最终索引。任一 Maven 项目失败、同一 `groupId:artifactId` 解析出多个版本、或依赖 XML 解析失败时立即退出，不替换已有索引。

进度日志输出到 stderr，最终 JSON 仍输出到 stdout，便于管道处理。关键阶段包括项目发现、workspace 构建、每个 reactor 构建、依赖解析、版本冲突检查、workspace XML 扫描、JAR 模型导入和原子发布。

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

### 5.4 Maven 依赖与 JAR 模型

### 5.5 四种依赖使用场景

**场景 1：最完整索引**

```bash
apsgraph scan --include-deps
```

构建全部 Maven workspace，解析业务依赖 JAR，并导入其中的业务模型 XML，适合 APSGraph 与 CodeGraph 做最完整的桥接。

**场景 2：只获取依赖 JAR XML**

```bash
apsgraph scan --include-deps --deps-mode framework
```

只构建最高本地 parent 边界（例如 `prod-parent -> ap-out-parent -> ap-parent -> external parent` 中的 `ap-parent`），Maven 使用 `-N` 非递归执行，不为了生成所有模块的 `target/gen` 类而完整编译 workspace。依赖解析也只针对该 parent 边界，随后仍按 `edsp-module` / `aps-module` 业务标记过滤 JAR。

**场景 3：复用依赖项目源码索引**

先在依赖项目执行：

```bash
cd /path/to/dependency-workspace
apsgraph scan
```

再在业务 workspace 合并其 SQLite 索引：

```bash
apsgraph scan --external-db /path/to/dependency-workspace/.apsgraph/apsgraph.db
```

也可在业务 workspace 的 `.apsgraph.json` 固化路径：

```json
{
  "externalIndexes": [
    "/path/to/dependency-workspace/.apsgraph/apsgraph.db"
  ]
}
```

外部索引中的模型使用 `external-db:<数据库>!<模型文件>` 逻辑路径；workspace 同名 `full_id` 优先，本地 XML 可引用外部模型，`sync` 不会删除这些外部模型。

**场景 4：只索引 workspace XML**

```bash
apsgraph scan
```

不调用 Maven、不导入依赖 JAR。如果引用了不存在的模型，扫描不会失败；JSON 返回 `unresolved` 数量和 `unresolved_models` 名称列表，并在 stderr 输出 warning。


默认参数等价于 `--maven-goal install --skip-tests --deps-scope runtime --db .apsgraph/apsgraph.db --jobs 4`，所有参数均可显式覆盖。构建日志位于 `.apsgraph/logs/maven/`，依赖解析日志位于 `.apsgraph/logs/maven-dependencies/`，依赖清单位于 `.apsgraph/dependency-manifest.json`。可在 workspace 根目录维护 `.apsgraph.json`：

```json
{
  "maven": {
    "excludeProjects": ["legacy-parent", "packaging/*"]
  }
}
```

也支持简化格式：

```json
{
  "excludeProjects": ["legacy-parent"]
}
```

除了显式排除规则外，`packaging=pom` 与带 `<modules>` 的普通 aggregator 不执行依赖解析；workspace 内 parent 链只解析最高本地边界。例如 `prod-parent -> ap-out-parent -> ap-parent -> external parent` 时，`prod-parent`、`ap-out-parent` 是中间 parent，会被跳过，只解析 `ap-parent`，避免同一条 parent 链重复解析传递依赖。项目级规则始终生效；默认跳过 artifactId 或项目路径匹配 `*dist` 的项目依赖分析；例如 `delivery-dist`、`packaging/*dist`。依赖 JAR 导入前会先读取 artifact POM：只有 `<properties>` 中 `<edsp-module>true</edsp-module>` 或 `<aps-module>true</aps-module>` 才会被视为业务模块并继续检索 XML；属性缺失或值为 `false` 时直接跳过，不再按 XML 后缀盲目导入。该规则只影响依赖解析与 JAR 导入，不影响 Maven reactor 构建。可用 `--exclude-project` 增加规则，例如 `--exclude-project 'packaging/*'`；添加 `--no-default-project-excludes` 可关闭默认规则。清单会记录 `projects_analyzed` 与 `projects_excluded`。

#### 混合 JDK workspace

旧 APS Maven 插件可能要求 Maven 进程运行在 JDK 8；JDK 11+ 移除 `javax.xml.bind` 后会出现 `ClassNotFoundException: javax.xml.bind.JAXBException`。不要依赖 Maven 插件自动补依赖，应在 `.apsgraph.json` 中按 Maven reactor/build unit 配置 JDK：

```json
{
  "maven": {
    "excludeProjects": ["*dist"],
    "jdk": {
      "default": "8",
      "javaHomes": {
        "8": "/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home",
        "17": "auto"
      },
      "rules": [
        {"match": ["api-parent", "api-parent/*"], "jdk": "17"},
        {"match": ["ap-parent", "ap-parent/*"], "jdk": "8"}
      ]
    }
  }
}
```

`javaHomes` 支持显式路径、`auto`（macOS 调用 `/usr/libexec/java_home -v PROFILE`）以及 `JAVA_HOME_8` / `JAVA_HOME_17` 环境变量。profile 无法解析、或同一个 reactor 内不同项目解析出多个 profile 时立即失败，不会静默替换 JDK。解析优先级为 `--project-jdk` > `--jdk` > workspace rule > workspace default > 当前环境 `JAVA_HOME`。同一项目的 build、dependency:list、dependency:copy-dependencies 三个阶段使用同一个 `JAVA_HOME`。检测到 JAXB 缺类错误时，APSGraph 保留原始日志路径并附加 JDK 8 诊断提示。

已有索引只需刷新依赖模型时：

```bash
apsgraph import-maven-deps
apsgraph import-maven-deps --build
apsgraph import-maven-deps --deps-scope compile --maven-goal package --no-skip-tests
```

`import-maven-deps` 会先在 staging 数据库中删除旧的 `jar:` 逻辑文件，再导入本次解析到且 POM 标记为业务模块的 JAR，成功后原子替换原索引；`--build` 时才执行 Maven 构建。

处理规则：

1. `scan` / `sync` 只读取工作空间 XML，不需要先执行 Maven 构建；
2. `scan --include-deps` 会执行构建并可能访问 Maven 仓库/网络；
3. 多个项目可以依赖同一坐标的同一版本；同一 `groupId:artifactId` 解析出多个版本时直接失败，不做 newest/nearest 选择，不同 groupId 复用同一 artifact ID 不算冲突；
4. JAR 条目使用 `jar:JAR路径!/条目路径` 作为逻辑路径；
5. `sync` 不会把 JAR 导入内容误判为新增/删除文件；
6. 不带 `--include-deps` 的 `scan` 是全量重建，会清空之前的 JAR 导入结果。

手工导入仍然可用：

```bash
apsgraph import-jars --jar /path/to/aps-foundation.jar --jar /path/to/aps-common.jar
```

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

### 5.7 refs — 引用关系查询

```bash
apsgraph refs BpDict.A.addr --direction both --depth 2
```

| 参数 | 说明 |
|---|---|
| `query` | 模型查询标识（full_id 或 raw_id） |
| `--direction` | `in`（谁引用了我）/ `out`（我引用了谁）/ `both`（默认） |
| `--depth` | 遍历深度（默认 1，最小 1） |

### 5.8 impact — 变更影响分析

```bash
apsgraph impact BpDict.A.addr --depth 3
```

从目标模型反向追溯受影响节点，按关系类型汇总，输出受影响节点列表、影响路径和未解析引用。

### 5.9 ddl — 单表 DDL 预览

```bash
apsgraph ddl kapp_sundry_busi --dialect mysql ```

生成单张表的建表 DDL（实验性，fail-closed，不自动执行）。

### 5.10 ddl-gen — 批量 DDL 生成

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

# 2. 导入框架基础模型（可选）
apsgraph import-jars --jar /path/to/aps-foundation.jar

# 3. 验证统计
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
