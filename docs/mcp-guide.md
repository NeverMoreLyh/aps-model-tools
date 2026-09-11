# APSGraph Metadata Graph MCP 使用说明

> 版本：0.18.8
> 更新时间：2026-09-11

---

## 1. 概述

`apsgraph serve-mcp` 把 APSGraph 的元数据索引（SQLite + FTS5 + 关系图）以 stdio MCP（Model Context Protocol）服务的形式暴露出去，供 ZCode、Claude Desktop、Cursor 等 MCP 客户端或任何 JSON-RPC 客户端调用。AI 助手借此可以直接检索 APS 元数据（表、字段、枚举、字典、服务、批处理等）并做影响分析，而不需要在业务工程里反复 `rg` 扫 XML。

要点：

- **只读**：服务以 `read_only=True` 打开 SQLite 索引，不写业务工程，不写索引。
- **零第三方依赖**：MCP 服务是 `apsgraph` 自带的纯 Python stdio 实现，无需安装 mcp SDK。
- **协议**：JSON-RPC 2.0 over stdio，逐行一个请求，逐行一个响应，兼容 `initialize` / `tools/list` / `tools/call` 与 `notifications/initialized`。

---

## 2. 前置条件：构建索引

MCP 服务读取的是扫描产物索引。若业务工程还没有索引，先执行一次扫描：

```bash
apsgraph scan --workspace /path/to/业务工程 --db /path/to/业务工程/.apsgraph/apsgraph.db
```

增量更新可用 `apsgraph sync`，只重建有变化的文件。

---

## 3. 启动方式

### 3.1 手动启动（验证用）

```bash
apsgraph serve-mcp \
  --db /path/to/业务工程/.apsgraph/apsgraph.db \
  --workspace /path/to/业务工程
```

启动后进程挂在前台，stdin 收 JSON-RPC 请求，stdout 回 JSON-RPC 响应（人类可读的日志不会污染 stdout）。`--workspace` 用于 `get_entity_source` 解析相对路径。

### 3.2 MCP 客户端配置（stdio）

ZCode / Claude Desktop / Cursor 等客户端的 MCP 配置中，把命令指向 `apsgraph`：

```json
{
  "mcpServers": {
    "apsgraph-metadata": {
      "command": "apsgraph",
      "args": [
        "serve-mcp",
        "--db", "/path/to/业务工程/.apsgraph/apsgraph.db",
        "--workspace", "/path/to/业务工程"
      ]
    }
  }
}
```

服务名（`serverInfo.name`）为 `apsgraph-metadata`，版本为 `0.1.0`。

---

## 4. 工具清单

共 7 个工具，全部返回 `content[0].text`（JSON 字符串）与 `structuredContent`（同内容结构化对象）双通道。

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `search_metadata` | 模糊检索元数据（FTS5，覆盖 ID、中文名、描述、源路径），按 bm25 相关度排序 | `query`（必填）、`limit`（默认 50，上限 500）、范围参数 |
| `find_entity` | 按 `stable_id` / `full_id` / `raw_id` 精确查找实体，用于消除歧义 | `query`（必填）、范围参数 |
| `find_references` | 查询某实体的入边/出边/双向引用，支持多层展开 | `query`（必填）、`direction`（`in`/`out`/`both`，默认 `both`）、`depth`（默认 1，上限 10） |
| `get_dependencies` | 从某实体沿出边遍历依赖 | `query`（必填）、`depth`（默认 3，上限 10） |
| `get_impact` | 反向影响分析（谁引用了我，逐层展开） | `query`（必填）、`depth`（默认 3，上限 10） |
| `find_unresolved_references` | 列出索引中未解析的引用（悬空边） | `relation_type`（可选）、`limit`（默认 50） |
| `get_entity_source` | 返回实体的源 XML 文件位置与原文 | `query`（必填）、`max_chars`（默认 50000，上限 200000）、范围参数 |

### 4.1 范围（scope）参数

`search_metadata`、`find_entity`、`get_entity_source` 支持以下范围参数，可组合使用：

| 参数 | 类型 | 含义 |
|---|---|---|
| `kinds` | string[] | 元数据类型，如 `["TABLE","FIELD","ELEMENT"]` |
| `project` | string | 按项目目录前缀过滤（如 `dept-parent`） |
| `module` | string | 按路径中的模块目录名过滤（如 `dept-bcs`） |
| `path` | string | glob 匹配源文件路径（如 `*/src/main/resources/tables/**`） |
| `file` | string | 按文件名（含尾匹配）过滤 |
| `owner` | string | 按所属父实体 ID 过滤 |
| `top_level` | boolean | 仅保留顶层节点（无 owner） |

### 4.2 结果约定

- `search_metadata` 返回 `{query, scope, count, results[]}`；每个结果带 `stable_id`、`kind`、`full_id`、`chinese_name`、`description`、`file_path`、`matched_fields`。
- `find_entity` / `get_entity_source` 找到 0 个或多于 1 个候选时返回 `error: "not_found_or_ambiguous"` 与 `candidates[]`，调用方应加范围参数收窄。
- 未知工具名抛 JSON-RPC 错误 `-32602`；未知方法返回 `-32601`。

### 4.3 调用示例

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
  "name":"search_metadata",
  "arguments":{"query":"存货档案","kinds":["TABLE","FIELD"],"limit":20}
}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
  "name":"get_impact",
  "arguments":{"query":"BpDict.A.addr","depth":2}
}}
```

---

## 5. 功能、性能与正确性验证

仓库提供 `scripts/verify_mcp.py`，对任意业务工程执行三类验证：

1. **协议与功能**：`initialize` / `tools/list` / 7 个 `tools/call` 逐一验证。
2. **性能**：随机抽样词，长驻 MCP 进程逐词调用 `search_metadata`，与（a）`rg` 直接检索原始 XML、（b）`apsgraph search` CLI 单进程冷启动，对比单次耗时。
3. **正确性**：MCP 返回节点的 `file_path` 必须出现在 `rg` 对同一关键词的命中文件集合中，并统计反向差异（rg 会命中注释、Java 代码、非元数据文本，属预期差异）。

```bash
python3 scripts/verify_mcp.py \
  --workspace /path/to/业务工程 \
  --db /path/to/业务工程/.apsgraph/apsgraph.db \
  --report /tmp/apsgraph-mcp-verify.json
```

脚本使用仓库源码（`PYTHONPATH=src`）启动被测服务；也可 `--apsgraph-bin apsgraph` 改用已安装的 CLI。

---

## 6. 已知限制

1. 服务为单请求串行处理（stdio 逐行），不支持并发请求。
2. `get_entity_source` 直接读工作区文件，要求 `--workspace` 与索引扫描时的工作区一致（或路径可解析）。
3. FTS5 模糊检索基于分词与 bm25，中文按索引分词方式匹配；对不含空格的中文长句建议先取其中的短词。
4. `find_references` / `get_dependencies` / `get_impact` / `get_entity_source` 要求查询词唯一命中一个实体，歧义时会返回候选列表而不是强行选择。
