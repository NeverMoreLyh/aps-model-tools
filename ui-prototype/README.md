# APSGraph UI 原型

> 当前为可确认的信息架构原型，标记为 `PROTOTYPE`；只读、不修改业务源码、不写入 CodeGraph。

## 运行

在存在 APSGraph SQLite 索引的 workspace 根目录运行：

```bash
python3 /Users/joshua/code/aps-model-tools/ui-prototype/server.py \
  --workspace /path/to/workspace \
  --db /path/to/workspace/.apsgraph/apsgraph.db \
  --port 8787
```

浏览器打开：

```text
http://127.0.0.1:8787
```

若索引尚未创建：

```bash
cd /path/to/workspace
apsgraph scan
```

## 当前页面

- 结构化模型查询：仅查询顶层模型（`owner_node_id IS NULL`），支持 full_id、raw_id、stable_id、文件路径、模型类型过滤；
- FlowTran 工作台入口：第一版按 `FLOWTRAN` 类型筛选，详情 Tab 预留基本信息、结构节点、关系与血缘、原始 XML；
- 模型详情：属性 JSON、直接子节点、关系/证据路径/置信度；
- 原始 XML：仅读取索引关联的本地 XML，archive/external index 或 workspace 外路径显示不可用状态；
- 统计卡片：文件、已解析文件、节点、关系、未解析引用；
- `GET /api/kinds`：只统计顶层模型类型；
- `GET /api/models`：只返回顶层模型（`owner_node_id IS NULL`）；
- API：`/api/health`、`/api/stats`、`/api/kinds`、`/api/models`、`/api/models/{stable_id}`、`/api/xml/{stable_id}`。

## 已验证的 SQLite 边界

UI 只读打开 APSGraph SQLite V2，使用实际表：

```text
model_files
nodes
edges
scan_state
```

原始 XML 没有复制进 SQLite；页面根据 `model_files.path` 回到 workspace 读取，符合索引和源文件分离设计。

## 后续实现前确认项

1. FlowTran 的实际 `kind`、XML tag 与属性命名需要以目标 workspace 扫描结果校准；
2. 接口定义、数据映射和流程编排需要依据 FlowTran 节点树/关系的实际节点类型拆成专用视图；
3. 其他模型详情页面可按 `TABLE`、`SERVICE`、`SERVICE_V2`、`NAMED_SQL`、`DICTIONARY`、`ENUM`、`PARAMETER`、`ERROR_CODE`、`BATCH_TRANSACTION` 实现专用字段卡片；
4. 原始 XML 查看器可增加节点定位、源码行号、折叠和结构化节点高亮；
5. 查询 API 稳定后再决定是否引入 Vue/React，不在本原型阶段锁定前端框架。
