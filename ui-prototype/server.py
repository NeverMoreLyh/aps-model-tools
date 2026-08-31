#!/usr/bin/env python3
"""APSGraph read-only UI prototype server; intentionally stdlib-only."""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def connect(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise FileNotFoundError(f"APSGraph SQLite not found: {db}")
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def props(row: sqlite3.Row):
    try:
        return json.loads(row["properties_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def node_dict(row: sqlite3.Row):
    item = dict(row)
    item["properties"] = props(row)
    item.pop("properties_json", None)
    return item


class APSGraphUIHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    db: Path
    workspace: Path


class Handler(BaseHTTPRequestHandler):
    server_version = "APSGraphUIPrototype/0.1"

    def json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self.json({"ok": True, "database": str(self.server.db)})
            elif parsed.path == "/api/stats":
                self.stats()
            elif parsed.path == "/api/kinds":
                self.kinds()
            elif parsed.path == "/api/models":
                self.models(parse_qs(parsed.query))
            elif parsed.path.startswith("/api/models/"):
                self.model(parsed.path.removeprefix("/api/models/"))
            elif parsed.path.startswith("/api/xml/"):
                self.xml(parsed.path.removeprefix("/api/xml/"))
            elif parsed.path == "/" or parsed.path == "/index.html":
                self.static("index.html")
            else:
                self.static(parsed.path.lstrip("/"))
        except FileNotFoundError as exc:
            self.json({"error": str(exc)}, 404)
        except (sqlite3.Error, ValueError) as exc:
            self.json({"error": str(exc)}, 400)

    def static(self, name):
        path = (STATIC / name).resolve()
        if STATIC not in path.parents or not path.is_file():
            self.json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def with_db(self):
        return connect(self.server.db)

    def stats(self):
        with self.with_db() as db:
            self.json({
                "files": db.execute("select count(*) from model_files").fetchone()[0],
                "parsed": db.execute("select count(*) from model_files where parse_status='PARSED'").fetchone()[0],
                "nodes": db.execute("select count(*) from nodes").fetchone()[0],
                "edges": db.execute("select count(*) from edges").fetchone()[0],
                "unresolved": db.execute("select count(*) from edges where to_node_id is null and raw_target is not null").fetchone()[0],
            })

    def kinds(self):
        with self.with_db() as db:
            rows = db.execute("select kind, count(*) as count from nodes group by kind order by count desc, kind").fetchall()
            self.json([dict(row) for row in rows])

    def models(self, query):
        q = (query.get("q") or [""])[0].strip()
        kind = (query.get("kind") or [""])[0].strip()
        limit = min(int((query.get("limit") or [100])[0]), 500)
        where, params = [], []
        if q:
            where.append("(n.full_id like ? or n.raw_id like ? or n.stable_id like ? or f.path like ?)")
            like = f"%{q}%"
            params.extend([like, like, like, like])
        if kind:
            where.append("n.kind=?")
            params.append(kind)
        clause = " where " + " and ".join(where) if where else ""
        sql = ("select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.owner_node_id,"
               "f.path as file_path,n.file_id,n.xml_tag,n.properties_json "
               "from nodes n join model_files f on f.id=n.file_id" + clause +
               " order by n.kind,n.full_id,f.path limit ?")
        params.append(limit)
        with self.with_db() as db:
            self.json([node_dict(row) for row in db.execute(sql, params).fetchall()])

    def model(self, stable_id):
        with self.with_db() as db:
            row = db.execute(
                "select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.owner_node_id,"
                "f.path as file_path,n.file_id,n.xml_tag,n.properties_json "
                "from nodes n join model_files f on f.id=n.file_id where n.stable_id=?",
                (stable_id,),
            ).fetchone()
            if not row:
                self.json({"error": "model not found"}, 404)
                return
            item = node_dict(row)
            children = db.execute(
                "select n.id,n.stable_id,n.kind,n.raw_id,n.full_id,n.owner_node_id,"
                "f.path as file_path,n.file_id,n.xml_tag,n.properties_json "
                "from nodes n join model_files f on f.id=n.file_id where n.owner_node_id=? order by n.kind,n.full_id",
                (row["id"],),
            ).fetchall()
            edges = db.execute(
                "select e.id,src.stable_id as from_id,dst.stable_id as to_id,e.raw_target,"
                "e.relation_kind,e.evidence_value,e.confidence,ef.path as evidence_path "
                "from edges e join nodes src on src.id=e.from_node_id left join nodes dst on dst.id=e.to_node_id "
                "join model_files ef on ef.id=e.evidence_file_id where e.from_node_id=? or e.to_node_id=? order by e.relation_kind,e.id",
                (row["id"], row["id"]),
            ).fetchall()
            item["children"] = [node_dict(child) for child in children]
            item["edges"] = [dict(edge) for edge in edges]
            self.json(item)

    def xml(self, stable_id):
        with self.with_db() as db:
            row = db.execute(
                "select f.path,n.full_id,n.xml_tag from nodes n join model_files f on f.id=n.file_id where n.stable_id=?",
                (stable_id,),
            ).fetchone()
            if not row:
                self.json({"error": "model not found"}, 404)
                return
            path_text = row["path"]
            if path_text.startswith("jar:") or path_text.startswith("external-db:"):
                self.json({"available": False, "reason": "logical archive/external index path; source XML is not local"})
                return
            path = Path(path_text)
            if not path.is_absolute():
                path = (self.server.workspace / path).resolve()
            else:
                path = path.resolve()
            workspace = self.server.workspace.resolve()
            if workspace not in path.parents and path != workspace:
                self.json({"available": False, "reason": "XML path is outside configured workspace"})
                return
            if not path.is_file():
                self.json({"available": False, "reason": "source XML file is not available", "path": str(path)})
                return
            text = path.read_text(encoding="utf-8", errors="replace")
            self.json({"available": True, "path": str(path), "full_id": row["full_id"], "xml_tag": row["xml_tag"], "content": text})


def main():
    parser = argparse.ArgumentParser(description="APSGraph read-only UI prototype")
    parser.add_argument("--db", type=Path, default=Path(".apsgraph/apsgraph.db"))
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    httpd = APSGraphUIHTTPServer((args.host, args.port), Handler)
    httpd.db = args.db
    httpd.workspace = args.workspace
    print(f"APSGraph UI prototype: http://{args.host}:{args.port}")
    print(f"read-only database: {args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
