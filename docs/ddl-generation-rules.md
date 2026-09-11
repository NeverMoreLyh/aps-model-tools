# APS 元数据模型 → 数据库建表脚本：生成规则梳理

> 版本：0.18.9

> 依据 `aps-maven/aps-model-util` 源码与 FreeMarker 模板逆向整理，
> 作为统一 DDL 生成工具（`apsgraph ddl-gen`）的实现基准。
>
> 模板路径：`aps-maven/aps-model-util/src/main/resources/cn/sunline/ltts/frw/model/generator/sql/`
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

Schema 中的 `restrictionType` 沿 `base` 递归到最底层 `SimpleType` 后再映射；枚举限制类型继承基础类型，不生成数据库原生 enum 类型。源码中可作为基础类型的名称包括：`eString`、`encString`、`cString`、`fixString`、`string`、`dateString`、`dateString8`、`timeString17`、`boolean`、`int`、`integer`、`long`、`dateTime`、`dataTime`、`date`、`time`、`timestamp`、`double`、`decimal`、`amount`、`schema`、`clob`、`blob`。

### MySQL（tdsql/oceanbase 同表）
| 基础类型 | 长度条件 | MySQL 类型 | 示例 |
|---|---|---|---|
| eString/encString/cString/dateString/string/schema | n < 1000 | varchar | `varchar(500)` |
| eString/encString/cString/dateString/string/schema | n >= 1000 | text | `text` |
| fixString | n | char | `char(1)` |
| boolean | 默认 1 | char | `char(1)` |
| int | 默认 10；`isUsed=true` 时 n <= 4 | tinyint | `tinyint(4)` |
| int | `isUsed=true` 时 4 < n <= 6 | smallint | `smallint(6)` |
| int | 默认 10；未触发 tiny/small 转换 | int | `int(10)` |
| long | 默认 16 | bigint | `bigint(16)` |
| dateTime | 默认无；显式 n | dateTime | `dateTime(3)` |
| dateString8 | 默认 8 | date | `date(8)` |
| date | - | date | `date` |
| time | 默认无；显式 n | time | `time(3)` |
| double/decimal/amount | p,s；amount 默认 (20,2) | decimal | `decimal(20,2)` |
| clob | - | text | `text` |
| blob | - | blob | `blob` |
| timestamp | 默认无；显式 n | timestamp | `timestamp(3)` |

### 方言家族与其他模板

| 方言/模板 | 类型映射来源 | 支持结论 |
|---|---|---|
| `tdsql`、`oceanbase` | 复用 MySQL 映射和 MySQL 二次转换 | MySQL 家族等价支持 |
| `gaussdb` | 复用 Oracle 映射和 Oracle 二次转换 | Oracle 家族等价支持 |
| `goldendb` | 独立映射，基本等同 MySQL | 可生成，按 GoldenDB 映射表执行 |
| `db2` | 独立映射，覆盖字符串、数值、日期时间、`blob` | 可生成；未命中类型按原名透传 |
| `db2as400` | 使用 DB2 模板链 | 可生成；类型能力以 DB2 映射为准 |
| `sybase` | 模板内局部 `userTypeMap` | 有限支持；不能视为完整类型覆盖 |
| `hsql` | 模板内局部 `userTypeMap` | 有限支持；模板局部映射优先 |
| `sqlserver` | 独立映射（`nvarchar`、`datetime2`、`bit`、`varbinary(max)` 等） | 可生成，但不是 APSGraph 三主方言 |
| `trafodion` | 无同名 SQL 模板和独立映射 | 未覆盖 |
| `unknown` | 无可靠方言转换 | 仅兜底透传，不能视为兼容性支持 |

部分方言映射表未包含全部基础类型；`baseTypeToDbType` 在未命中时回退为基础类型原名，因此文档将这类结果标记为“原样透传”。

### Oracle（gaussdb 同表）
| 基础类型 | 长度条件 | Oracle 类型 | 示例 |
|---|---|---|---|
| eString/encString/cString/dateString/string/schema | n <= 4000 | varchar2 | `varchar2(500)` |
| eString/encString/cString/dateString/string/schema | n > 4000 | clob | `clob` |
| fixString | n | char | `char(1)` |
| boolean | 默认 1 | char | `char(1)` |
| int/integer/double/long/decimal/amount | p,s；int 默认 (10)，long 默认 (16)，amount 默认 (20,2) | number | `number(20,2)` |
| date/dateString8/time/dataTime | dateString8 默认 8 | date | `date(8)` |
| timeString17 | 默认无；显式 n | timestamp | `timestamp(3)` |
| blob | - | blob | `blob` |
| clob | - | clob | `clob` |
| timestamp | 默认无；显式 n | timestamp | `timestamp(3)` |

