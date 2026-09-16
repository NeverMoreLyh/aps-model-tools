# APSGraph 运维说明文档

> 版本：0.28.1
> 更新时间：2026-09-16
> 文档定位：说明安装发布、索引巡检、故障处理、备份回滚和 CI 使用。

## 1. 支持环境

| 项 | 要求 |
|---|---|
| Python | 3.9 或以上 |
| 数据库驱动 | db-diff 在线模式按需配置 |
| Excel 导出 | `pip install apsgraph[excel]` |

核心 CLI 不要求第三方 Python 依赖，不调用 Maven，不选择 JDK。

## 2. 安装与升级

```bash
python3 -m pip install -e .
python3 -m pip wheel . --no-deps -w dist
python3 -m pip install --force-reinstall dist/apsgraph-<VERSION>-py3-none-any.whl
apsgraph --version
apsgraph options
```

## 3. 目录与产物

默认在 workspace 当前目录下：

```text
.apsgraph/
  apsgraph.db
```

`scan` 使用临时 SQLite 文件完成全量重建并原子替换目标索引；成功后不应残留临时文件。

## 4. 日常巡检

```bash
apsgraph options --workspace /path/to/workspace
apsgraph status --workspace /path/to/workspace
apsgraph stats --db /path/to/workspace/.apsgraph/apsgraph.db
```

关注解析失败文件、added/modified/deleted 数量、unresolved 数量以及节点和边规模。外部索引可通过 `scan --external-db DB` 复用，workspace 定义优先。

## 5. Workspace 索引

```bash
apsgraph scan --workspace /path/to/workspace
apsgraph sync --workspace /path/to/workspace
```

`scan` 只解析受支持的 XML 并重建 SQLite V2 索引；`sync` 按文件路径和 SHA-256 更新已有索引。两者均不修改业务源码。

## 6. 常见故障

### 6.1 unresolved 过多

确认目标模型确实存在于 workspace 或外部 SQLite 索引中，并检查引用值是否只是 Java 类名、SQL primitive 或其他非 APS 值。

### 6.2 XML 解析失败

查看 `scan` 或 `sync` JSON 中的 `failed_files`，修复对应 XML；需要流水线阻断时使用 `--fail-on-parse-error`。

### 6.3 索引不存在或非法

检查 `--db` 路径。不要手工修改 SQLite；移走非法文件后重新执行 `scan`。跨 workspace 索引需要重新构建。

### 6.4 workbench 启动失败

报 `index database does not exist` 时先对目标工作区执行 `apsgraph scan`，或用 `--db` 指向正确索引。报端口占用时用 `--port` 换端口；`--no-browser` 可在无桌面环境的 CI/远程主机上只打印 URL 不拉起浏览器。

## 7. 性能治理

日常变更优先使用 `sync`，大范围变更或索引损坏时使用 `scan`。查询应限制影响分析深度，大型索引放在可靠的本地磁盘。

## 8. 备份与回滚

备份 `.apsgraph/apsgraph.db` 和必要的 workspace 配置。停止正在运行的 `scan`/`sync` 后，可以恢复数据库备份：

```bash
cp backup/apsgraph.db .apsgraph/apsgraph.db
```

工具版本回滚：

```bash
python3 -m pip install --force-reinstall apsgraph==<VERSION>
apsgraph --version
```

## 9. CI 建议

```bash
apsgraph --version
apsgraph scan --workspace /path/to/workspace
apsgraph status --workspace /path/to/workspace
apsgraph stats --db /path/to/workspace/.apsgraph/apsgraph.db
```

保留 stdout JSON 作为构建产物，并按团队策略决定 `--fail-on-parse-error` 是否阻断流水线。

## 10. 安全运维

- 业务源码只读。
- `.apsgraph` 仅用于索引和 SQLite 产物。
- DDL 只生成，不执行。
- CodeGraph 数据库只读。
- `workbench` 查询工作台仅绑定 `127.0.0.1`，索引以只读模式打开，不接受任何写请求（非 GET 一律 405）。不要将端口转发或反向代理到公网；如需远程访问，由运维侧自行落地鉴权与访问控制。
