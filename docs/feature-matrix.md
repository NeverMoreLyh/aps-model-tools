# APSGraph 功能支持清单

> 版本：0.18.8
> 更新时间：2026-09-11

---

## 1. 功能矩阵

### 1.1 索引与扫描

| 功能 | 状态 | 说明 |
|---|---|---|
| 全量扫描工作空间 | ✅ | 27 种 XML 后缀，自动排除 target/.git 等 |
| SQLite V2 Schema | ✅ | model_files/nodes/edges/scan_state 四表 |
| 节点类型识别 | ✅ | 20+ 种 kind（TABLE/FIELD/SCHEMA/TRANSACTION 等） |
| 跨文件引用解析 | ✅ | TYPE_REF/DICT_REF/EXTENDS 等 10+ 种关系 |
| 增量同步 | ✅ | SHA-256 文件指纹，单事务增删改 |
| 同步状态检查 | ✅ | 文件差异统计 |
| 外部索引复用 | ✅ | `scan --external-db` 或 `.apsgraph.json externalIndexes`，workspace 定义优先并可跨索引解析引用 |
| 未解析引用警告 | ✅ | XML 扫描返回 `unresolved_models` 并输出 stderr warning，不阻断扫描 |
| 非模型引用过滤 | ✅ | 过滤 error 描述、SQL/Java primitive 与 Java 类名，拆分多值 `extension`，降低 unresolved 噪声 |
| 版本与参数查看 | ✅ | `apsgraph --version` 输出版本；`apsgraph options` 只读输出默认参数与 workspace 生效规则 |
| 解析错误记录 | ✅ | 跳过并记录，可选 fail-on-parse-error |
| 索引归属校验 | ✅ | 拒绝非本工作空间的索引 |
| 遗留 V1 升级 | ✅ | 自动检测 V1 并重建为 V2 |
| 非相关表检测 | ✅ | 拒绝非 APS 索引数据库 |

### 1.2 查询与分析

| 功能 | 状态 | 说明 |
|---|---|---|
| 索引统计 | ✅ | 节点/边/文件/未解析引用数 |
| 模型详情查看 | ✅ | 属性 + 一级关系 |
| 引用关系查询 | ✅ | 入/出/双向，可指定深度 |
| 模糊查询 | ✅ | full_id / raw_id 匹配 |
| 歧义检测 | ✅ | 多匹配时列出候选 |
| 变更影响分析 | ✅ | 反向追溯，按关系类型汇总 |
| 未解析引用报告 | ✅ | 影响分析中标记 questions |

### 1.3 DDL 生成

| 功能 | 状态 | 说明 |
|---|---|---|
| 单表 DDL 预览 | ✅ | 实验性，fail-closed |
| 批量 DDL 生成 | ✅ | 全部或指定表 |
| MySQL 方言 | ✅ | varchar/int/bigint/decimal/text/blob 等 |
| Oracle 方言 | ✅ | varchar2/number/date/clob/blob 等 |
| PostgreSQL 方言 | ✅ | varchar/integer/bigint/text/bytea 等 |
| 类型映射 | ✅ | 逆向自 apsgraph-util TableDdlUtil |
| 默认长度补齐 | ✅ | string=255, boolean=1, int=10, long=16, amount=20,2 |
| varchar→text/clob 转换 | ✅ | `--text-threshold`（默认 1000） |
| byCharacter 长度倍率 | ✅ | `--db-ratio`（默认 1.0） |
| 继承字段展开 | ✅ | extension 父表字段递归合并 |
| 枚举注释 | ✅ | 字段注释含枚举值（最多 5 个） |
| 主键生成 | ✅ | ALTER TABLE ADD CONSTRAINT |
| 索引生成 | ✅ | 普通/唯一索引 |
| 序列生成 | ✅ | MySQL 序列表 / Oracle CREATE SEQUENCE / PG CREATE SEQUENCE |
| MySQL AUTO_INCREMENT | ✅ | `--auto-increment` 前置代理主键列 |
| Oracle public synonym | ✅ | `--username` 生成同义词 |
| Oracle 表空间 | ✅ | `--table-space` / `--index-space` |
| MySQL 字符集 | ✅ | `--charset`（默认 utf8mb4） |
| 分片表展开 | ❌ | 按逻辑表名生成，不展开分片 |
| 自定义 DDL 片段 | ❌ | `<ddls>` 不输出，注释告警 |
| DDL 自动执行 | ❌ | fail-closed，仅生成不执行 |

