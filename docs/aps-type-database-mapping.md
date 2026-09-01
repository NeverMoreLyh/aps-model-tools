# APS 基础类型到数据库列类型映射规则

> 版本：1.0.0
> 适用基线：APS 6.51.173 / `aps-maven-6.51.16-RELEASE`
> 证据来源：`SimpleType.java`、`TableDdlUtil.java`、`DdlGenerator.java`、`mysql.ftl`、`oracle.ftl`、`postgresql.ftl`，并以 `/Users/joshua/code/v8.7-all` XML/SQLite 索引交叉校准。
> 用途：为 APSGraph SQLite 校准和 dbm2 的源库 → Canonical Model → 目标数据库 DDL 转换提供共同规则。

## 1. 结论

APS 表字段的最终数据库列类型不是直接由字段上的 `type` 字符串决定，而是沿模型引用链解析到 APS `SimpleType` 基础类型后，再按目标数据库映射：

```text
Table.field.type
  → typeObj
  → RestrictionType / SubEnum / ComplexType
  → baseTypeObj
  → SimpleType
  → DdlGenerator/TableDdlUtil.baseTypeToDbType(baseType, DbType)
  → 长度/精度/特殊阈值修正
  → 数据库列类型
```

典型链路：

```text
Table.field.type = MySchema.U_CUSTOMER_NAME
  → RestrictionType(MySchema.U_CUSTOMER_NAME)
  → base = Base.U_NAME
  → RestrictionType(Base.U_NAME)
  → base = string
  → SimpleType.string
  → MySQL varchar(n) / Oracle varchar2(n) / PostgreSQL varchar(n)
```

字典引用和枚举引用主要提供业务语义与值域；DDL 类型仍须追溯到最终 `SimpleType`。不能把字典 ID、枚举 ID 或限制类型 ID 直接当成数据库类型。

## 2. 规则证据

### 2.1 基础类型定义

APS `SimpleType` 位于：

```text
aps-maven-6.51.16-RELEASE/aps-model-util/src/main/java/
cn/sunline/ltts/frw/model/dm/SimpleType.java
```

已定义基础类型：

```text
byte
string
blob
clob
fixString
dateString
dateString8
timeString17
encString
cString
eString
boolean
int
integer
long
date
time
dateTime
timestamp
decimal
amount
double
class
schema
expr
cursor
resultSet
map
object
```

### 2.2 类型链解析

框架 `TableDdlUtil.getSimpleType(ElementType)` 的实际规则：

```text
SimpleType
  → 直接返回

RestrictionType
  → baseTypeObj 是 SimpleType：返回
  → baseTypeObj 是 RestrictionType：递归解析

SubEnum
  → owner 是 RestrictionType：解析 owner
  → 否则解析 baseTypeObj 对应 RestrictionType
```

框架 `ModelUtil.getSimpleType` 还明确支持：

```text
RestrictionType → RestrictionType → SimpleType
SubEnum → owner/base RestrictionType → SimpleType
```

当前 APSGraph `ddlgen.py` 对 `RESTRICTION_TYPE` 已采用递归解析，并合并类型链上的 facet：

```text
dbLength
maxLength
fractionDigits
dbFractionDigits
byCharacter
```

### 2.3 FTL 的统一入口

三个模板均先执行：

```text
help.getBaseType(field.typeObj)
help.baseTypeToDbType(baseType, DbType)
```

然后再处理字段长度和数据库特殊类型：

```text
MySQL：getFieldLengthString → getMysqlDbType
Oracle：getFieldLengthString → getOracleDbType
PostgreSQL：getPgsqlFieldLengthString → getPostgresqlDbType
```

因此 dbm2/APSGraph 应将“基础类型解析”和“目标数据库类型渲染”建成两个独立步骤。

## 3. 字段类型链规则

### 3.1 直接基础类型

```xml
<field id="status" type="string"/>
```

解析：

```text
string → SimpleType.string
```

### 3.2 限制类型

```xml
<restrictionType
    id="U_NAME"
    base="string"
    maxLength="64"/>
```

字段：

```xml
<field id="name" type="Base.U_NAME"/>
```

解析：

