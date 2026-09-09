#!/usr/bin/env python3
"""真实 v8.7-all APSGraph 验收测试。

该脚本不是 Python 单元测试：它构建临时索引，逐个调用公开 CLI，记录耗时，
并使用 ripgrep 对模型搜索/引用证据做原始 XML 交叉验证。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_WORKSPACE = Path("/Users/joshua/code/v8.7-all")


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int = 300) -> dict:
    started = time.perf_counter()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "apsgraph", *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    output = proc.stdout
    parsed = None
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        pass
    return {"args": args, "exit_code": proc.returncode, "elapsed_seconds": round(elapsed, 3), "output": output, "json": parsed}


def require(result: dict, label: str) -> None:
    if result["exit_code"] != 0:
        raise AssertionError(f"{label} failed: {result['output'][-2000:]}")


def rg_count(workspace: Path, pattern: str) -> int:
    proc = subprocess.run(
        ["rg", "-F", "-l", "--glob", "*.xml", pattern, str(workspace)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def choose_samples(db: Path) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    table = conn.execute("""
        select n.raw_id, n.full_id, n.stable_id, f.path
        from nodes n join model_files f on f.id=n.file_id
        where n.kind='TABLE' and n.owner_node_id is not null
        group by n.full_id having count(*)=1 order by length(n.raw_id), n.raw_id limit 1
    """).fetchone()
    unique_search = conn.execute("""
        select raw_id from nodes
        where raw_id is not null and length(raw_id)>=5
        group by raw_id having count(*)=1 order by length(raw_id), raw_id limit 1
    """).fetchone()
    ref = conn.execute("""
        select n.raw_id, n.full_id, n.stable_id, f.path, e.relation_kind, e.raw_target
        from nodes n join edges e on e.to_node_id=n.id join model_files f on f.id=n.file_id
        where n.raw_id is not null and e.raw_target is not null
        order by length(n.raw_id), n.raw_id limit 1
    """).fetchone()
    type_node = conn.execute("""
        select n.raw_id, n.full_id, n.stable_id, f.path
        from nodes n join model_files f on f.id=n.file_id
        where n.kind in ('RESTRICTION_TYPE','SUBENUM','DICTIONARY','COMPLEX_TYPE')
          and n.raw_id is not null
        group by n.raw_id having count(*)=1 order by n.kind, length(n.raw_id), n.raw_id limit 1
    """).fetchone()
    schema = conn.execute("""
        select n.raw_id, n.full_id, n.stable_id, f.path
        from nodes n join model_files f on f.id=n.file_id
        where n.kind='SCHEMA' and n.owner_node_id is null
        group by n.raw_id having count(*)=1 order by length(n.raw_id), n.raw_id limit 1
    """).fetchone()
    result = {
        "table": dict(table) if table else None,
        "search": unique_search[0] if unique_search else None,
        "ref": dict(ref) if ref else None,
        "type": dict(type_node) if type_node else None,
        "schema": dict(schema) if schema else None,
    }
    conn.close()
    if not all(result.values()):
        raise AssertionError(f"unable to choose real metadata samples: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="v8.7-all APSGraph real acceptance test")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace not found: {workspace}")
    temp_root = Path(tempfile.mkdtemp(prefix="apsgraph-v87-acceptance-"))
    db = temp_root / "apsgraph.db"
    report_path = args.report or (temp_root / "report.json")
    results: list[dict] = []
    try:
        scan = run_cmd(["scan", "--workspace", str(workspace), "--db", str(db)], timeout=600)
        require(scan, "scan")
        results.append(scan)
        stats = run_cmd(["stats", "--db", str(db)])
        require(stats, "stats")
        results.append(stats)
        if not isinstance(stats["json"], dict) or stats["json"].get("nodes", 0) < 100000:
            raise AssertionError(f"unexpected real graph size: {stats['json']}")
        samples = choose_samples(db)
        table = samples["table"]
        search_token = samples["search"]
        ref = samples["ref"]
        type_node = samples["type"]
        schema = samples["schema"]

        commands = [
            (["options", "--workspace", str(workspace)], "options"),
            (["status", "--workspace", str(workspace), "--db", str(db)], "status"),
            (["show", "--db", str(db), table["stable_id"]], "show"),
            (["search", "--db", str(db), "--limit", "10", search_token], "search_raw_id"),
            (["search", "--db", str(db), "--limit", "10", "acct"], "search_fuzzy"),
            (["refs", "--db", str(db), "--direction", "both", "--depth", "1", ref["stable_id"]], "refs"),
            (["impact", "--db", str(db), "--depth", "2", ref["stable_id"]], "impact"),
            (["ddl", "--db", str(db), "--dialect", "mysql", table["stable_id"]], "ddl"),
            (["ddl-gen", "--db", str(db), "--dialect", "mysql", "--tables", table["stable_id"]], "ddl_gen"),
            (["doc-export", "--db", str(db), "--type", "table", "--tables", table["stable_id"]], "doc_export"),
            (["xlsx-export", "--db", str(db), "--output-dir", str(temp_root / "xlsx"), "--types", "table", "--projects", "aggr-parent"], "xlsx_export"),
            (["classify", "--db", str(db), "--workspace", str(workspace), "--output", str(temp_root / "classify.md"), "--json-output", str(temp_root / "classify.json")], "classify"),
            (["bridge", "--db", str(db), "--workspace", str(workspace), schema["stable_id"]], "bridge"),
        ]
        for command, label in commands:
            result = run_cmd(command, timeout=600)
            require(result, label)
            results.append({"label": label, **result})

        # db-diff is tested separately below with a minimal valid offline schema.
        (temp_root / "empty-actual.json").write_text(json.dumps({"tables": []}), encoding="utf-8")
        dbdiff = run_cmd(["db-diff", "--db", str(db), "--dialect", "mysql", "--actual-json", str(temp_root / "empty-actual.json"), "--output-json", str(temp_root / "dbdiff.json")])
        # db-diff intentionally returns non-zero when the real metadata model has
        # incompatibilities; acceptance validates the report contract rather than
        # hiding those real findings.
        if dbdiff["exit_code"] not in (0, 1, 2) or not isinstance(dbdiff["json"], dict):
            raise AssertionError(f"db-diff contract failed: {dbdiff}")
        dbdiff["expected_nonzero_findings"] = dbdiff["exit_code"] == 2
        results.append({"label": "db_diff_offline", **dbdiff})

        # Raw XML cross-validation: search terms and reference targets must occur in XML,
        # and the graph's reference evidence must have a source file.
        cross = {
            "search_token": search_token,
            "search_xml_file_count": rg_count(workspace, search_token),
            "reference_raw_target": ref["raw_target"],
            "reference_target_xml_file_count": rg_count(workspace, ref["raw_target"]),
            "reference_evidence_file": ref["path"],
            "type_raw_id": type_node["raw_id"],
            "type_xml_file_count": rg_count(workspace, type_node["raw_id"]),
        }
        if cross["search_xml_file_count"] < 1 or cross["reference_target_xml_file_count"] < 1 or cross["type_xml_file_count"] < 1:
            raise AssertionError(f"ripgrep cross-validation failed: {cross}")

        payload = {
            "status": "PASS",
            "workspace": str(workspace),
            "db": str(db),
            "samples": samples,
            "cross_validation": cross,
            "commands": results,
            "stats": stats["json"],
            "temp_root": str(temp_root),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "PASS", "report": str(report_path), "temp_root": str(temp_root), "commands": len(results), "stats": stats["json"], "cross_validation": cross}, ensure_ascii=False, indent=2))
        if args.keep_db:
            print(f"kept temporary acceptance directory: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)
        return 0
    except Exception as exc:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"status": "FAIL", "error": str(exc), "commands": results, "temp_root": str(temp_root)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "FAIL", "error": str(exc), "report": str(report_path), "temp_root": str(temp_root)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