### 1.4 数据库差异对比（db-diff）

| 功能 | 状态 | 说明 |
|---|---|---|
| MySQL 在线对比 | ✅ | 直连 information_schema |
| Oracle 在线对比 | ✅ | 直连 data dictionary views |
| PostgreSQL 在线对比 | ❌ | 需 psycopg 驱动，未实现 |
| 离线 JSON 模式 | ✅ | `--actual-json` 读取导出的 schema |
| 实际 schema 导出 | ✅ | `--dump-actual` 导出 JSON |
| DSN 环境变量 | ✅ | `--dsn-env` 避免命令行暴露密码 |
| 表级差异报告 | ✅ | ERROR / WARNING 严重级别 |
| 列缺失/多余检测 | ✅ | |
| 类型族不匹配检测 | ✅ | |
| 精度/标度变化检测 | ✅ | 缩小=ERROR，扩大=WARNING |
| 可空性不匹配检测 | ✅ | |
| 主键不匹配检测 | ✅ | |
| 索引差异检测 | ✅ | 缺失/多余/类型/列不匹配 |
| 注释差异检测 | ✅ | WARNING |
| 多余表检测 | ✅ | 默认 WARNING，可选 `--strict-extra-tables` 升级为 ERROR |
| JSON 报告输出 | ✅ | |
| Markdown 报告输出 | ✅ | |

### 1.5 CodeGraph 桥接（bridge）

| 功能 | 状态 | 说明 |
|---|---|---|
| 模型→生成 Java 映射 | ✅ | 包名 + 生成符号 + @ConfigType 证据 |
| target/gen 文件验证 | ✅ | 直接检查生成文件是否存在 |
| CodeGraph 只读查询 | ✅ | 只读模式，不修改 CodeGraph 数据库 |
| 多仓库支持 | ✅ | `--codegraph repo=db` 可重复 |
| 未覆盖仓库报告 | ✅ | 无 CodeGraph 索引的仓库标记为 gap |
| 外部类型/内部类型区分 | ✅ | |
| 消费者置信度分级 | ✅ | CERTAIN_COMPILED / DERIVED 等 |
| 动态/运行时引用 | ❌ | 不在 CodeGraph 索引范围 |
| CodeGraph 写入 | ❌ | 只读，永不写入 |

### 1.6 能力分类审计（classify）

| 功能 | 状态 | 说明 |
|---|---|---|
| 34 种功能能力分类 | ✅ | 日切/安全/Redis/外发/日终/联机/批量等 |
| 模型 ID/描述/路径证据 | ✅ | |
| Java 包名/类名证据 | ✅ | |
| 中英文关键词匹配 | ✅ | |
| 分类报告 (Markdown) | ✅ | |
| 分类报告 (JSON) | ✅ | |
| 混合/未分类标记 | ✅ | 需人工确认 |
| 自动重构 | ❌ | 启发式审计，非自动改写 |

---

## 2. 支持的 APS 模型文件类型

| 后缀 | 节点类型 | 说明 |
|---|---|---|
| `.tables.xml` | TABLE / FIELD / INDEX | 数据库表定义 |
| `.u_schema.xml` | SCHEMA | 数据库 Schema |
| `.e_schema.xml` / `.d_schema.xml` / `.c_schema.xml` | SCHEMA | 扩展 Schema |
| `.parms.xml` | TABLE / FIELD | 参数表定义 |
| `.flowtrans.xml` | TRANSACTION | 交易流程定义 |
| `.nsql.xml` | SQL_GROUP / NAMED_SQL | 命名 SQL |
| `.batchStep.xml` | BATCH_STEP | 批量步骤 |
| `.batchgroup.xml` | BATCH_GROUP | 批量组 |
| `.batch_tran.xml` | BATCH_TRANSACTION | 批量交易 |
| `.file_batch_tran.xml` | FILE_BATCH_TRANSACTION | 文件批量交易 |
| `.serviceType.xml` / `.serviceImpl.xml` | SERVICE_TYPE / SERVICE_IMPLEMENTATION | 服务定义 |
| `.apsServiceType.xml` / `.apsServiceImpl.xml` | SERVICE_TYPE / SERVICE_IMPLEMENTATION | APS 服务 |
| `.dmsServiceType.xml` / `.dmsServiceImpl.xml` | SERVICE_TYPE / SERVICE_IMPLEMENTATION | DMS 服务 |
| `.error.xml` | — | 错误定义 |
| `.constant.xml` | — | 常量定义 |
| `.plugin.xml` / `.plugin2.xml` | — | 插件定义 |
| `.componentSchema.xml` | — | 组件 Schema |
| `.report.xml` | — | 报表定义 |
| `.sharding.xml` | — | 分片定义 |
| `.webtran.xml` | — | Web 交易 |
| `.workflow.xml` | — | 工作流 |
| `.transtest.xml` | — | 交易测试 |