```text
Base.U_NAME
  → base=string
  → SimpleType.string
  → 保留 maxLength=64
```

### 3.3 多级限制类型

```xml
<restrictionType id="U_NAME" base="string" maxLength="255"/>
<restrictionType id="U_SHORT_NAME" base="Base.U_NAME" maxLength="64"/>
```

解析：

```text
U_SHORT_NAME
  → U_NAME
  → string
```

属性合并规则：

```text
父类型先提供默认 facet
子类型同名 facet 覆盖父类型
```

### 3.4 枚举类型

枚举可能通过 `SubEnum` 或限制类型的枚举集合表达：

```xml
<restrictionType id="U_STATUS" base="string">
    <enumeration value="A" longname="有效"/>
    <enumeration value="I" longname="无效"/>
</restrictionType>
```

解析：

```text
U_STATUS
  → base=string
  → SimpleType.string
```

数据库列类型仍是字符串类型；枚举值用于：

- 注释；
- Markdown/Excel 文档；
- 校验和造数；
- 业务值域分析。

它不会自动生成数据库 `ENUM`，除非目标数据库规则明确配置。

### 3.5 字典类型

```xml
<complexType id="CustomerStatus" dict="true">
    <element id="value" type="Base.U_STATUS"/>
</complexType>
```

字典字段引用：

```xml
<field id="status" type="Base.U_STATUS" ref="DemoDict.CustomerStatus.value"/>
```

解析：

```text
type → 物理数据库列类型
ref  → 字典业务语义和值域
```

`ref` 不应替代 `type` 参与数据库列类型计算。

## 4. APS SimpleType → 数据库类型总表

下表是 `TableDdlUtil` 静态映射与 FTL 二次修正合并后的基线。

| APS SimpleType | Java 语义 | MySQL 基础列类型 | Oracle 基础列类型 | PostgreSQL 基础列类型 |
|---|---|---|---|---|
| `eString` | 数据库加密字符串 | `varchar` | `varchar2` | `varchar` |
| `encString` | 加密字符串 | `varchar` | `varchar2` | `varchar` |
| `cString` | 中文字符串 | `varchar` | `varchar2` | `varchar` |
| `fixString` | 定长字符串 | `char` | `char` | `char` |
| `dateString` | 日期字符串 | `varchar` | `varchar2` | `varchar` |
| `dateString8` | 8 位日期字符串/Date 存储 | `date` | `date` | `date` |
| `timeString17` | 时间字符串/timestamp 存储 | `datetime` | `timestamp` | `datetime`（模板基线） |
| `string` | 可变长字符串 | `varchar` | `varchar2` | `varchar` |
| `boolean` | 布尔值 | `char` | `char` | `boolean` |
| `byte` | 字节 | 未在三张 FTL 静态表中显式定义 | 未显式定义 | 未显式定义 |
| `int` | 整型 | `int` | `number` | `integer` |
| `integer` | 整型 | 兼容 `int` | `number` | `integer` |
| `long` | 长整型 | `bigint` | `number` | `bigint` |
| `dateTime` | 时间戳 | `dateTime`（原始映射大小写） | `date` | `timestamp` |
| `date` | 日期 | `date` | `date` | `date` |
| `time` | 时间 | `time` | `date` | `time` |
| `double` | 浮点/数值 | `decimal` | `number` | `decimal` |
| `decimal` | 可带小数数值 | `decimal` | `number` | `decimal` |
| `amount` | 金额 | `decimal` | `number` | `decimal` |
| `clob` | 大字符串 | `text` | `clob` | `text` |
| `blob` | 二进制大对象 | `blob` | `blob` | `bytea` |
| `timestamp` | 时间戳 | `timestamp` | `timestamp` | `timestamp` |
| `schema` | Schema 字符串 | `varchar` | `varchar2` | `varchar` |
| `expr` | 表达式字符串 | 未在三张 FTL 静态表中显式定义 | 未显式定义 | 未显式定义 |
| `cursor` | ResultSet 游标 | 不适合作为表列 | 不适合作为表列 | 不适合作为表列 |
| `resultSet` | ResultSet 游标 | 不适合作为表列 | 不适合作为表列 | 不适合作为表列 |
| `map` | Map | 不适合作为普通表列 | 不适合作为普通表列 | 不适合作为普通表列 |
| `object` | Object | 不适合作为普通表列 | 不适合作为普通表列 | 不适合作为普通表列 |
| `class` | Java Class | 不适合作为普通表列 | 不适合作为普通表列 | 不适合作为普通表列 |

