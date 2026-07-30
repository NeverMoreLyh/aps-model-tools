from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .store import connect


CAPABILITIES: Mapping[str, Tuple[str, Sequence[str]]] = {
    "day-switch": ("统一日切功能", ("day switch", "day_switch", "日切", "核心日期")),
    "security": ("安全组件", ("security", "encrypt", "decrypt", "pin", "mac", "cvv", "arqc", "密押", "密钥", "安全")),
    "redis": ("Redis工具类", ("redis", "cache", "缓存")),
    "socket": ("Socket工具类", ("socket", " tcp", "通信连接")),
    "outbound": ("外发组件", ("outbound", "外发", "remote send")),
    "eod-control": ("日终禁用解禁", ("eod", "end of day", "日终", "禁用", "解禁")),
    "online-hooks": ("联机前后处理", ("online", "联机前", "联机后", "interceptor")),
    "prompt-auth": ("提示、警告、授权", ("prompt", "warning", "authorize", "提示", "警告", "授权")),
    "batch-hooks": ("批量前后处理", ("batch", "批量前", "批量后")),
    "dao-hooks": ("DAO前后处理", ("dao", "odb", "dao前", "dao后")),
    "service-engine-hooks": ("服务引擎前后处理", ("service engine", "服务引擎", "serviceinvoke")),
    "optimistic-lock": ("乐观锁机制", ("optimistic", "乐观锁", "version lock")),
    "plugin": ("可插拔功能", ("plugin", "spi", "extension", "扩展点", "可插拔")),
    "file-transfer": ("文件传输", ("filetransfer", "file transfer", "文件传输", "上传", "下载")),
    "coding-rule": ("编码规则", ("coderule", "code rule", "编码规则")),
    "sequence-parameter": ("序号参数", ("sequence parameter", "序号参数")),
    "sms": ("短信通知", ("sms", "short mesg", "short message", "短信")),
    "journal-sequence": ("流水号机制", ("journal sequence", "rung num", "流水号", "序列号")),
    "parameter-io": ("参数导入导出", ("parameter import", "parameter export", "参数导入", "参数导出")),
    "utilities": ("工具类", ("tools", "util", "helper", "工具")),
    "batch-to-online": ("批量转联机", ("batch to online", "batchonl", "批转联")),
    "data-clean": ("数据清理", ("dataclean", "data clean", "purge", "retention", "数据清理", "归档清理")),
    "common-file": ("通用文件处理", ("common file", "file split", "file merge", "通用文件", "文件拆分")),
    "unitization-extension": ("单元化扩展", ("unitization", "单元化")),
    "reversal-inquiry-rollback": ("统一冲正、查证、回滚", ("reversal", "rollback", "verification", "冲正", "查证", "回滚")),
    "idempotency": ("防重幂等", ("idempot", "duplicate prevention", "防重", "幂等")),
    "business-log": ("业务日志登记", ("business log", "busilog", "txn log", "业务日志", "交易日志")),
    "message-adapter": ("报文适配", ("message adapter", "报文适配", "协议转换")),
    "runtime-context": ("公共运行区", ("runenv", "runtime context", "运行区", "运行环境")),
    "aps-extension": ("APS平台扩展", ("aps extension", "platform extension", "平台扩展")),
    "parameter-maintenance": ("统一参数维护", ("parameter maintenance", "参数维护")),
    "transaction-state": ("事务状态控制", ("transaction state", "事务状态", "提交控制")),
    "dynamic-static-list": ("动态列表与静态列表", ("dynamic list", "static list", "dynmc list", "动态列表", "静态列表")),
    "sharding": ("Shard分片机制", ("shard", "sharding", "分片")),
    "file-config": ("文件配置", ("file config", "文件配置", "文件参数")),
}
TARGET_KINDS = {"COMPLEX_TYPE", "TABLE", "SQL_GROUP", "NAMED_SQL", "SERVICE_TYPE", "SERVICE_IMPLEMENTATION"}


def _normalize(value: str) -> str:
    return re.sub(r"[_./:$-]+", " ", value.lower())