### PostgreSQL
| 基础类型 | 长度条件 | PG 类型 | 示例 |
|---|---|---|---|
| eString/encString/cString/dateString/string/schema | n < 1000 | varchar | `varchar(500)` |
| eString/encString/cString/dateString/string/schema | n >= 1000 | text | `text` |
| fixString | n | char | `char(1)` |
| boolean | - | boolean | `boolean` |
| int | 默认无；显式 n | integer | `integer(10)` |
| long | 默认无；显式 n | bigint | `bigint(19)` |
| dateTime | 默认无；显式 n | timestamp | `timestamp(3)` |
| date/dateString8 | dateString8 默认 8；显式 n | date | `date(8)` |
| time | 默认无；显式 n | time | `time(3)` |
| double/decimal/amount | p,s；默认 (20,2) | decimal | `decimal(20,2)` |
| clob | 最终为 text 时省略 | text | `text` |
| blob | 默认无；显式 n | bytea | `bytea(1024)` |
| timestamp | 默认无；显式 n | timestamp | `timestamp(3)` |

### 三大方言的逐类型长度行为汇总

| 类型类别 | 显式设置长度/精度 | 未设置长度 | 最终类型转换/示例 |
|---|---|---|---|
| 字符串：`eString`、`encString`、`cString`、`string`、`schema` | 生成 `类型(n)`；`byCharacter=true` 时先乘 `DBRATIO` | 使用方言默认值：MySQL/Oracle `string=(255)`、PG `string=(255)`、`schema=(255)` | MySQL/PG `n < 1000` 为 `varchar(n)`，`n >= 1000` 为 `text`；Oracle `n <= 4000` 为 `varchar2(n)`，`n > 4000` 为 `clob` |
| `dateString` | 生成 `varchar(n)` / `varchar2(n)` 及对应长度 | MySQL/Oracle/PG 默认 `(8)`（按映射表） | MySQL/PG 达阈值时转 `text`；Oracle 超过 4000 转 `clob` |
| `fixString` | `char(n)` | 无默认长度 | 不做二次转换，例如 `char(1)` |
| `boolean` | `char(n)`（MySQL/Oracle）；PG 会拼接为 `boolean(n)` | MySQL/Oracle 默认 `(1)`；PG 无默认 | 不做二次转换 |
| `int` / `integer` | MySQL/Oracle/PG 都会拼接显式 `(n)`；MySQL `isUsed=true` 时 n≤4/≤6 转 `tinyint`/`smallint` | MySQL 默认 `int(10)`；Oracle 默认 `number(10)`；PG 无默认 | 例如 `int(10)`、`number(10)`、`integer(10)` |
| `long` | 显式 `(n)`；可带精度形成 `(n,s)` | MySQL 默认 `bigint(16)`；Oracle 默认 `number(16)`；PG 无默认 | 例如 `bigint(19)`、`number(19)`、`bigint(19)` |
| `double` / `decimal` / `amount` | 显式 `(p,s)` | MySQL/Oracle 只有 `amount` 默认 `(20,2)`；PG 三者默认 `(20,2)` | `decimal(p,s)` 或 Oracle `number(p,s)` |
| `date` / `dateString8` / `dataTime` | 显式长度会被直接拼接 | `dateString8` 使用 `(8)`；其余通常无默认 | 例如 `date(8)`；生成器不校验数据库是否接受该长度 |
| `time` / `timeString17` | 显式长度会被直接拼接 | 通常无默认 | 例如 `time(3)`、Oracle `timestamp(3)` |
| `dateTime` / `timestamp` | 显式长度会被直接拼接，精度也可能形成 `(n,s)` | 通常无默认 | 例如 `timestamp(3)`；MySQL 的 `dateTime` 映射值按源码为 `dateTime` |
| `clob` | MySQL/PG 最终为 `text`，Oracle 为 `clob`；模板对 `text/clob` 省略长度 | 无默认 | `text` / `clob` |
| `blob` | 显式长度会被直接拼接 | 无默认 | 例如 MySQL `blob(1024)`、PG `bytea(1024)`；生成器不做合法性校验 |

> 重要：`fieldFraction` 或 `dbFractionDigits` 生效时，通用长度函数会对所有类型追加 `,s`，不只限于 decimal；例如可能生成 `timestamp(3,6)`。这属于模板/辅助方法的原样输出行为，不代表数据库语法一定合法。

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
