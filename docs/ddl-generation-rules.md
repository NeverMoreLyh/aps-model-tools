# APS 元数据模型 → 数据库建表脚本：生成规则梳理

> 依据 `aps-maven-6.51.16-RELEASE/apsgraph-util` 源码与 FreeMarker 模板逆向整理，
> 作为统一 DDL 生成工具（`apsgraph ddl-gen`）的实现基准。
>
> 模板路径：`apsgraph-util/src/main/resources/cn/sunline/ltts/frw/model/generator/sql/`
> Java 入口：`cn.sunline.ltts.frw.model.generator.sql.DdlGenerator` / `TableDdlUtil`

## 1. 生成管线总览

```
Schema(types) → List<Table> → FreeMarker 模板(<dbtype>.ftl + sql_macro.ftl)
                                   ↓ help = DdlGenerator 静态方法
                              CREATE TABLE / ALTER PK / CREATE INDEX / SEQUENCE / COMMENT
```

- 模板按 `DbType` 枚举选择：`mysql.ftl`、`oracle.ftl`、`postgresql.ftl`（同目录还有 db2/tdsql/goldendb/gaussdb/sqlserver/oceanbase 等）。
- 模板根上下文变量：`tables`、`help`(DdlGenerator 实例)、`DbType`、`username`、`tableSpace`、`indexSpace`、
  `auto_increment`、`withseq`、`hasDesc`、`hasFieldList`、`keyProcess`、`isDeleteComment`、
  `hasLocalIndex`、`isReverseIndex`、`hasGenDdlSql`、`hasGenIndexDdlSql`、`hasPKinAlter`。

## 2. 通用规则（三种数据库一致）

| 规则 | 说明 | 源码位置 |
|---|---|---|
| 抽象表跳过 | `table.isAbstract()` 为 true 不生成 | 各 .ftl `generateTbale` 宏首行 |
| 分片表展开 | 有 sharding 时生成 `表名_0..表名_(N-1)` 共 N 张 | 各 .ftl 顶层 list |
| 物理列名 | `logicalId = dbname 非空 ? dbname : id` | Field.getLogicalId() |
| 字段来源 | `getTrueFieldWithoutCache()`：父表(extension)字段 + 本表字段，同名覆盖 | Table.getTrueFieldWithoutCache() |
| 类型解析链 | field.typeObj → RestrictionType → … → SimpleType，取最底层 baseType | TableDdlUtil.getSimpleType() |
| 列长度 | `dbLength → maxLength → 父类型递归`；小数位 `dbFractionDigits → fractionDigits → 递归`；`byCharacter=true` 且配置了 DBRATIO 时长度×系数 | ModelUtil.getLength/getFractionDigits、TableDdlUtil.getFieldLengthString |
| 无长度时默认 | 按默认长度表补（见 §3） | TableDdlUtil.getDefaultLength |
| NOT NULL | `field.nullable==false` → `not null` | 各 .ftl |
| 默认值 | `field.defaultValue`：`#` 开头取枚举值；数字类型原样；通用函数(CURRENT_TIMESTAMP 等)原样；其余加单引号 | DdlGenerator.getDefault() |
| 字段级索引属性 | `field.index=unique/index` 收集（MySQL/PG 模板收集但未实际落索引，Oracle 走 uniques） | 各 .ftl |
| 物理索引来源 | `<indexes>` 容器（区别于 `<odbindexes>`，后者只驱动 DAO 操作不生成物理索引） | Table.getIndex() |
| 主键兜底 | 无 field 级 primarykey 且无额外主键定义时，取第一个 primarykey/unique 索引字段为主键 | DdlGenerator.getKeys() |
| partition | 有 partition 属性时追加入主键列 | DdlGenerator.getKeys() |
| 序列 | `withseq` 时按 `<dbSequence>` 生成（见 §5） | sql_macro.ftl generateSeq |
| 自定义 DDL | `<ddls>` 片段按 dbType 匹配原样输出（本次工具不支持，仅保留接口） | sql_macro.ftl genddlfrag |

