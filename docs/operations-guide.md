# APSGraph 运维说明文档

> 版本：0.43.0
> 更新时间：2026-09-20
> 文档定位：说明安装发布、索引巡检、故障处理、备份回滚和 CI 使用。

## 1. 支持环境

| 项 | 要求 |
|---|---|
| Python | 3.9 或以上 |
| 数据库驱动 | db-diff 在线模式按需配置 |
| Excel 导出 | `pip install apsgraph[excel]` |

核心 CLI 不要求第三方 Python 依赖，不调用 Maven，不选择 JDK。

## 2. 安装与升级

### 2.1 install.sh 一键安装

```bash
./install.sh          # 构建并安装（自动适配 PEP 668 等环境限制）
PYTHON=python3.10 ./install.sh   # 指定解释器
```

### 2.2 pip 安装/升级

```bash
python3 -m pip install -e .
python3 -m pip wheel . --no-deps -w dist
python3 -m pip install --force-reinstall dist/apsgraph-<VERSION>-py3-none-any.whl
apsgraph --version
apsgraph options
```

### 2.3 从 Git 仓库安装

```bash
python3 -m pip install git+https://github.com/NeverMoreLyh/aps-model-tools.git
```

### 2.4 发布到 PyPI（可选）

```bash
python3 -m pip install build twine
python3 -m build            # 生成 dist/ 下的 sdist 与 wheel
python3 -m twine upload dist/*   # 需要 PyPI 账号与 API Token；发布后用户可直接 pip install apsgraph
```

发布到 PyPI 后，终端用户安装命令简化为 `python3 -m pip install apsgraph`（Excel 导出装 `apsgraph[excel]`）。

## 3. 目录与产物

默认在 workspace 当前目录下：

```text
.apsgraph/
  apsgraph.db
```

用户主目录下有两个用户级注册表（跨 workspace 共享）：

```text
~/.apsgraph/
  workbench-registry.json   # 运行中的 workbench 实例（list/close）
  registry.json             # 已 scan 的 workspace 注册表（APSGRAPH_HOME 可重定向）
```

`scan` 使用临时 SQLite 文件完成全量重建并原子替换目标索引；成功后不应残留临时文件。`scan` 成功后会以原子替换方式把 workspace 注册进 `~/.apsgraph/registry.json`（CI 中建议用 `APSGRAPH_HOME` 指向临时目录隔离机器状态）。注册表损坏时相关命令 fail-closed 报错，可删除该文件后重新 `scan` 各 workspace 重建（注册信息可完全由重新扫描恢复，不承载索引数据）。

## 4. 日常巡检

单仓库巡检：

```bash
apsgraph options --workspace /path/to/workspace
apsgraph status --workspace /path/to/workspace
apsgraph stats --db /path/to/workspace/.apsgraph/apsgraph.db
```

多仓库巡检（基于全局注册表，一次覆盖所有已注册 workspace；默认人类可读表格，加 `--json` 输出机器可读格式供流水线消费）：

```bash
apsgraph workspace list          # 注册条目与索引可用性总览（同名目录用短 id 区分）
apsgraph workspace status --all  # 逐仓库报告 fresh / stale / missing
apsgraph workspace check --all   # 索引健康（schema 版本 + integrity_check）
```

关注 `missing`/`stale` 数量、added/modified/deleted 数量、解析失败文件与 `corrupt` 报告；`check` 报损坏的索引用 `apsgraph workspace rebuild --workspace <token>` 修复。外部索引可通过 `scan --external-db DB` 复用，workspace 定义优先。

## 5. Workspace 索引

```bash
apsgraph scan --workspace /path/to/workspace
apsgraph sync --workspace /path/to/workspace
# 多仓库批量（等价于逐仓库执行）：
apsgraph workspace sync --all
apsgraph workspace rebuild --all   # scanner 版本升级或索引损坏后的全量重建
```

`scan` 只解析受支持的 XML 并重建 SQLite V2 索引；`sync` 按文件路径和 SHA-256 更新已有索引。两者均不修改业务源码。低频维护可在服务空闲时执行 `apsgraph workspace vacuum --all` 压缩索引（索引正被 workbench 服务时会因占用失败跳过，先 `workbench close` 再执行）。

## 6. 常见故障

### 6.1 unresolved 过多

确认目标模型确实存在于 workspace 或外部 SQLite 索引中，并检查引用值是否只是 Java 类名、SQL primitive 或其他非 APS 值。

