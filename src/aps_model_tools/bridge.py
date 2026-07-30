from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .store import connect, find_nodes


_CONFIG_TYPE = re.compile(r'@(?:[\w$.]+\.)?ConfigType\s*\([^)]*value\s*=\s*"([^"]+)"', re.DOTALL)


def _resolve_one(conn: sqlite3.Connection, query: str) -> Dict[str, Any]:
    nodes = find_nodes(conn, query)
    if not nodes:
        raise ValueError(f"model not found: {query}")
    exact = [node for node in nodes if node["full_id"] == query]
    candidates = exact or nodes
    if len(candidates) != 1:
        raise ValueError(f"ambiguous model: {query}")
    return candidates[0]


def _outer_fqcn(node: Mapping[str, Any]) -> str:
    package = str(node["properties"].get("package") or "")
    if not package:
        # Nested nodes inherit package from their model file; caller fills it from model_files.
        package = str(node.get("package_name") or "")
    root = str(node["full_id"]).split(".", 1)[0]
    return f"{package}.{root}" if package else root


def _generated_candidates(node: Mapping[str, Any], package_name: str) -> List[Dict[str, str]]:
    full_id = str(node["full_id"])
    root, _, member = full_id.partition(".")
    outer = f"{package_name}.{root}" if package_name else root
    kind = node["kind"]
    result = [{"java_symbol": outer, "relation": "GENERATES_OUTER_TYPE"}]
    if kind == "TABLE" and member:
        result.extend([
            {"java_symbol": f"{outer}.{member}", "relation": "GENERATES_ENTITY"},
            {"java_symbol": f"{outer}.{member[0].upper() + member[1:]}Dao", "relation": "GENERATES_DAO"},
        ])
    elif kind in {"COMPLEX_TYPE", "DICTIONARY", "SERVICE_OPERATION", "NAMED_SQL"} and member:
        result.append({"java_symbol": f"{outer}.{member}", "relation": "GENERATES_NESTED_TYPE"})
    return result


def _generated_path(workspace: Path, file_path: str, package_name: str, root: str) -> Path:
    parts = Path(file_path).parts
    try:
        src = parts.index("src")
    except ValueError:
        return workspace / "__missing__"
    module = Path(*parts[:src])
    return workspace / module / "target/gen" / Path(*package_name.split(".")) / f"{root}.java"


def _verify_generated_links(path: Path, full_id: str, candidates: Iterable[Dict[str, str]]) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    config_values = set(_CONFIG_TYPE.findall(text))
    links = []
    for candidate in candidates:
        simple = candidate["java_symbol"].rsplit(".", 1)[-1]
        symbol_present = bool(re.search(rf'\b(?:class|interface|enum)\s+{re.escape(simple)}\b', text))
        explicit_model = full_id in config_values
        links.append({
            **candidate,
            "generated_file": str(path),
            "confidence": "CERTAIN" if explicit_model and symbol_present else "DERIVED" if symbol_present else "UNRESOLVED",
            "evidence": [value for value, ok in ((f"ConfigType:{full_id}", explicit_model), (f"symbol:{simple}", symbol_present)) if ok],
        })
    return links


def _codegraph_consumers(repository: str, db_path: Path, symbols: Iterable[str]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    coverage = {"repository": repository, "database": str(db_path)}
    if not db_path.is_file():
        coverage["status"] = "MISSING_INDEX"
        return coverage, []
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("pragma table_info(nodes)")}
        required = {"id", "kind", "name", "qualified_name", "file_path", "language",
                    "start_line", "end_line", "signature"}
        if not required.issubset(columns):
            coverage["status"] = "UNSUPPORTED_INDEX"
            return coverage, []
        consumers: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            rows = conn.execute(
                """select id,kind,name,qualified_name,file_path,language,start_line,end_line,signature
                   from nodes where language='java' and (name=? or name=? or signature like ?)
                   order by file_path,start_line,id""",
                (symbol, symbol.rsplit(".", 1)[-1], f"%{symbol}%"),
            ).fetchall()
            for row in rows:
                item = dict(row)
                item.update({"repository": repository, "java_symbol": symbol,
                             "confidence": "CERTAIN" if item["kind"] == "import" and symbol in (item.get("signature") or item["name"]) else "DERIVED"})
                consumers[item["id"]] = item
        coverage.update({"status": "INDEXED", "matched_consumers": len(consumers)})
        return coverage, list(consumers.values())
    finally:
        conn.close()


def build_bridge_report(aps_db: Path | str, workspace: Path | str, query: str,
                        codegraph_databases: Mapping[str, Path | str]) -> Dict[str, Any]:
    root = Path(workspace).resolve()
    conn = connect(aps_db, read_only=True)
    try:
        state = conn.execute("select workspace from scan_state where id=1").fetchone()
        if state and Path(state[0]).resolve() != root:
            raise ValueError(f"index belongs to another workspace: {state[0]}")
        node = _resolve_one(conn, query)
        file_row = conn.execute("select package_name from model_files where id=?", (node["file_id"],)).fetchone()
        package_name = str(file_row[0] or "")
    finally:
        conn.close()
    model_root = str(node["full_id"]).split(".", 1)[0]
    generated_file = _generated_path(root, node["file_path"], package_name, model_root)
    generated_links = _verify_generated_links(
        generated_file, str(node["full_id"]), _generated_candidates(node, package_name))
    symbols = [link["java_symbol"] for link in generated_links
               if link["confidence"] != "UNRESOLVED"
               and not ("." in str(node["full_id"]) and link["relation"] == "GENERATES_OUTER_TYPE")]
    coverage, consumers = [], []
    for repository, database in sorted(codegraph_databases.items()):
        status, found = _codegraph_consumers(repository, Path(database), symbols)
        coverage.append(status); consumers.extend(found)
    return {
        "target": {key: node[key] for key in ("stable_id", "full_id", "kind", "file_path")},
        "generated_links": generated_links,
        "code_consumers": sorted(consumers, key=lambda x: (x["repository"], x["file_path"], x.get("start_line") or 0, x["id"])),
        "coverage": {"repositories": coverage, "generated_file_exists": generated_file.is_file()},
        "limitations": [
            "CodeGraph target/gen is not required; generated Java is verified directly.",
            "CodeGraph databases are read-only and never modified.",
            "Dynamic reflection and runtime registration remain outside this report.",
        ],
    }
