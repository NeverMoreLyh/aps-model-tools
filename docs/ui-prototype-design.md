# APSGraph UI 原型设计确认稿

## 目标

为 APSGraph SQLite 提供本地只读 Web 工作台：结构化查询 APS 元数据模型，同时保留回到原始 XML 的证据路径。原型阶段不编辑模型、不执行 DDL、不写 CodeGraph。

## 信息架构

```text
模型查询
├── 全局搜索 / 类型 / 文件过滤
├── 统计概览
└── 模型详情
    ├── 基本信息
    ├── 结构节点
    ├── 关系与血缘
    └── 原始 XML

FlowTran 工作台
├── 基本信息
├── 接口定义
├── 数据映射
├── 流程编排
├── 调用/被调用关系
└── 原始 XML

其他模型
├── Table：字段、类型、主键、索引
├── Dictionary/Enum：类型、枚举值、引用
├── Service：接口、实现、调用关系
├── Named SQL：参数、SQL、读写表
├── Parameter/Error Code：值域和使用方
└── Batch Transaction：拆分、执行、合并
```

## 页面原型

当前可运行原型为：

```text
ui-prototype/server.py
ui-prototype/static/index.html
```

页面采用深色 IDE 工作台布局：左侧模型导航，中间模型列表，右侧详情 Tab；FlowTran 入口只依赖索引实际 `kind` 过滤，不预设不存在的字段。

## 数据访问

```text
SQLite V2（read-only）
  → Python stdlib HTTP API
  → HTML/CSS/JavaScript UI
```

API 只使用参数化 SQL，详情返回 `properties_json` 解析后的属性、子节点和关系证据。XML 采用 workspace 内路径回读；外部索引和归档路径明确显示 XML 不可用。

## 真实数据校准原则

第一版 UI 不凭空把某一 XML 属性命名成“接口/映射/节点”。正式实现前需在目标 APS workspace 执行：

```bash
apsgraph scan --workspace /path/to/workspace --db /path/to/workspace/.apsgraph/apsgraph.db
apsgraph stats --workspace /path/to/workspace --db /path/to/workspace/.apsgraph/apsgraph.db
apsgraph show <model-id> --db /path/to/workspace/.apsgraph/apsgraph.db
```

再根据真实 `kind`、`xml_tag`、`properties`、`CONTAINS` 和引用关系配置专用渲染器。