> `dateTime` 的 MySQL 静态映射值在源码中为 `dateTime`，这是历史模板行为；实际数据库通常使用 `DATETIME`。dbm2 不应未经验证擅自改变该兼容规则，应在目标方言策略中显式规范化并保留来源映射。

## 5. MySQL 规则

### 5.1 基础映射

源码静态表：

```java
mysqlMapping = {
    eString=varchar,
    encString=varchar,
    dateString8=date,
    timeString17=datetime,
    cString=varchar,
    fixString=char,
    dateString=varchar,
    string=varchar,
    boolean=char,
    int=int,
    long=bigint,
    dateTime=dateTime,
    date=date,
    time=time,
    double=decimal,
    decimal=decimal,
    amount=decimal,
    schema=varchar,
    clob=text,
    blob=blob,
    timestamp=timestamp
}
```

### 5.2 默认长度

源码 `mysqlLength`：

| 基础类型 | 默认定义 |
|---|---:|
| `int` | `(10)` |
| `long` | `(16)` |
| `dateString` | `(8)` |
| `schema` | `(255)` |
| `boolean` | `(1)` |
| `amount` | `(20,2)` |
| `string` | `(255)` |

优先级：

```text
field 实际长度
  → restriction.dbLength
  → restriction.maxLength
  → mysqlLength 默认值
```

### 5.3 MySQL FTL 特殊修正

`getMysqlDbType`：

```text
varchar(n), n >= textLength
  → text
```

其中 `textLength` 是生成器运行时配置，不应在 dbm2 中写死为一个不可配置的常量。

当 `isUsed=true` 时，整型还可能发生：

```text
int(n), n <= tinyintLength
  → tinyint

int(n), n <= smallintLength
  → smallint
```

这是模板辅助逻辑，不是 `mysqlMapping` 的基础映射。

### 5.4 MySQL FTL 建表结构

```text
create table table_name (
    field_name db_type [length] [default] [identity] [not null] [comment]
)
[character set / table comment]
```

主键：

```text
字段 primarykey=true
  → 收集字段名
  → alter table ... add constraint ... primary key (...)
```

索引：

```text
field.index=unique
  → unique index

field.index=index
  → 普通 index
```

## 6. Oracle 规则

### 6.1 基础映射

源码静态表：

```java
oracleMapping = {
    eString=varchar2,
    encString=varchar2,
    dateString8=date,
    timeString17=timestamp,
    cString=varchar2,
    fixString=char,
    dateString=varchar2,
    string=varchar2,
    boolean=char,
    int=number,
    integer=number,
    double=number,
    long=number,
    dataTime=date,
    date=date,
    timestamp=timestamp,
    time=date,
    decimal=number,
    amount=number,
    schema=varchar2,
    blob=blob,
    clob=clob
}
```

源码中同时出现 `dateTime`/`dataTime` 拼写差异。dbm2 应在 Canonical Model 层规范为一个稳定的时间戳基础类型，在 Oracle renderer 层明确映射，不应依赖拼写偶然性。

### 6.2 默认长度

Oracle `oracleLength` 与 MySQL 基线一致：

| 基础类型 | 默认定义 |
|---|---:|
| `int` | `(10)` |
| `long` | `(16)` |
| `dateString` | `(8)` |
| `schema` | `(255)` |
| `boolean` | `(1)` |
| `amount` | `(20,2)` |
| `string` | `(255)` |

### 6.3 Oracle FTL 特殊修正

`getOracleDbType`：

```text
varchar2(n), n > 4000
  → clob
```

Oracle 模板生成：

```text
create [global temporary] table ...
```

主键可以内联：

```text
constraint pk_table primary key (columns)
```

或根据配置在后续处理；具体取决于：

