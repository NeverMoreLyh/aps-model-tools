#!/usr/bin/env python3
"""APSGraph MCP 服务功能、性能与正确性验证脚本。

对任意业务工程执行三类验证：

1. 协议与功能：initialize / tools/list / 7 个 tools/call 逐一验证。
2. 性能：随机抽样 raw_id 与中文名，长驻 MCP 进程逐词调用 search_metadata，
   与（a）rg 直接检索原始 XML、（b）apsgraph search CLI 冷启动对比单次耗时。
3. 正确性：MCP 返回节点的 file_path 必须出现在 rg 对同一关键词的命中文件
   集合中；rg 会额外命中注释、Java 代码、非元数据文本，属预期差异，单独统计。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXPECTED_TOOLS = {
    "search_metadata", "find_entity", "find_references", "get_dependencies",
    "get_impact", "find_unresolved_references", "get_entity_source",
}


def ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class McpClient:
    """Long-lived stdio MCP client: one JSON-RPC request per line."""

    def __init__(self, command: list, env: dict):
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
        )
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        if self.proc.poll() is not None:
            raise AssertionError(f"MCP server exited: {self.proc.stderr.read()[-2000:]}")
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            request["params"] = params
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"MCP server closed stdout (code={self.proc.poll()})")
        response = json.loads(line)
        if response.get("id") != self.next_id:
            raise AssertionError(f"MCP response id mismatch: {response}")
        if "error" in response:
            raise AssertionError(f"MCP error for {method}: {response['error']}")
        return response["result"]

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})["structuredContent"]

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait(timeout=10)


def resolve_file(workspace: Path, file_path: str) -> Path:
    path = Path(file_path)
    return path if path.is_absolute() else workspace / path


def rg_files(workspace: Path, term: str) -> set:
    # -i because the index normalizes ASCII ids to lowercase while raw XML keeps original case
    proc = subprocess.run(
        ["rg", "-F", "-i", "-l", "--glob", "*.xml", "--", term, str(workspace)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return {line for line in proc.stdout.splitlines() if line.strip()}


def choose_samples(db: Path, rng: random.Random, per_category: int) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    raw_rows = conn.execute("""
        select n.stable_id, n.raw_id, n.kind, n.full_id, f.path
        from nodes n join model_files f on f.id=n.file_id
        where n.raw_id is not null and length(n.raw_id) between 4 and 40
        group by n.raw_id order by random() limit ?
    """, (per_category,)).fetchall()
    # model_search.longname stores tokenized text; sample real names from properties_json
    cjk_rows = conn.execute("""
        select n.stable_id, n.raw_id, n.kind, n.full_id, f.path, n.properties_json
        from nodes n join model_files f on f.id=n.file_id
        where n.properties_json like '%"longname":%'
        order by random() limit 400
    """).fetchall()
    cjk_unique: dict = {}
    for row in cjk_rows:
        try:
            term = json.loads(row["properties_json"]).get("longname") or ""
        except json.JSONDecodeError:
            continue
        if 2 <= len(term) <= 8 and all('\u4e00' <= ch <= '\u9fff' for ch in term):
            cjk_unique.setdefault(term, dict(row) | {"term": term})
    cjk_rows = rng.sample(list(cjk_unique.values()), min(per_category, len(cjk_unique)))
    if len(cjk_unique) < 1:
        raise SystemExit("index lacks Chinese longname samples")
    table = conn.execute("""
        select n.stable_id, n.raw_id, f.path from nodes n
        join model_files f on f.id=n.file_id
        where n.kind='TABLE' and n.stable_id is not null
        group by n.raw_id having count(*)=1 order by random() limit 1
    """).fetchone()
    ref = conn.execute("""
        select n.stable_id, n.raw_id, n.kind, f.path from nodes n
        join edges e on e.to_node_id=n.id
        join model_files f on f.id=n.file_id
        where n.stable_id is not null order by random() limit 1
    """).fetchone()
    hub = conn.execute("""
        select n.stable_id, n.raw_id, f.path from nodes n
        join edges e on e.from_node_id=n.id
        join model_files f on f.id=n.file_id
        where n.kind='TABLE' and n.stable_id is not null
        group by n.id order by count(*) desc limit 1
    """).fetchone()
    conn.close()
    if not raw_rows or not cjk_rows or not all((table, ref, hub)):
        raise SystemExit("index lacks usable samples; rebuild it with apsgraph scan")
    return {
        "raw_terms": [dict(row) for row in raw_rows],
        "cjk_terms": [dict(row) for row in cjk_rows],
        "table": dict(table), "ref": dict(ref), "hub": dict(hub),
    }


def run_functional(client: McpClient, samples: dict, workspace: Path) -> list:
    checks = []
    def record(label: str, ok: bool, detail: dict):
        checks.append({"label": label, "ok": bool(ok), **detail})

    result = client.request("initialize", {"protocolVersion": "2024-11-05"})
    record("initialize", result["serverInfo"]["name"] == "apsgraph-metadata", {"serverInfo": result["serverInfo"]})

    tools = client.request("tools/list")["tools"]
    names = {tool["name"] for tool in tools}
    record("tools_list", EXPECTED_TOOLS <= names, {"tools": sorted(names), "missing": sorted(EXPECTED_TOOLS - names)})

    table, ref, hub = samples["table"], samples["ref"], samples["hub"]

    def run_check(label: str, call: callable, verify: callable):
        try:
            payload = call()
            ok, detail = verify(payload)
        except Exception as exc:
            payload, ok, detail = None, False, {"exception": str(exc)}
        record(label, ok, detail)

    run_check("search_metadata",
              lambda: client.call_tool("search_metadata", {"query": table["raw_id"], "kinds": ["TABLE"], "limit": 20}),
              lambda p: (p["count"] >= 1 and any(r["stable_id"] == table["stable_id"] for r in p["results"]),
                         {"query": table["raw_id"], "count": p["count"]}))

    run_check("find_entity",
              lambda: client.call_tool("find_entity", {"query": table["stable_id"], "kinds": ["TABLE"]}),
              lambda p: (p["count"] == 1,
                         {"query": table["stable_id"], "count": p["count"],
                          "ambiguous": p.get("error") == "not_found_or_ambiguous"}))

    run_check("find_references",
              lambda: client.call_tool("find_references", {"query": hub["stable_id"], "direction": "out", "depth": 1}),
              lambda p: (p.get("edges") and len(p.get("nodes", [])) >= 2,
                         {"root": hub["stable_id"], "nodes": len(p.get("nodes", [])),
                          "edges": len(p.get("edges", [])), "error": p.get("error")}))

    run_check("get_dependencies",
              lambda: client.call_tool("get_dependencies", {"query": hub["stable_id"], "depth": 2}),
              lambda p: (len(p.get("edges", [])) >= 1,
                         {"root": hub["stable_id"], "nodes": len(p.get("nodes", [])),
                          "edges": len(p.get("edges", [])), "error": p.get("error")}))

    run_check("get_impact",
              lambda: client.call_tool("get_impact", {"query": ref["stable_id"], "depth": 2}),
              lambda p: ("target" in p and len(p.get("paths", [])) >= 1,
                         {"target": ref["stable_id"], "affected_nodes": len(p.get("affected_nodes", [])),
                          "paths": len(p.get("paths", [])), "error": p.get("error")}))

    run_check("find_unresolved_references",
              lambda: client.call_tool("find_unresolved_references", {"limit": 5}),
              lambda p: (isinstance(p.get("results"), list) and p["count"] <= 5, {"count": p["count"]}))

    def verify_source(p):
        xml = p.get("xml", "")
        source = p.get("source_path", "")
        return bool(xml) and resolve_file(workspace, source).is_file(), \
            {"stable_id": table["stable_id"], "source_path": source,
             "xml_chars": len(xml), "truncated": p.get("content_truncated"), "error": p.get("error")}

    run_check("get_entity_source",
              lambda: client.call_tool("get_entity_source", {"query": table["stable_id"], "max_chars": 20000}),
              verify_source)

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="APSGraph MCP functional/performance/correctness verification")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--samples", type=int, default=8, help="sample size per term category (raw_id and Chinese name)")
    parser.add_argument("--limit", type=int, default=20, help="search result limit for every timed call")
    parser.add_argument("--seed", type=int, help="random seed; default: current Unix time")
    parser.add_argument("--apsgraph-bin", default=None, help="use installed apsgraph command instead of repo source")
    parser.add_argument("--skip-cli", action="store_true", help="skip the apsgraph search CLI cold-start baseline")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    db = args.db.resolve()
    if not workspace.is_dir() or not db.is_file():
        raise SystemExit(f"workspace or db not found: {workspace} / {db}")
    rng = random.Random(args.seed if args.seed is not None else int(time.time()))
    seed = rng.randint(1, 2**31 - 1)
    rng = random.Random(seed)

    env = os.environ.copy()
    if args.apsgraph_bin:
        serve_cmd = [args.apsgraph_bin, "serve-mcp", "--db", str(db), "--workspace", str(workspace)]
        cli_cmd = [args.apsgraph_bin, "search", "--db", str(db), "--limit", str(args.limit)]
    else:
        env["PYTHONPATH"] = str(SRC)
        serve_cmd = [sys.executable, "-B", "-m", "apsgraph", "serve-mcp", "--db", str(db), "--workspace", str(workspace)]
        cli_cmd = [sys.executable, "-B", "-m", "apsgraph", "search", "--db", str(db), "--limit", str(args.limit)]

    samples = choose_samples(db, rng, args.samples)
    terms = [(row["raw_id"], row, "raw_id") for row in samples["raw_terms"]]
    terms += [(row["term"], row, "chinese_name") for row in samples["cjk_terms"]]
    report_path = args.report or Path(tempfile_report_name())

    payload: dict = {"workspace": str(workspace), "db": str(db), "seed": seed,
                     "search_limit": args.limit, "sample_meta": samples}
    client = McpClient(serve_cmd, env)
    exit_code = 0
    try:
        payload["functional"] = run_functional(client, samples, workspace)
        functional_failed = [c["label"] for c in payload["functional"] if not c["ok"]]

        # warm-up: page cache for MCP, CLI and rg so medians reflect steady state
        client.call_tool("search_metadata", {"query": samples["table"]["raw_id"], "limit": 5})
        if not args.skip_cli:
            subprocess.run(cli_cmd + [samples["table"]["raw_id"]], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=env, timeout=120)
        rg_files(workspace, samples["table"]["raw_id"])

        rows = []
        for term, meta, category in terms:
            started = time.perf_counter()
            mcp_payload = client.call_tool("search_metadata", {"query": term, "limit": args.limit})
            mcp_ms = ms(started)

            cli_ms = None
            cli_files = set()
            if not args.skip_cli:
                started = time.perf_counter()
                proc = subprocess.run(cli_cmd + [term], env=env, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
                cli_ms = ms(started)
                try:
                    cli_files = {str(resolve_file(workspace, row["file_path"]))
                                 for row in json.loads(proc.stdout).get("results", []) if row.get("file_path")}
                except (json.JSONDecodeError, KeyError):
                    pass

            started = time.perf_counter()
            rg = rg_files(workspace, term)
            rg_ms = ms(started)

            mcp_files = {str(resolve_file(workspace, row["file_path"]))
                         for row in mcp_payload["results"] if row.get("file_path")}
            missing = mcp_files - rg
            hard_missing = [row for row in mcp_payload["results"]
                            if row.get("matched_fields")
                            and str(resolve_file(workspace, row["file_path"])) not in rg]
            row_report = {
                "term": term, "category": category, "sample_kind": meta["kind"],
                "mcp_ms": mcp_ms, "cli_ms": cli_ms, "rg_ms": rg_ms,
                "mcp_result_count": mcp_payload["count"],
                "mcp_file_count": len(mcp_files), "cli_file_count": len(cli_files),
                "rg_file_count": len(rg),
                "mcp_files_missing_from_rg": sorted(missing),
                "hard_missing": [r["full_id"] for r in hard_missing],
                "rg_extra_files": len(rg - mcp_files),
                "cli_files_subset_of_rg": cli_files <= rg,
            }
            rows.append(row_report)

        hard_failures = [r for r in rows if r["hard_missing"]]
        cli_subset_failures = [r for r in rows if not r["cli_files_subset_of_rg"]]
        payload["correctness"] = {
            "rule": "MCP results whose matched_fields (id/full_id/longname/description) hit must appear in rg hit files; "
                    "results matching only via indexed file_path tokens are reported separately",
            "terms": len(rows),
            "hard_failures": hard_failures,
            "cli_files_not_in_rg": cli_subset_failures,
            "rg_extra_files_total": sum(r["rg_extra_files"] for r in rows),
            "rg_extra_files_expected_reason": "rg matches raw XML text including comments, Java code, "
                                              "non-metadata files; MCP only returns model nodes",
        }
        payload["performance"] = {
            "mcp_ms_median": statistics.median(r["mcp_ms"] for r in rows),
            "mcp_ms_avg": round(statistics.fmean(r["mcp_ms"] for r in rows), 2),
            "cli_ms_median": statistics.median(r["cli_ms"] for r in rows) if not args.skip_cli else None,
            "cli_ms_avg": round(statistics.fmean(r["cli_ms"] for r in rows), 2) if not args.skip_cli else None,
            "rg_ms_median": statistics.median(r["rg_ms"] for r in rows),
            "rg_ms_avg": round(statistics.fmean(r["rg_ms"] for r in rows), 2),
            "mcp_speedup_vs_cli_median": round(
                statistics.median(r["cli_ms"] for r in rows) / statistics.median(r["mcp_ms"] for r in rows), 2
            ) if not args.skip_cli else None,
            "mcp_speedup_vs_rg_median": round(
                statistics.median(r["rg_ms"] for r in rows) / statistics.median(r["mcp_ms"] for r in rows), 2
            ),
            "per_term": rows,
        }

        if functional_failed:
            payload["status"] = "FAIL"
            payload["error"] = f"functional checks failed: {functional_failed}"
            exit_code = 1
        elif hard_failures or cli_subset_failures:
            payload["status"] = "FAIL"
            payload["error"] = "correctness cross-validation failed"
            exit_code = 1
        else:
            payload["status"] = "PASS"
    except Exception as exc:
        payload["status"] = "FAIL"
        payload["error"] = str(exc)
        exit_code = 1
    finally:
        client.close()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: payload.get(key) for key in ("status", "error", "workspace", "seed")}
    summary["report"] = str(report_path)
    if payload.get("performance"):
        summary["performance"] = {k: v for k, v in payload["performance"].items() if k != "per_term"}
        summary["correctness_terms"] = payload["correctness"]["terms"]
        summary["hard_failures"] = len(payload["correctness"]["hard_failures"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


def tempfile_report_name() -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"apsgraph-mcp-verify-{int(time.time())}.json")


if __name__ == "__main__":
    raise SystemExit(main())
