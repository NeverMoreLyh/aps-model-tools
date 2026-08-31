# APSGraph 运维说明文档

> 版本：0.13.0
> 更新时间：2026-08-23  
> 文档定位：面向维护者和运维人员，说明安装发布、目录管理、日常巡检、故障处理、日志排查、性能治理和回滚策略。

---

## 1. 支持环境

| 项 | 要求 |
|---|---|
| Python | 3.9 或以上 |
| Maven | 使用 `--include-deps` 时需要 |
| JDK | 按项目需要，支持 JDK 8 / 17 混合规则 |
| 数据库驱动 | db-diff 在线模式按需配置 |
| Excel 导出 | `pip install apsgraph[excel]` |

核心 CLI 不要求第三方 Python 依赖。

## 2. 安装与升级

### 2.1 开发安装

```bash
cd /path/to/aps-model-tools
python3 -m pip install -e .
```

### 2.2 Excel 能力

```bash
python3 -m pip install -e ".[excel]"
```

### 2.3 Wheel 发布

```bash
python3 -m pip wheel . --no-deps -w dist
python3 -m pip install --force-reinstall dist/apsgraph-<VERSION>-py3-none-any.whl
```

### 2.4 版本验证

```bash
apsgraph --version
apsgraph options
```

`options` 是只读命令，不会创建索引。

## 3. 目录与产物

默认在 workspace 当前目录下：

```text
.apsgraph/
  apsgraph.db
  dependency-manifest.json
  deps/
  logs/
    maven/
    maven-framework/
    maven-dependencies/
```

| 路径 | 说明 |
|---|---|
| `apsgraph.db` | 主索引 |
| `dependency-manifest.json` | 依赖、JAR、业务标记和模型数量 |
| `deps/` | Maven copy 出来的依赖 JAR |
| `logs/` | Maven 构建与依赖解析日志 |
| `*.tmp` | staging 数据库，正常成功后不会残留 |

可使用 `--cache-dir` 改变 `.apsgraph` 位置，使用 `--db` 指定索引。

## 4. 日常巡检

### 4.1 版本与配置

```bash
apsgraph --version
apsgraph options --workspace /path/to/workspace
```

确认：

- 版本符合预期；
- workspace 路径正确；
- DB 和 cache 默认值正确；
- 排除规则生效；
- JDK 规则生效。

### 4.2 索引状态

```bash
apsgraph status --workspace /path/to/workspace
apsgraph stats --db /path/to/workspace/.apsgraph/apsgraph.db
```

关注：

- parse failed 数量；
- added / modified / deleted 数量；
- unresolved 数量；
- 节点与边规模。

### 4.3 依赖索引

```bash
apsgraph scan --include-deps --deps-mode framework --workspace /path/to/workspace
```

适合日常获取 workspace + 框架 JAR 模型。

### 4.4 纯源码索引

```bash
apsgraph scan --workspace /path/to/workspace
```

不调用 Maven，适合快速刷新 workspace XML。

## 5. Workspace 配置

`.apsgraph.json` 示例：

```json
{
  "excludeProjects": ["*dist", "packaging/*"],
  "maven": {
    "excludeProjects": ["*dist"],
    "jdk": {
      "default": "8",
      "javaHomes": {
        "8": "/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home",
        "17": "auto"
      },
      "rules": [
        {"match": ["api-parent", "api-parent/*"], "jdk": "17"}
      }
    }
  }
}
```

### 5.1 JDK 策略

优先级：

```text
--project-jdk
> --jdk
> workspace rule
> workspace default
> 当前 JAVA_HOME
```

同一 build unit 解析出多个 profile 时会失败，需要拆分 reactor或显式修正规则。

### 5.2 JAXB 缺类

如果日志包含：

```text
java.lang.ClassNotFoundException: javax.xml.bind.JAXBException
```

处理建议：

1. 确认该项目是否必须使用 JDK 8；
2. 为项目配置 JDK 8 profile；
3. 若必须 JDK 17，给 Maven 增加 JAXB 依赖；
4. 查看 `.apsgraph/logs/` 中完整命令和日志；
5. 修正后重新执行 scan，既有索引不会被失败任务破坏。

## 6. 日志排查

| 日志 | 位置 |
|---|---|
| 全量 Maven build | `.apsgraph/logs/maven/` |
| framework build | `.apsgraph/logs/maven-framework/` |
| dependency list / copy | `.apsgraph/logs/maven-dependencies/` |

排查顺序：

1. 查看 stderr 中的失败阶段；
2. 打开对应阶段日志；
3. 查找 `[ERROR]`、`BUILD FAILURE`、`ClassNotFoundException`；
4. 查看 `dependency-manifest.json`；
5. 检查 POM 业务标记和版本冲突；
6. 修正后重跑。