### 6.2 XML 解析失败

查看 `scan` 或 `sync` JSON 中的 `failed_files`，修复对应 XML；需要流水线阻断时使用 `--fail-on-parse-error`。

### 6.3 索引不存在或非法

检查 `--db` 路径。不要手工修改 SQLite；移走非法文件后重新执行 `scan`。跨 workspace 索引需要重新构建。

### 6.4 workbench 启动失败

报 `index database does not exist` 时先对目标工作区执行 `apsgraph scan`，或用 `--db` 指向正确索引；多 workspace 模式下该错误意味着默认选中 workspace 的索引缺失，重新对其 `scan` 或在页面选择器中切换到其他 workspace。报 `no registered workspaces` 时表示 workspace 注册表为空且当前目录无索引，先 `scan`。报 `unknown workspace` 时按错误信息中的注册名列表修正 `--workspace`；同名注册项冲突时改用 workspace 路径指定。默认即绑定随机空闲端口，多实例互不冲突；显式 `--port N` 指定的端口被占用时报错退出（不会自动降级换端口）；`--no-browser` 可在无桌面环境的 CI/远程主机上只打印 URL 不拉起浏览器。

需要同时打开多个工作区时，为每个工作区分别执行 `apsgraph workbench --port 0`，然后用 `apsgraph workbench list` 查看全部实例（端口、PID、工作区；默认人类可读表格，`--json` 供脚本），用 `apsgraph workbench close --port N`（或 `--all`）关闭；该命令读取用户级注册表 `~/.apsgraph/workbench-registry.json`（可用 `APSGRAPH_WORKBENCH_REGISTRY` 覆盖），过期条目（进程已退出）会在 list 时自动清理。若怀疑注册表条目过期，直接重新执行 `workbench list` 即可自愈，无需手工编辑该文件。跨仓库查询优先使用多 workspace 模式：一个实例即可切换全部已注册工作区，无需逐个启动。

### 6.5 注册表损坏或 workspace 失效

`invalid workspace registry` 报错说明 `~/.apsgraph/registry.json` 损坏或结构非法：修正或删除该文件后重新 `scan` 各 workspace 即可重建，不影响任何已构建索引。workbench 选择器中标注"（失效）"的条目表示其索引文件已不存在，对该 workspace 重新 `scan` 即可恢复；条目不会自动剔除。确认不再需要的条目用 `apsgraph workspace remove --workspace <id|名称|路径>` 移除（同名目录用短 id 精确指定）（确认不再需要磁盘索引时才加 `--purge`，该选项会删除 `.apsgraph/` 缓存目录）。

### 6.6 workspace sync/vacuum 报 database is locked

批量维护与运行中的 workbench 并发时会按 fail-soft 记录失败并继续其余 workspace。处理：先 `apsgraph workbench list` 找到占用实例，`apsgraph workbench close --port <port>` 停止后重跑失败项（`apsgraph workspace sync --workspace <token>`）。

## 7. 性能治理

日常变更优先使用 `sync`，大范围变更或索引损坏时使用 `scan`。查询应限制影响分析深度，大型索引放在可靠的本地磁盘。

## 8. 备份与回滚

备份 `.apsgraph/apsgraph.db` 和必要的 workspace 配置。停止正在运行的 `scan`/`sync` 后，可以恢复数据库备份：

```bash
cp backup/apsgraph.db .apsgraph/apsgraph.db
```

全局注册表 `~/.apsgraph/registry.json` 可一并备份；它不承载索引数据，丢失后重新 `scan` 各 workspace 即可重建。

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
- `~/.apsgraph` 仅用于两个用户级注册表：实例注册表（`workbench-registry.json`）与全局 workspace 注册表（`registry.json`，只有 `scan` 注册与 workbench "最后使用"时间戳刷新两处写入）；均为临时文件原子替换，不写入任何索引或源码内容。
- DDL 只生成，不执行。
- CodeGraph 数据库只读。
- `workbench` 查询工作台仅绑定 `127.0.0.1`，索引以只读模式打开，不接受任何写请求（非 GET 一律 405）。不要将端口转发或反向代理到公网；如需远程访问，由运维侧自行落地鉴权与访问控制。
- workbench 实例注册表只写入用户主目录 `~/.apsgraph/workbench-registry.json`，不触碰业务仓库与 CodeGraph 数据库；`workbench close` 在终止进程前先探测目标端口仍响应 workbench `/api/stats`，避免 pid 复用时误杀无关进程。