## 3. 类型映射（TableDdlUtil 静态映射表，原样摘录）

### MySQL（tdsql/oceanbase 同表）
| 基础类型 | MySQL 类型 | 默认长度(mysqlLength) |
|---|---|---|
| eString/encString/cString/dateString/string/schema | varchar | string=(255) schema=(255) |
| fixString | char | — |
| boolean | char | boolean=(1) |
| int | int | int=(10) |
| long | bigint | long=(16) |
| dateTime | dateTime | — |
| date / dateString8 | date | dateString=(8) |
| time | time | — |
| double/decimal/amount | decimal | amount=(20,2) |
| clob | text | — |
| blob | blob | — |
| timestamp | timestamp | — |

### Oracle（gaussdb 同表）
| 基础类型 | Oracle 类型 | 默认长度(oracleLength) |
|---|---|---|
| eString/encString/cString/dateString/string/schema | varchar2 | string=(255) schema=(255) |
| fixString | char | — |
| boolean | char | boolean=(1) |
| int/integer/double/long/decimal/amount | number | int=(10) long=(16) amount=(20,2) |
| date / dateString8 / time / dataTime | date | dateString=(8) |
| timeString17 | timestamp | — |
| blob/clob | blob/clob | — |
| timestamp | timestamp | — |

### PostgreSQL
| 基础类型 | PG 类型 | 默认长度(postgresqlLength) |
|---|---|---|
| eString/encString/cString/dateString/string/schema | varchar | string=(255) schema=(255) |
| fixString | char | — |
| boolean | boolean | — |
| int | integer | — |
| long | bigint | — |
| dateTime | timestamp | — |
| date / dateString8 | date | dateString=(8) |
| time | time | — |
| double/decimal/amount | decimal | decimal=(20,2) double=(20,2) amount=(20,2) |
| clob | text | — |
| blob | bytea | — |
| timestamp | timestamp | — |

### 二次特殊转换（模板内调用）
| 数据库 | 方法 | 规则 |
|---|---|---|
| MySQL | getMysqlDbType | `varchar(n)` 且 n≥1000 → `text`（textLength=1000）；`int` 仅在 isUsed=true 时按 ≤4→tinyint、≤6→smallint（默认关闭） |
| Oracle | getOracleDbType | `varchar2(n)` 且 n>4000 → `clob` |
| PostgreSQL | getPostgresqlDbType | `varchar(n)` 且 n≥1000 → `text` |

### 修饰符（addRemain）
RestrictionType 上 `isUnsigned=true` → 追加 ` unsigned`；`isZerofill=true` → 追加 ` zerofill`（MySQL/PG 模板使用）。

## 4. 各数据库语句结构差异

### MySQL（mysql.ftl）
```sql
create table 表名 (
    [id bigint(20) PRIMARY KEY AUTO_INCREMENT NOT NULL COMMENT "无业务含义主键",]  -- auto_increment=true 时
    列名 类型(长度) [unsigned] [default x] [identity] [not null]
        comment '长名(枚举值1-名称1,...最多5个...)',
    ...
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC collate=utf8mb4_unicode_ci COMMENT='表长名';
alter table 表名 add constraint pk_表名 primary key (keys);
create [unique] index idx_id on 表名 (f1, f2);
```
- 主键用 `ALTER TABLE ADD CONSTRAINT pk_<表名>`，**不在 CREATE 内**。
- 已用作主键的第一个 primarykey/unique 索引不再重复生成普通索引（getTableIndex 过滤）。
- text 列不带长度；出现 text 置 tableSuffix 标记（当前模板未使用）。