## 7. 常见故障

### 7.1 Maven 构建失败

现象：

```text
Maven build failed for ...
```

处理：

1. 打开日志；
2. 在项目目录手动复现命令；
3. 区分源码错误、JDK 错误、依赖仓库问题；
4. 修复后重跑；
5. 失败期间主索引不会被替换。

### 7.2 同坐标多版本

现象：

```text
conflicting Maven dependency versions are not allowed
```

处理：

- 检查 `groupId:artifactId` 相同但 version 不同的依赖；
- 使用 dependency management 固定版本；
- 修正后重跑；
- 不同 groupId 的相同 artifactId 不是该错误。

### 7.3 parent 单独编译失败但项目内成功

检查：

- 是否从 parent 根目录执行；
- 是否缺少 `-N`；
- 是否使用了错误 JDK；
- 本地仓库是否包含必需 parent；
- workspace 相对路径是否与手动执行一致。

framework 模式应显示边界项目，例如 `ap-parent`，并执行 `mvn -B -DskipTests -N install`。

### 7.4 unresolved 过多

确认使用版本不低于 0.10.0。新版会：

- 依赖导入后刷新最终统计；
- 过滤非模型引用；
- 拆分多值 extension。

如果仍有 unresolved：

1. 判断目标是否业务模型；
2. 确认依赖 JAR 是否含业务标记；
3. 检查是否需要 full 模式；
4. 考虑为依赖源码生成 external DB；
5. 用 `show` 查询目标是否在索引中。

### 7.5 XML 解析失败

查看 `stats` / scan JSON 中的 failed files。处理：

- 修复 XML；
- 或明确接受失败并继续；
- 需要 CI 阻断时使用 `--fail-on-parse-error`。

### 7.6 索引不存在或非法

现象：

```text
index database does not exist
refusing to rebuild database without a valid APS index identity
```

处理：

- 检查 `--db`；
- 不应手工编辑 SQLite；
- 删除或移动非法文件后重新 scan；
- 跨 workspace 索引需重新构建。

## 8. 性能治理

| 问题 | 建议 |
|---|---|
| Maven 慢 | 预热本地仓库，保留 `.m2` 缓存 |
| framework scan 慢 | 确认只构建 parent 边界 |
| 全量 scan 慢 | 日常使用 `sync` |
| 并发不足 | 使用 `--jobs` 提高 Maven 阶段并行 |
| 磁盘紧张 | 清理旧日志和 `deps/`，保留 manifest |
| 查询过慢 | 检查 DB 是否 SSD，限制 depth |
| CI 抖动 | 固定 Maven repo、JDK 和 Python 版本 |

## 9. 备份与回滚

### 9.1 需要备份

- 当前 `apsgraph.db`；
- `dependency-manifest.json`；
- workspace 配置 `.apsgraph.json`。

### 9.2 索引回滚

```bash
cp backup/apsgraph.db .apsgraph/apsgraph.db
```

建议停止正在执行的 scan / sync 后再复制。

### 9.3 工具版本回滚

```bash
python3 -m pip install --force-reinstall apsgraph==<VERSION>
apsgraph --version
```

### 9.4 重建

```bash
apsgraph scan --include-deps --deps-mode framework
```

失败不会覆盖既有索引，可安全重试。

## 10. CI 建议

```bash
apsgraph --version
apsgraph scan --include-deps --deps-mode framework
apsgraph status
apsgraph stats --db .apsgraph/apsgraph.db
```

建议：

- 缓存 `.m2`；
- 缓存 `.apsgraph/deps` 可选；
- stdout JSON 写入 artifact；
- stderr 日志写入 console；
- 对 unresolved 建立基线，而非简单设置为零；
- Maven 失败直接阻断；
- XML parse failed 是否阻断按团队策略配置。

## 11. 安全运维

- 不要把包含密码的 DSN 写入脚本。
- 在线 db-diff 使用环境变量保存密码。
- 输出报告会脱敏 DSN，但仍应限制 artifact 访问。
- `.apsgraph` 中包含路径和模型信息，应与源码同等访问控制。
- 不建议把生成 DDL 直接交给自动化执行，必须人工评审。

## 12. 维护规则

每次修改必须：

1. 更新五类核心文档之一：
   - `requirements.md`
   - `design.md`
   - `product-whitepaper.md`
   - `usage-guide.md`
   - `operations-guide.md`
2. 升级版本；
3. 跑全量测试；
4. commit；
5. 构建 wheel；
6. 安装并验证 CLI；
7. 对涉及 Maven / 真实 workspace 的功能做实测；
8. 按 `AGENTS.md` 的代码版本管理规范提交并推送代码。

仓库根目录的 `AGENTS.md` 是代理协作和发布流程的强制规则入口。