```text
keyProcess
hasDefindPrimaryKeyAfter(table)
```

### 6.4 Oracle 空字符串注意事项

APS `string`/`dateString` 映射到 Oracle 字符串类型时，必须记录 Oracle 的空字符串语义：

```text
Oracle 中 '' 按 NULL 处理
```

dbm2 数据转换不能简单假设：

```text
MySQL '' = Oracle ''
```

应在数据校验和字段映射报告中标记该差异。

## 7. PostgreSQL 规则

### 7.1 基础映射

源码静态表：

```java
postgresqlMapping = {
    eString=varchar,
    encString=varchar,
    dateString8=date,
    timeString17=datetime,
    cString=varchar,
    fixString=char,
    dateString=varchar,
    string=varchar,
    boolean=boolean,
    int=integer,
    long=bigint,
    dateTime=timestamp,
    date=date,
    time=time,
    double=decimal,
    decimal=decimal,
    amount=decimal,
    schema=varchar,
    clob=text,
    blob=bytea,
    timestamp=timestamp
}
```

### 7.2 默认长度

源码 `postgresqlLength`：

| 基础类型 | 默认定义 |
|---|---:|
| `dateString` | `(8)` |
| `schema` | `(255)` |
| `decimal` | `(20,2)` |
| `double` | `(20,2)` |
| `amount` | `(20,2)` |
| `string` | `(255)` |

### 7.3 PostgreSQL FTL 特殊修正

`getPostgresqlDbType`：

```text
varchar(n), n >= textLength
  → text
```

模板使用：

```text
getPgsqlFieldLengthString
```

再拼接：

```text
default
identity
not null
```

主键和索引采用与 MySQL 类似的后置 DDL。

> `timeString17=datetime` 与 PostgreSQL 原生类型并不自然匹配，这是当前 APS 静态映射的历史兼容值。dbm2 应在 PostgreSQL target policy 中明确决定是否规范为 `timestamp`，并在校准报告中保留“源规则值”和“规范化值”。

## 8. 长度、精度和 facet 规则

### 8.1 长度优先级

推荐 Canonical Model 统一采用：

```text
field override
  > 当前限制类型 facet
  > 父限制类型 facet
  > 目标数据库默认 facet
```

APS 原生字段通常由 `field.getFieldLength()` 取得长度；如果限制类型启用 `byCharacter` 且配置了 `DBRATIO`，原生逻辑会做长度换算：

```text
fieldLength × DBRATIO
```

dbm2 应将该换算记录为：

```text
length_source = derived
length_ratio = ...
```

不能只保存最后的数字。

### 8.2 Decimal 精度/小数位

对：

```text
decimal
double
amount
```

优先使用：

```text
field precision/fraction
restriction.dbFractionDigits
restriction.fractionDigits
目标默认值
```

APS 金额默认：

```text
(20,2)
```

### 8.3 大字段阈值

阈值修正不是基础类型映射，而是目标 renderer 的第二阶段：

```text
基础类型 string → varchar
长度达到目标阈值 → text/clob
```

dbm2 应保存：

```text
canonical_type = STRING
base_target_type = varchar/varchar2
rendered_target_type = text/clob
conversion_reason = LENGTH_THRESHOLD
```

## 9. dbm2/APSGraph 校准契约

### 9.1 SQLite 至少需要的字段

对于字段类型校准，APSGraph SQLite 应能提供：

```text
FIELD 节点
  stable_id
  full_id
  raw_id
  file_id
  owner_node_id
  properties.type
  properties.ref
  properties.dbname
  properties.nullable
  properties.primarykey
  properties.defaultValue

RESTRICTION_TYPE 节点
  properties.base
  properties.maxLength
  properties.dbLength
  properties.fractionDigits
  properties.dbFractionDigits
  properties.byCharacter

ENUM_VALUE 节点
  properties.value
  properties.longname

DICTIONARY / COMPLEX_TYPE 节点
  properties.dict

关系
  TYPE_REF
  DICT_REF
  EXTENDS
  CONTAINS
```

### 9.2 校准输出

dbm2 不应只输出：

```text
column_type = varchar(255)
```

应输出完整解释：

