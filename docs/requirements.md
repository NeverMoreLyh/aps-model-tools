# APSGraph 需求文档

> 版本：0.11.0  
> 更新时间：2026-08-23  
> 文档定位：本文件是 APSGraph 的长期需求基线，汇总产品定位、用户需求、功能需求、非功能需求与演进需求。新增需求必须先更新本文，再进入设计与实现。

---

## 1. 产品定位

APSGraph 是面向 APS 元数据模型的只读分析工具，把散落在源码和依赖 JAR 中的 XML 模型解析为 SQLite 关系图，为影响分析、DDL 生成、数据库差异审计、CodeGraph 桥接、能力分类和技术文档导出提供统一索引。

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

### R4 Maven workspace 构建

`scan --include-deps` 需要完成：

1. 发现 workspace 下全部 Maven `pom.xml`；
2. 识别 reactor / aggregator；
3. 根据 workspace 内依赖关系计算构建顺序；
4. 按顺序执行 Maven 构建；
5. 任一构建失败立即退出；
6. 全部构建成功后解析 Maven 依赖；
7. 导入业务依赖 JAR 中的模型 XML；
8. 原子发布最终索引。

默认行为：

| 项 | 默认值 |
|---|---|
| Maven goal | `install` |
| 测试 | `-DskipTests` |
| dependency scope | `runtime` |
| jobs | `4` |
| workspace | 当前目录 |
| DB | `.apsgraph/apsgraph.db` |

build 与 dependencies 两个阶段必须串行；各阶段内部可以对无依赖任务并行。

### R5 Maven 版本与冲突规则

- 同一 `groupId:artifactId` 解析出多个版本时必须 fail-closed。
- 不同 `groupId` 可以复用相同 `artifactId`，不得误报。
- 多个项目依赖同一个 JAR 时必须去重，只导入一次。
- workspace 内依赖优先识别为本地依赖，避免无意义的重复解析。

### R6 项目排除规则

- 默认排除 artifactId 或项目路径匹配 `*dist` 的项目，仅影响依赖分析，不影响 Maven 构建。
- 支持命令行 `--exclude-project PATTERN` 可重复传入。
- 支持 `.apsgraph.json`：

```json
{
  "maven": {
    "excludeProjects": ["*dist", "packaging/*"]
  }
}
```

- 支持 `--no-default-project-excludes` 关闭内置默认排除规则。

### R7 Parent 边界规则

对本地 parent 链只分析最高边界，例如：

```text
prod-parent -> ap-out-parent -> ap-parent -> external parent
```

其中 `ap-out-parent`、`prod-parent` 是传递性 parent，不单独解析依赖，只分析 `ap-parent`。

### R8 依赖业务标记

依赖 JAR 的 POM 必须满足以下任一条件才自动导入 XML：

```xml
<properties>
  <edsp-module>true</edsp-module>
</properties>
```

或：

```xml
<properties>
  <aps-module>true</aps-module>
</properties>
```

- 标记缺失、为 `false` 或非法时跳过导入。
- 先判断 POM 标记，再读取 JAR XML，降低解析量。
- 手工 `import-jars` 不受该自动发现规则限制。

### R9 四种依赖使用场景

| 场景 | 命令 | 需求 |
|---|---|---|
| 最全场景 | `scan --include-deps` | 全 workspace 编译、全量依赖解析、依赖 JAR XML 导入 |
| 框架依赖场景 | `scan --include-deps --deps-mode framework` | 只构建最高本地 parent 边界，导入框架 JAR 模型 |
| 依赖源码索引复用 | `scan --external-db DB` | 合并依赖项目已生成的 APS SQLite 索引 |
| 纯 workspace 场景 | `scan` | 只解析 workspace XML，unresolved 仅警告 |

`--include-deps` 与 `--external-db` 互斥。

### R10 混合 JDK workspace

- 支持同一 workspace 中 JDK 8 与 JDK 17 项目并存。
- 支持 workspace 级默认 JDK、规则、`--jdk`、`--project-jdk`、`--java-home`。
- 同一 reactor / build unit 必须使用同一个 JDK profile，冲突时失败。
- 同一项目的构建、依赖列表、依赖复制必须使用相同 JDK。
- 遇到 `javax.xml.bind.JAXBException` 缺类错误时，保留原始日志并提示 JDK 8 兼容性。

### R11 阶段日志

Maven 集成必须输出关键阶段日志到 stderr，stdout 保持 JSON 机器可读：

```text
discover Maven projects
build Maven workspace
resolve Maven dependencies
check dependency versions
scan workspace XML
import dependency JAR models
publish index atomically
```

并输出项目级进度与关键统计。

### R12 安全发布

- SQLite 索引必须通过 staging 数据库构建。
- Maven 构建、版本冲突检查、依赖解析、JAR XML解析任一失败时，不替换既有索引。
- 发布使用原子 `rename`。
- 依赖刷新时旧 JAR 模型必须被替换。
- external index 与 workspace 同名模型冲突时，workspace 定义优先。

### R13 引用准确性

- 引用值统一去除首尾空白。
- 不把以下内容当成 APS 模型引用：
  - error XML 描述属性；
  - SQL primitive；
  - Java primitive / Java 类名；
  - `parameterMap.class`、`resultMap.resultClass`；
  - index 语义值 `index`、`unique`、`primarykey`。
- 表 `extension` 支持空格分隔多值，每个父模型生成独立 `EXTENDS` 边。
- `scan --include-deps` 返回最终索引的 `unresolved` / `unresolved_models`。
- 保留 `workspace_unresolved_before_dependency_import` 便于对比依赖导入效果。

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
7. 验证 `apsgraph --version` 与 `options`。

## 4. 非功能需求

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
