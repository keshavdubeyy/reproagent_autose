"""Local chat-style web UI (stdlib only). See docs/product-flows.md.

  python -m reproagent.ui [--port 8765] [--dataset benchmarks/dataset.json] [--results results]
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .llm import DEFAULT_MODEL
from .session import SessionStore, fetch_github_issue, inspect_repo

STATIC = Path(__file__).parent / "static" / "index.html"
CFG: dict = {}
STORE: SessionStore | None = None


def load_dataset() -> list[dict]:
    p = Path(CFG["dataset"])
    if not p.exists():
        return []
    base = p.parent
    out = []
    for b in json.loads(p.read_text()):
        rp = base / b["report"]
        out.append({"id": b["id"], "repo": str((base / b["repo"]).resolve()),
                    "fixed_repo": str((base / b["fixed_repo"]).resolve()) if b.get("fixed_repo") else "",
                    "python": str((base / b["python"]).absolute()) if b.get("python") else "",
                    "gt_test": b.get("gt_test", ""), "report": rp.read_text() if rp.exists() else ""})
    return out


def list_results() -> list[dict]:
    rows = []
    for p in sorted(Path(CFG["results"]).rglob("*.json")):
        if p.name.startswith("eval_") or "sessions" in p.parts:
            continue
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict) or "bug_id" not in d:
            continue
        rows.append({"path": str(p.relative_to(CFG["results"])), "bug_id": d["bug_id"], "mode": d["mode"],
                     "status": d["status"], "fail_to_pass": d.get("fail_to_pass"), "fix_status": d["fix_status"],
                     "gt_pass": d.get("gt_pass"), "seconds": d["seconds"]})
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            body = STATIC.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if u.path == "/api/config":
            return self._json({"model": CFG["model"]})
        if u.path == "/api/bugs":
            return self._json(load_dataset())
        if u.path == "/api/results":
            return self._json(list_results())
        if u.path == "/api/result":
            root = Path(CFG["results"]).resolve()
            p = (root / q.get("path", [""])[0]).resolve()
            if not p.is_relative_to(root) or not p.is_file():
                return self._json({"error": "not found"}, 404)
            return self._json(json.loads(p.read_text()))
        if u.path == "/api/sessions":
            return self._json(STORE.list())
        m = re.fullmatch(r"/api/sessions/(S-[0-9A-F]{4})", u.path)
        if m:
            d = STORE.events(m[1], int(q.get("since", ["0"])[0]))
            return self._json(d) if d else self._json({"error": "no such session"}, 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            b = self._body()
            if path == "/api/inspect":
                return self._json(inspect_repo(b.get("path", ""), b.get("python") or None))
            if path == "/api/issue":
                return self._json(fetch_github_issue(b.get("url", "")))
            if path == "/api/sessions":
                ws = b.get("workspace") or {}
                if not ws.get("ok"):
                    return self._json({"error": "connect a healthy repository first"}, 400)
                s = STORE.create(workspace=ws, model=CFG["model"], bug_id=b.get("bug_id"),
                                 fixed_repo=b.get("fixed_repo") or None, gt_test=b.get("gt_test") or None)
                return self._json(s.summary())
            m = re.fullmatch(r"/api/sessions/(S-[0-9A-F]{4})/(report|confirm|fix|apply|discard)", path)
            if m:
                s = STORE.get(m[1])
                if not s:
                    return self._json({"error": "session is archived (read-only)"}, 409)
                action = m[2]
                if action == "report":
                    s.submit_report(b["text"])
                elif action == "confirm":
                    s.confirm(b.get("edited"))
                elif action == "fix":
                    s.fix(b.get("hint", ""))
                elif action == "apply":
                    return self._json(s.apply(bool(b.get("include_patch", True))))
                elif action == "discard":
                    s.discard()
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as e:  # report to the UI instead of dropping the connection
            self._json({"error": f"{e}"}, 400)


def main():
    global STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dataset", default="benchmarks/dataset.json")
    ap.add_argument("--results", default="results")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args()
    CFG.update(dataset=a.dataset, results=a.results, model=a.model)
    STORE = SessionStore(Path(a.results) / "sessions")
    print(f"ReproAgent on http://127.0.0.1:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