### Oracle（oracle.ftl）
```sql
create [global temporary ]table 表名 (     -- virtual=true 时临时表
    列名 varchar2(n) [default x] [not null],
    ...
    [constraint pk_表名 primary key (keys)]   -- keyProcess=true 时内联
    [unique(列)]                              -- 字段级 unique 索引内联
)[tableSpace];
alter table 表名 add constraint idx_id primary key (父表公共键在前, 本索引字段) using index;  -- primarykey 索引
create [unique] index idx_id on 表名 (字段)[indexSpace][ reverse][local];                    -- 普通/唯一索引
comment on table 表名 is '表长名';
comment on column 表名.列 is '长名(枚举...)';
create or replace public synonym 表名 for username.表名;   -- username 非空时
```
- 主键：优先内联（keyProcess 模式），否则走 `generateIndex` 宏的 `ALTER ADD CONSTRAINT ... USING INDEX`。
- Oracle 索引字段顺序用 `getIndexStrForOracle`：**父表公共字段键在前**。
- 注释独立 COMMENT ON 语句；枚举列表最多展示 5 个值。
- 虚拟表追加 `on commit delete rows`。

### PostgreSQL（postgresql.ftl）
```sql
create table 表名 (
    [自增id列]                                  -- auto_increment=true 时（模板沿用 MySQL 语法，属历史遗留）
    列名 类型(长度) [unsigned] [default x] [identity] [not null],
    ...
);
alter table 表名 add constraint pk_表名 primary key (keys);
create [unique] index idx_id on 表名 (f1, f2);
comment on table 表名 is '表长名';
comment on column 表名.列 is '长名(枚举...)';
```
- 结构与 MySQL 基本一致：主键 ALTER 追加、注释 COMMENT ON、无表尾选项子句。
- 长度计算用 `getPgsqlFieldLengthString`（仅 varchar/decimal 类追加长度，逻辑上等价于只对这两类生效）。

## 5. 序列生成（withseq，sql_macro.ftl generateSeq）

| 数据库 | 产物 |
|---|---|
| MySQL | `delete/insert ksys_liusdy` 序列登记表（APS 自有约定） |
| Oracle | `CREATE SEQUENCE id START WITH x INCREMENT BY y MINVALUE x [MAXVALUE z] CACHE n CYCLE/NOCYCLE ORDER` |
| PostgreSQL | `CREATE SEQUENCE id START x INCREMENT y MINVALUE x [MAXVALUE z] CACHE n CYCLE/NO CYCLE` |

## 6. 与 apsgraph SQLite 索引的对应关系

| 模板概念 | SQLite 索引对应 |
|---|---|
| Table | nodes.kind='TABLE'，properties: name/longname/extension/abstract/virtual/tableType/sharding 相关 |
| fields | TABLE → FIELDS(xml_tag='fields') → FIELD |
| 物理索引 | TABLE → INDEXES(xml_tag='indexes') → INDEX（properties: type=index/unique/primarykey, fields 空格或逗号分隔） |
| odbindexes | TABLE → ODBINDEXES（**不生成物理索引**，跳过） |
| dbSequence | TABLE → SEQUENCE(properties: startWith/incrementBy/cache/cycle/maxValue/minValue) |
| 类型链 | FIELD.type → RESTRICTION_TYPE(base/maxLength/dbLength/fractionDigits/dbFractionDigits/byCharacter/isUnsigned/isZerofill) → … → 原始类型 |
| 枚举注释 | RESTRICTION_TYPE → ENUM_VALUE(value/longname) |
| 继承字段 | TABLE.extension 指向父表/复合类型，递归展开后同名字段覆盖 |

## 7. 已知差异与取舍（工具实现说明）

1. **identity 关键字**：原模板对 MySQL/PG 直接输出 `identity`（MySQL 实际不支持该关键字，靠 auto_increment 选项另加 id 列）。工具对 MySQL 输出 `AUTO_INCREMENT`，对 PG 输出 `GENERATED BY DEFAULT AS IDENTITY`，对 Oracle 12c+ 输出 `GENERATED BY DEFAULT AS IDENTITY`，并在报告中标注与原模板的差异。
2. **ddlfrags 自定义片段**：工具暂不输出，遇到时以注释告警。
3. **DBRATIO 系数**：原生由首选项注入，工具提供 `--db-ratio` 参数，默认 1。
4. **textLength 阈值**：沿用 1000，可用参数覆盖。
5. **索引名冲突**：Oracle 索引是 schema 级命名空间，跨表同名索引原模板会冲突，工具在报告中标注。