def _term_matches(text: str, term: str) -> bool:
    normalized = _normalize(term).strip()
    if not normalized:
        return False
    if re.fullmatch(r"[a-z0-9]+", normalized):
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None
    return normalized in text


def classify_text(*parts: str) -> List[Dict[str, Any]]:
    text = _normalize(" ".join(part for part in parts if part))
    matches = []
    for capability, (label, terms) in CAPABILITIES.items():
        evidence = sorted({term for term in terms if _term_matches(text, term)})
        if evidence:
            matches.append({"id": capability, "label": label, "evidence": evidence,
                            "confidence": "DERIVED" if len(evidence) > 1 else "INFERRED"})
    return matches


def _java_packages(workspace: Path) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for path in workspace.rglob("*.java"):
        relative = path.relative_to(workspace)
        if any(part in {".git", "target", ".codegraph", ".idea"} for part in relative.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
        if not match:
            continue
        repository = relative.parts[0] if relative.parts else ""
        package = match.group(1)
        key = (repository, package)
        entry = grouped.setdefault(key, {"repository": repository, "package": package, "files": 0,
                                         "classes": [], "capability_scores": Counter()})
        entry["files"] += 1; entry["classes"].append(path.stem)
        for item in classify_text(package, path.stem, relative.as_posix()):
            entry["capability_scores"][item["id"]] += len(item["evidence"])
    result = []
    for entry in grouped.values():
        scores = entry.pop("capability_scores")
        ordered = [item[0] for item in scores.most_common()]
        entry.update({"capabilities": ordered, "primary_capability": ordered[0] if len(ordered) == 1 else "QUESTION",
                      "candidate_mixed_capabilities": len(ordered) > 1,
                      "classification": "CANDIDATE" if ordered else "UNCLASSIFIED",
                      "classes": sorted(entry["classes"])})
        result.append(entry)
    return sorted(result, key=lambda item: (item["repository"], item["package"]))


def audit_capabilities(aps_db: Path | str, workspace: Path | str) -> Dict[str, Any]:
    root = Path(workspace).resolve()
    conn = connect(aps_db, read_only=True)
    try:
        state = conn.execute("select workspace from scan_state where id=1").fetchone()
        if state and Path(state[0]).resolve() != root:
            raise ValueError(f"index belongs to another workspace: {state[0]}")
        rows = conn.execute("""select f.id,f.path,f.model_id,f.package_name,f.suffix,
            n.full_id,n.kind,n.properties_json
            from model_files f left join nodes n on n.file_id=f.id
            where f.parse_status='PARSED' order by f.path,n.id""").fetchall()
        failures = [dict(row) for row in conn.execute(
            "select path,error_message from model_files where parse_status='PARSE_FAILED' order by path")]
    finally:
        conn.close()
    files: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        entry = files.setdefault(row["id"], {"path": row["path"], "model_id": row["model_id"],
            "package": row["package_name"], "suffix": row["suffix"], "target_nodes": [], "matches": defaultdict(list)})
        if row["kind"] not in TARGET_KINDS:
            continue
        props = json.loads(row["properties_json"] or "{}")
        matches = classify_text(row["path"], row["model_id"] or "", row["package_name"] or "",
                                row["full_id"] or "", " ".join(str(v) for v in props.values()))
        entry["target_nodes"].append({"full_id": row["full_id"], "kind": row["kind"], "matches": matches})
        for match in matches:
            entry["matches"][match["id"]].extend(match["evidence"])
    model_files = []
    for entry in files.values():
        capabilities = sorted(entry.pop("matches"))
        entry.update({"capabilities": capabilities, "candidate_mixed_capabilities": len(capabilities) > 1,
                      "classification": "CANDIDATE" if capabilities else "UNCLASSIFIED"})
        model_files.append(entry)
    model_files.sort(key=lambda item: item["path"])
    java_packages = _java_packages(root)
    model_by_capability = defaultdict(list)
    for item in model_files:
        for capability in item["capabilities"]:
            model_by_capability[capability].append({"model_id": item["model_id"], "path": item["path"],
                                                    "classification": item["classification"]})
    java_by_capability = defaultdict(list)
    for item in java_packages:
        for capability in item["capabilities"]:
            java_by_capability[capability].append({"repository": item["repository"], "package": item["package"],
                                                   "classification": item["classification"]})
    capability_coverage = []
    for capability, (label, _) in CAPABILITIES.items():
        model_evidence = model_by_capability[capability]
        java_evidence = java_by_capability[capability]
        capability_coverage.append({"id": capability, "label": label,
            "status": "CANDIDATE" if model_evidence or java_evidence else "NOT_FOUND",
            "model_evidence": model_evidence, "java_evidence": java_evidence,
            "question": "需架构负责人结合模型关系、生成物和调用链确认" if model_evidence or java_evidence else "当前关键词证据未发现；不等同能力不存在"})
    return {"workspace": str(root), "capability_catalog": {key: value[0] for key, value in CAPABILITIES.items()},
            "summary": {"model_files": len(model_files), "candidate_mixed_model_files": sum(x["candidate_mixed_capabilities"] for x in model_files),
                        "unclassified_model_files": sum(not x["capabilities"] for x in model_files),
                        "java_packages": len(java_packages), "candidate_mixed_java_packages": sum(x["candidate_mixed_capabilities"] for x in java_packages),
                        "unclassified_java_packages": sum(x["classification"] == "UNCLASSIFIED" for x in java_packages)},
            "model_files": model_files, "java_packages": java_packages, "capability_coverage": capability_coverage,
            "parse_failures": failures,
            "limitations": ["All keyword classifications are CANDIDATE evidence and require architecture-owner confirmation.",
                            "Generated Java and CodeGraph consumers are not yet inputs to capability classification."]}


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# V8.7 元数据与 Java 功能分类现状分析", "", f"- 工作空间：`{report['workspace']}`", "",
             "## 汇总", "", "| 指标 | 数量 |", "| --- | ---: |"]
    for key, value in report["summary"].items(): lines.append(f"| {key} | {value} |")
    lines += ["", "## 35项能力覆盖矩阵", "", "| 能力 | 状态 | 模型候选 | Java候选 | 待确认 |", "| --- | --- | ---: | ---: | --- |"]
    for item in report["capability_coverage"]:
        lines.append(f"| {item['label']} | {item['status']} | {len(item['model_evidence'])} | {len(item['java_evidence'])} | {item['question']} |")
    lines += ["", "## 元数据候选混合热点", ""]
    mixed = [item for item in report["model_files"] if item["candidate_mixed_capabilities"]]
    if not mixed: lines.append("未识别到多功能候选模型文件。")
    for item in mixed:
        labels = [report["capability_catalog"][cap] for cap in item["capabilities"]]
        lines += [f"### `{item['model_id'] or item['path']}`", "", f"- 当前文件：`{item['path']}`",
                  f"- 候选能力：{'、'.join(labels)}", "- 候选改进方向：先由架构负责人用模型关系、生成物和调用链确认是否真的混合，再决定保留共享模型或按能力拆分。", ""]
    lines += ["## Java package 分类", "", "| 仓库 | package | 主能力 | 文件数 |", "| --- | --- | --- | ---: |"]
    for item in report["java_packages"]:
        cap = item["primary_capability"]
        label = report["capability_catalog"].get(cap, cap)
        lines.append(f"| {item['repository']} | `{item['package']}` | {label} | {item['files']} |")
    lines += ["", "## 解析失败", ""]
    if report["parse_failures"]:
        for item in report["parse_failures"]:
            lines.append(f"- `{item['path']}`：{item['error_message']}")
    else:
        lines.append("无。")
    lines += ["", "## 改进原则", "", "1. 候选分类不是架构事实；先确认功能边界，再迁移模型、生成Java和手写Java。",
              "2. 对 `Common/Tools/Sys` 建立准入条件，能归入具体功能的优先归类。",
              "3. 每次迁移保留旧契约或委托层，验证新旧节点混跑、回滚、模型生成和CodeGraph调用方。", "",
              "## 限制", ""] + [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"