```json
{
  "field": "customer_name",
  "source_type_ref": "Base.U_CUSTOMER_NAME",
  "type_chain": [
    "Base.U_CUSTOMER_NAME",
    "Base.U_NAME",
    "string"
  ],
  "canonical_type": "STRING",
  "facets": {
    "maxLength": 64,
    "nullable": false
  },
  "target": "oracle",
  "base_target_type": "varchar2",
  "rendered_column_type": "varchar2(64)",
  "rule_source": "aps-metamodel-6.51.173/TableDdlUtil/oracle.ftl",
  "confidence": "CERTAIN"
}
```

### 9.3 未解析和歧义必须失败关闭

以下情况不应静默降级：

```text
type 引用不存在
type 引用存在多个候选
restriction 循环
SubEnum 无 owner/base
基础类型不在映射表
长度/精度非法
复杂对象被当作普通数据库列
```

输出：

```text
UNRESOLVED_TYPE
AMBIGUOUS_TYPE
TYPE_CYCLE
UNSUPPORTED_BASE_TYPE
INVALID_FACET
NON_SCALAR_TYPE
```

## 10. 规则配置建议

### 10.1 不建议将核心映射全部放在 YAML

核心代码规则应保持在版本化 renderer 中：

```text
SimpleType → canonical type
canonical type → target base type
literal/default rendering
length/precision legality
large-type threshold
```

理由：

- 类型安全；
- 可测试；
- 方言逻辑可扩展；
- 防止配置拼写造成错误 DDL；
- 与 APS FTL/Java 源码保持可审计对应。

### 10.2 适合配置化的覆盖项

```yaml
mappingPolicy:
  version: aps-6.51.173
  targets:
    mysql:
      textThreshold: 1000
      defaults:
        string: 255
        amount: [20, 2]
      overrides:
        - sourceType: dateTime
          targetType: datetime
    oracle:
      varchar2ToClobThreshold: 4000
      lengthSemantics: BYTE
    postgresql:
      varcharToTextThreshold: 1000
      timeString17Target: timestamp
```

建议配置字段：

```text
目标数据库阈值
默认长度
DBRATIO
BYTE/CHAR 语义
目标方言覆盖
兼容性告警级别
```

不建议配置任意 Java 类名或任意 SQL 片段。

## 11. APSGraph 与 dbm2 的分工

```text
APSGraph
  → 识别 XML 文件
  → 建立 FIELD/type/ref/base 节点和关系
  → 解析类型链
  → 输出 canonical type、facet、证据和置信度

 dbm2
  → 读取 APSGraph SQLite
  → 根据 source/target 数据库选择 renderer
  → 生成目标 DDL
  → 执行 PreCheck
  → 输出 SQLFILE/Markdown/校准报告
```

APSGraph 不应把 Oracle/MySQL/PostgreSQL 的最终 DDL 字符串写死到模型节点中；应提供：

```text
source type chain
canonical type
facets
rule version
```

让 dbm2 的目标数据库 renderer 消费。

## 12. 验证状态

已从 APS 6.51.173 源码确认：

- `SimpleType` 基础类型全集；
- `TableDdlUtil.getSimpleType` 递归类型解析；
- `TableDdlUtil.baseTypeToDbType` 数据库映射；
- `mysqlMapping/mysqlLength`；
- `oracleMapping/oracleLength`；
- `postgresqlMapping/postgresqlLength`；
- MySQL `varchar → text` 阈值逻辑；
- PostgreSQL `varchar → text` 阈值逻辑；
- Oracle `varchar2 → clob` 阈值逻辑；
- 三个 FTL 对主键、索引、默认值、identity、nullable 和注释的消费方式；
- `/Users/joshua/code/v8.7-all` 中真实 FlowTran、Table、ServiceType、Named SQL XML 结构。

以下内容仍应以目标版本实际 XML/源码继续校准：

```text
byte/expr/map/object/cursor 作为表列的业务约束
所有自定义 facet 的版本差异
PostgreSQL timeString17 的历史兼容处理
Oracle dateTime/dataTime 拼写兼容
FTL 运行时 textLength、isUsed、DBRATIO 的实际配置值
```