---

## 3. 支持的关系类型

| 关系 | 说明 |
|---|---|
| TYPE_REF | 类型引用（field.type、javaType、base 等） |
| DICT_REF | 字典引用（ref 属性） |
| EXTENDS | 继承（extension 属性） |
| IMPLEMENTS | 实现服务接口（serviceType 属性） |
| CALLS_SERVICE | 调用服务（serviceName 属性） |
| CALLS_TRANSACTION | 调用交易（transactionId / transaction 属性） |
| READS_TABLE | 读取表 |
| WRITES_TABLE | 写入表 |
| USES_NAMED_SQL | 使用命名 SQL |
| GENERATES | 生成 |
| IMPLEMENTED_BY | 被实现 |
| REGISTERED_AS | 注册为 |
| RESOLVES_TO | 解析为 |

---

## 4. 34 种能力分类

| 能力标识 | 中文名称 |
|---|---|
| day-switch | 统一日切功能 |
| security | 安全组件 |
| redis | Redis 工具类 |
| socket | Socket 工具类 |
| outbound | 外发组件 |
| eod-control | 日终禁用解禁 |
| online-hooks | 联机前后处理 |
| prompt-auth | 提示、警告、授权 |
| batch-hooks | 批量前后处理 |
| dao-hooks | DAO 前后处理 |
| service-engine-hooks | 服务引擎前后处理 |
| optimistic-lock | 乐观锁机制 |
| plugin | 可插拔功能 |
| file-transfer | 文件传输 |
| coding-rule | 编码规则 |
| sequence-parameter | 序号参数 |
| sms | 短信通知 |
| journal-sequence | 流水号机制 |
| parameter-io | 参数导入导出 |
| utilities | 工具类 |
| batch-to-online | 批量转联机 |
| data-clean | 数据清理 |
| common-file | 通用文件处理 |
| unitization-extension | 单元化扩展 |
| reversal-inquiry-rollback | 统一冲正、查证、回滚 |
| idempotency | 防重幂等 |
| business-log | 业务日志登记 |
| message-adapter | 报文适配 |
| runtime-context | 公共运行区 |
| aps-extension | APS 平台扩展 |
| parameter-maintenance | 统一参数维护 |
| transaction-state | 事务状态控制 |
| dynamic-static-list | 动态列表与静态列表 |
| sharding | Shard 分片机制 |
| file-config | 文件配置 |

---

## 5. 代码规模

| 维度 | 数量 |
|---|---|
| 源码文件 | 11 个 Python 模块 |
| 源码行数 | ~3,200 行 |
| 测试文件 | 9 个 |
| 测试行数 | ~1,140 行 |
| CLI 命令 | 13 个 |
| 支持的 XML 后缀 | 27 种 |
| 节点类型 | 20+ 种 |
| 关系类型 | 13 种 |
| 能力分类 | 34 种 |
| DDL 方言 | 3 种（MySQL/Oracle/PostgreSQL） |

---

## 6. 与 dbm2 迁移工具的集成

APSGraph 产出的 SQLite V2 索引可作为 dbm2 数据库迁移工具的可选校准源：

```yaml
# dbm2 application.yml
migration:
 calibration-sqlite-path: /path/to/v87-models.db
```

启用后 dbm2 迁移引擎参考 APS 元数据索引校准类型映射与迁移策略；未启用时使用默认规则。
