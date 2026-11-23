"""ReproAgent Runner: write a bug report like a prompt, press Run, watch the agent work step by step.

    .venv-autose/bin/python scripts/runner_ui.py          # then open http://127.0.0.1:8766

Targets: a ShopCart demo bug (local, no Docker, fastest), any SWE-bench Lite bug with a code snapshot
(Docker; the first run pulls the image), or any Python project folder on this machine. The bug report
is prefilled from the benchmark but is yours to rewrite. One job at a time; each job is saved under
runs/ui/<job>/ (job.json, log.txt, result.json, the agent's report).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from live_dashboard import detail_from_path  # noqa: E402

UI_RUNS = ROOT / "runs" / "ui"
SHOP = ROOT / "benchmarks" / "shopcart"
PUBLIC = ROOT / "benchmarks" / "swe_bench_lite" / "public"
PY = ROOT / ".venv-autose" / "bin" / "python"
# The React page (ui/, built into ui/dist). When it is not built, the inline PAGE below is served instead.
DIST = ROOT / "ui" / "dist"
ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
               ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json"}


def dist_asset(path: str) -> Path | None:
    """A file under ui/dist/assets for a URL path like /assets/x.js, or None (no traversal outside it)."""
    base = (DIST / "assets").resolve()
    f = (DIST / unquote(path).lstrip("/")).resolve()
    return f if base in f.parents and f.is_file() and f.suffix in ASSET_TYPES else None


class Job:
    def __init__(self, jid: str, spec: dict):
        self.id, self.spec = jid, spec
        self.dir = UI_RUNS / jid
        self.lines: list[tuple[float, str]] = []
        self.started = time.time()
        self.proc: subprocess.Popen | None = None
        self.status = "running"

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "job.json").write_text(json.dumps(self.spec, indent=2), encoding="utf-8")
        self.proc = subprocess.Popen([str(PY if PY.exists() else sys.executable), "-u",
                                      str(ROOT / "scripts" / "runner_worker.py"), str(self.dir / "job.json")],
                                     cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     bufsize=1, env={**__import__("os").environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        with (self.dir / "log.txt").open("a", encoding="utf-8") as log:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self.lines.append((time.time(), line))
                log.write(line + "\n")
                log.flush()
        rc = self.proc.wait()
        if self.status == "running":
            self.status = "done" if rc == 0 else ("stopped" if rc in (143, -15) else "failed")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.status = "stopped"
            self.proc.terminate()


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()


def active() -> Job | None:
    return next((j for j in JOBS.values() if j.status == "running"), None)


def night_running() -> bool:
    try:
        return bool(subprocess.run(["pgrep", "-f", "autose.evaluation.night"], capture_output=True,
                                   text=True, timeout=3).stdout.strip())
    except Exception:
        return False


_OPTS: dict | None = None


def options() -> dict:
    global _OPTS
    if _OPTS is None:
        shop = []
        if (SHOP / "bugs.json").exists():
            for b in json.loads((SHOP / "bugs.json").read_text())["bugs"]:
                if (SHOP / "work" / b["id"] / "buggy").exists():
                    shop.append({"id": b["id"], "title": b["title"], "body": b["body"],
                                 "repo": str(SHOP / "work" / b["id"] / "buggy"),
                                 "fixed": str(SHOP / "work" / b["id"] / "fixed")})
        swe = []
        if (PUBLIC / "bug_reports.json").exists():
            from autose.run import to_report
            for r in json.loads((PUBLIC / "bug_reports.json").read_text()):
                if (PUBLIC / "code" / r["instance_id"]).exists():
                    rep = to_report(r, "full")
                    swe.append({"id": r["instance_id"], "repo": r["repo"], "title": rep.title, "body": rep.body})
        _OPTS = {"shopcart": shop, "swebench": swe}
    images = set()
    try:
        out = subprocess.run(["docker", "images", "--format", "{{.Repository}}"], capture_output=True,
                             text=True, timeout=5).stdout
        images = {l.split("/")[-1] for l in out.split()}
    except Exception:
        pass
    swe = [{**b, "image_ready": any(b["id"].replace("__", "_1776_") in i for i in images)} for b in _OPTS["swebench"]]
    return {"shopcart": _OPTS["shopcart"], "swebench": swe, "night_running": night_running(),
            "active": active().id if active() else None, "root": str(ROOT)}


def history() -> list[dict]:
    rows = []
    if not UI_RUNS.exists():
        return rows
    for d in sorted(UI_RUNS.iterdir(), reverse=True)[:30]:
        try:
            spec = json.loads((d / "job.json").read_text())
        except Exception:
            continue
        res = json.loads((d / "result.json").read_text()) if (d / "result.json").exists() else {}
        j = JOBS.get(d.name)
        rows.append({"id": d.name, "title": spec.get("title"), "target": spec.get("target"),
                     "bug_id": spec.get("bug_id"), "config": spec.get("config"),
                     "status": j.status if j else ("done" if res else "interrupted"),
                     "verdict": res.get("verdict"), "fix_status": res.get("fix_status"),
                     "resolved": res.get("resolved"), "error": res.get("error"), "wall_s": res.get("wall_s")})
    return rows


def job_state(jid: str, frm: int) -> dict:
    j = JOBS.get(jid)
    d = UI_RUNS / jid
    if j:
        lines, status, started = j.lines, j.status, j.started
    elif d.exists():
        lines = [(0.0, l) for l in (d / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()] \
            if (d / "log.txt").exists() else []
        status, started = ("done" if (d / "result.json").exists() else "interrupted"), 0.0
    else:
        return {"error": "no such job"}
    out = {"id": jid, "status": status, "started": started, "now": time.time(), "root": str(ROOT),
           "spec": json.loads((d / "job.json").read_text()) if (d / "job.json").exists() else {},
           "total": len(lines), "lines": [{"t": t, "s": s} for t, s in lines[frm:]]}
    if status != "running" and (d / "result.json").exists():
        res = json.loads((d / "result.json").read_text())
        out["result"] = res
        if res.get("report_file"):
            out["detail"] = detail_from_path(Path(res["report_file"]))
    return out


def new_job(body: dict) -> tuple[int, dict]:
    with LOCK:
        if active():
            return 409, {"error": "a run is already in progress; stop it first"}
        target = body.get("target")
        title, text = (body.get("title") or "").strip(), (body.get("body") or "").strip()
        if not title or not text:
            return 400, {"error": "write a title and a description for the bug"}
        if body.get("config") not in ("A", "C", "D", "E"):
            return 400, {"error": "unknown configuration"}
        jid = time.strftime("%Y%m%d-%H%M%S")
        spec = {"id": jid, "target": target, "title": title, "body": text, "config": body["config"],
                "fix": bool(body.get("fix", True)), "grade": bool(body.get("grade")), "created": time.time()}
        if target == "shopcart":
            b = next((b for b in options()["shopcart"] if b["id"] == body.get("bug_id")), None)
            if not b:
                return 400, {"error": "pick a ShopCart bug"}
            spec.update(bug_id=b["id"], repo=b["repo"], fixed=b["fixed"])
        elif target == "swebench":
            iid = body.get("bug_id") or ""
            if not re.fullmatch(r"[\w.-]+", iid) or not (PUBLIC / "code" / iid).exists():
                return 400, {"error": "pick a SWE-bench bug that has a code snapshot"}
            spec.update(bug_id=iid)
        elif target == "folder":
            repo = Path(body.get("repo") or "").expanduser()
            if not repo.is_dir():
                return 400, {"error": f"folder not found: {repo}"}
            fixed = (body.get("fixed") or "").strip()
            if fixed and not Path(fixed).expanduser().is_dir():
                return 400, {"error": f"fixed-version folder not found: {fixed}"}
            spec.update(repo=str(repo), fixed=fixed or None, bug_id=re.sub(r"\W+", "_", repo.name) or "project")
        else:
            return 400, {"error": "unknown target"}
        j = Job(jid, spec)
        JOBS[jid] = j
        j.start()
        return 200, {"id": jid}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                if (DIST / "index.html").is_file():
                    return self.send((DIST / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
                return self.send(PAGE, ctype="text/html; charset=utf-8")
            if u.path.startswith("/assets/"):
                f = dist_asset(u.path)
                if f:
                    return self.send(f.read_bytes(), ctype=ASSET_TYPES[f.suffix])
            if u.path == "/api/options":
                return self.send(options())
            if u.path == "/api/history":
                return self.send(history())
            m = re.fullmatch(r"/api/job/([\w-]+)", u.path)
            if m:
                return self.send(job_state(m.group(1), int(q.get("from", ["0"])[0])))
        except Exception as exc:
            return self.send({"error": repr(exc)}, 500)
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.send({"error": "bad json"}, 400)
        if u.path == "/api/run":
            code, res = new_job(body)
            return self.send(res, code)
        m = re.fullmatch(r"/api/job/([\w-]+)/stop", u.path)
        if m and m.group(1) in JOBS:
            JOBS[m.group(1)].stop()
            return self.send({"ok": True})
        self.send({"error": "not found"}, 404)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReproAgent Runner</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Libre+Caslon+Display&family=Libre+Caslon+Text:ital,wght@0,400;0,700;1,400&family=Red+Hat+Mono:wght@400;500&display=swap">
<style>
/* Paper-and-ink tokens. Light is the default; "night paper" is the dark variant with the same names. */
:root{--canvas:#fef9ed;--paper:#f7f1e3;--action:#f7ecd9;--peach-s:#fbd3be;--peach:#c4673d;--ink:#5d524b;--ink-2:#3f3732;--muted:#766960;--line:#e7dcc6;--code:#f3ead7;
--ok:#346846;--ok-s:#e2ecd5;--warn:#7a5710;--warn-s:#f7e7c1;--bad:#a5432f;--bad-s:#f8dcd2;--btn:#5d524b;--btn-fg:#f7ecd9;--shadow:rgba(93,82,75,.28);
--serif:"Libre Caslon Text",Georgia,serif;--disp:"Libre Caslon Display","Libre Caslon Text",Georgia,serif;--mono:"Red Hat Mono",ui-monospace,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--canvas:#1d1814;--paper:#28211c;--action:#352b24;--peach-s:#4b3226;--peach:#eaa883;--ink:#f1e6d4;--ink-2:#fbf3e4;--muted:#bcab98;--line:#3e342c;--code:#231c17;
--ok:#9fd2a6;--ok-s:#22321f;--warn:#e6c27a;--warn-s:#3a2f1c;--bad:#f2a08e;--bad-s:#40231d;--btn:#f1e6d4;--btn-fg:#1d1814;--shadow:rgba(0,0,0,.5);color-scheme:dark}}
:root[data-theme="dark"]{--canvas:#1d1814;--paper:#28211c;--action:#352b24;--peach-s:#4b3226;--peach:#eaa883;--ink:#f1e6d4;--ink-2:#fbf3e4;--muted:#bcab98;--line:#3e342c;--code:#231c17;
--ok:#9fd2a6;--ok-s:#22321f;--warn:#e6c27a;--warn-s:#3a2f1c;--bad:#f2a08e;--bad-s:#40231d;--btn:#f1e6d4;--btn-fg:#1d1814;--shadow:rgba(0,0,0,.5);color-scheme:dark}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{margin:0;background:var(--canvas);color:var(--ink)}
body{font:16px/1.55 var(--serif);letter-spacing:-.005em;-webkit-font-smoothing:antialiased}
.mono,code,kbd{font-family:var(--mono)}code{font-size:.88em;background:var(--code);padding:1px 5px;border-radius:3px;overflow-wrap:anywhere}
kbd{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:1px 5px;background:var(--canvas)}
.lbl{font:500 13px/1.4 var(--mono);letter-spacing:.04em;color:var(--muted)}
.muted{color:var(--muted)}
a{color:inherit}

/* Shell: the sidebar is sticky inside the grid (align-self:start keeps it from stretching). */
.app{display:grid;grid-template-columns:280px minmax(0,1fr);min-height:100vh}
.side{position:sticky;top:0;align-self:start;height:100vh;height:100dvh;overflow:auto;background:var(--paper);padding:22px 14px 16px;display:flex;flex-direction:column;gap:14px;z-index:30}
.brand{font:400 22px/1.1 var(--disp);letter-spacing:-.02em;padding:0 8px;color:var(--ink-2)}.brand i{color:var(--peach)}
.side .btn{width:100%}
.hist{display:grid;gap:2px;flex:1;align-content:start}
.hitem{all:unset;box-sizing:border-box;display:grid;gap:5px;padding:10px;border-radius:4px;cursor:pointer;min-width:0}
.hitem:hover{background:var(--action)}.hitem.on{background:var(--canvas)}
.hitem:focus-visible{outline:2px solid var(--peach)}
.ht{font:15px/1.3 var(--serif);color:var(--ink-2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hm{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;font:12px/1.3 var(--mono);color:var(--muted)}
.sidefoot{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:0 6px}
.topbar{display:none}
.scrim{display:none}
@media (max-width:860px){
 .app{grid-template-columns:minmax(0,1fr)}
 .side{position:fixed;inset:0 auto 0 0;width:min(300px,86vw);transform:translateX(-102%);transition:transform .25s ease;visibility:hidden}
 .app.open .side{transform:none;visibility:visible;box-shadow:0 4px 80px var(--shadow)}
 .app.open .scrim{display:block;position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:20}
 .topbar{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 16px;background:var(--canvas);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
}

/* Controls */
button{font:inherit;color:inherit}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:500 14px/1 var(--mono);letter-spacing:-.015em;background:var(--action);color:var(--ink);border:0;border-radius:28px;padding:13px 26px;cursor:pointer;white-space:nowrap;text-decoration:none}
.btn:hover{filter:brightness(.97)}
.btn.primary{background:var(--btn);color:var(--btn-fg)}
.btn.small{padding:9px 16px;font-size:13px}
.btn.ghost{background:transparent;border:1px solid var(--line)}
.btn.stop{background:var(--bad-s);color:var(--bad)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn:focus-visible,.choice:focus-visible{outline:2px solid var(--peach);outline-offset:2px}
input,select,textarea{font:16px/1.45 var(--serif);color:var(--ink-2);background:var(--canvas);border:1px solid var(--line);border-radius:4px;padding:10px 12px;width:100%;min-width:0}
select{font-family:var(--mono);font-size:14px}
textarea{min-height:170px;resize:vertical}
input:focus,select:focus,textarea:focus{outline:2px solid var(--peach);outline-offset:-1px}
label.f{display:grid;gap:6px}
.hint{font-size:14px;color:var(--muted)}
.chk{display:flex;gap:10px;align-items:flex-start;font-size:15px;cursor:pointer}.chk input{width:auto;margin-top:5px;accent-color:var(--ink)}

/* Page */
main{min-width:0;padding:36px 32px 120px}
.col{max-width:880px;margin:0 auto;display:grid;gap:22px;min-width:0}
@media (max-width:600px){main{padding:22px 16px 90px}.col{gap:18px}}
h1{font:400 clamp(30px,5vw,40px)/1.12 var(--disp);letter-spacing:-.025em;margin:0;color:var(--ink-2)}
h1 em{font-family:var(--serif);font-style:italic;font-size:.92em}
h2{font:400 26px/1.2 var(--disp);letter-spacing:-.02em;margin:0;color:var(--ink-2)}
h3{font:400 20px/1.25 var(--disp);margin:0;color:var(--ink-2)}
.lead{font-size:18px;margin:6px 0 0;max-width:640px}
.card{background:var(--paper);border-radius:4px;padding:24px;min-width:0}
@media (max-width:600px){.card{padding:18px 16px}}
.how{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin:0;padding:0;list-style:none}
.how li{display:grid;gap:2px;font-size:15px}.how b{font:500 13px var(--mono);letter-spacing:.04em;color:var(--peach)}
@media (max-width:700px){.how{grid-template-columns:1fr;gap:10px}}
.note{background:var(--warn-s);color:var(--ink-2);border-radius:4px;padding:12px 14px;font-size:15px}
.errbox{background:var(--bad-s);color:var(--ink-2);border-radius:4px;padding:12px 14px;font-size:15px}

/* New-run form */
.composer{display:grid;gap:26px}
.step{display:grid;gap:12px}
.steph{display:flex;align-items:baseline;gap:10px}.steph .n{font:500 13px var(--mono);color:var(--peach)}
.choices{display:grid;gap:10px;grid-template-columns:repeat(3,minmax(0,1fr))}
.choices.cfg{grid-template-columns:repeat(2,minmax(0,1fr))}
@media (max-width:700px){.choices,.choices.cfg{grid-template-columns:minmax(0,1fr)}}
.choice{all:unset;box-sizing:border-box;cursor:pointer;display:grid;gap:4px;align-content:start;background:var(--canvas);border:1px solid var(--line);border-radius:4px;padding:14px 16px;min-width:0}
.choice:hover{border-color:var(--muted)}
.choice.on{border-color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink);background:var(--canvas)}
.choice .cn{font:400 18px/1.2 var(--disp);color:var(--ink-2);display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.choice .cd{font-size:14px;line-height:1.4}
.choice .ct{font:12px/1.3 var(--mono);color:var(--muted)}
.rec{font:500 11px var(--mono);background:var(--peach-s);color:var(--ink-2);border-radius:20px;padding:3px 8px;white-space:nowrap}
details.adv>summary,details.more>summary{cursor:pointer;font:500 13px var(--mono);color:var(--muted);list-style:none;display:inline-flex;gap:6px;align-items:center}
details.adv>summary::before,details.more>summary::before{content:"+";display:inline-block;width:14px}
details[open].adv>summary::before,details[open].more>summary::before{content:"\2212"}
details.adv>summary::-webkit-details-marker,details.more>summary::-webkit-details-marker{display:none}
.advbody{display:grid;gap:12px;margin-top:12px;padding-left:20px}
.legend{font:12px/1.5 var(--mono);color:var(--muted);margin:0;padding:0;list-style:none}
.actions{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
.actions .hint{flex:1 1 240px}

/* Run view */
.runtop{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.switch{display:inline-flex;gap:8px;align-items:center;font:500 13px var(--mono);color:var(--muted);cursor:pointer}
.switch input{width:auto;accent-color:var(--ink)}
.rhead{display:grid;gap:6px}
.status{display:grid;gap:18px}
.nowline{font:400 clamp(22px,3.4vw,28px)/1.22 var(--disp);letter-spacing:-.015em;margin:0;color:var(--ink-2)}
.nowline em{font-family:var(--serif);font-style:italic;font-size:.82em;color:var(--ink)}
.nowsub{margin:0;font-size:17px}
.nowfoot{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.pulse{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--peach);margin-right:8px;vertical-align:1px;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
.status.running{background:linear-gradient(135deg,var(--peach-s) 0%,var(--paper) 62%)}

/* Stage tracker */
.track{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px;position:relative}
.st{display:grid;gap:8px;justify-items:center;text-align:center;position:relative;min-width:0}
.st::before{content:"";position:absolute;top:13px;left:calc(-50% + 16px);right:calc(50% + 16px);height:1px;background:var(--line)}
.st:first-child::before{display:none}
.st.done::before,.st.active::before,.st.fail::before{background:var(--muted)}
.sn{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font:500 13px var(--mono);background:var(--canvas);color:var(--muted);border:1px solid var(--line);position:relative;z-index:1}
.sl{font:500 12px/1.3 var(--mono);color:var(--muted);opacity:.7;position:relative;padding:2px 4px}
.st.done .sn{background:var(--ink);color:var(--canvas);border-color:var(--ink)}
.st.done .sl{color:var(--ink);opacity:1}
.st.active .sn{background:var(--peach-s);color:var(--ink-2);border-color:var(--peach)}
.st.active .sl{color:var(--ink-2);opacity:1}
.st.fail .sn{background:var(--bad-s);color:var(--bad);border-color:var(--bad)}.st.fail .sl{color:var(--bad);opacity:1}
.st.halt .sn{border-style:dashed}.st.halt .sl{opacity:1}
.st.skip .sl{text-decoration:line-through;text-decoration-color:var(--line)}
.scribble{position:absolute;inset:-7px -9px;width:calc(100% + 18px);height:calc(100% + 14px);color:var(--peach);pointer-events:none;overflow:visible}
.trackcap{display:none;font:500 13px var(--mono);color:var(--ink);text-align:center}
@media (max-width:640px){.sl{display:none}.trackcap{display:block}.st::before{left:calc(-50% + 14px);right:calc(50% + 14px)}}

/* Verdict */
.vhead{font:400 clamp(26px,4vw,34px)/1.15 var(--disp);letter-spacing:-.02em;margin:0;color:var(--ink-2)}
.vsum{margin:0;font-size:17px}
.checks{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.checks li{display:grid;grid-template-columns:28px minmax(0,1fr);gap:12px;align-items:start}
.ck{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font:600 14px var(--mono)}
.ck.ok{background:var(--ok-s);color:var(--ok)}.ck.bad{background:var(--bad-s);color:var(--bad)}.ck.warn{background:var(--warn-s);color:var(--warn)}.ck.skip{background:var(--canvas);color:var(--muted);border:1px solid var(--line)}
.checks b{font-weight:700;display:block;color:var(--ink-2)}.checks span{font-size:15px}
.tone-ok .vhead{color:var(--ok)}.tone-bad .vhead{color:var(--bad)}.tone-warn .vhead{color:var(--warn)}
.stats{display:flex;flex-wrap:wrap;gap:6px 18px}
.badge{display:inline-block;font:500 12px/1.3 var(--mono);padding:3px 9px;border-radius:20px;background:var(--canvas);color:var(--muted);white-space:nowrap}
.badge.ok{background:var(--ok-s);color:var(--ok)}.badge.bad{background:var(--bad-s);color:var(--bad)}.badge.warn{background:var(--warn-s);color:var(--warn)}.badge.run{background:var(--peach-s);color:var(--ink-2)}

/* Code, diffs, logs */
pre{margin:0;background:var(--code);color:var(--ink-2);border-radius:4px;padding:12px 14px;font:13px/1.6 var(--mono);overflow:auto;white-space:pre;max-height:420px}
.diff{border-radius:4px;overflow:hidden;background:var(--code)}
.dfile{font:500 13px var(--mono);padding:8px 14px;background:var(--action);color:var(--ink-2)}
.diff pre{border-radius:0;padding:8px 0}
.dl{display:block;padding:0 14px;min-width:max-content}
.dl.add{background:var(--ok-s);color:var(--ok)}.dl.del{background:var(--bad-s);color:var(--bad)}.dl.hunk{color:var(--muted)}
.raw{font:12.5px/1.6 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;background:var(--code);color:var(--ink-2);border-radius:4px;padding:12px 14px;max-height:380px;overflow:auto}

/* Timeline */
.tl{list-style:none;margin:0;padding:0;display:grid}
.grp{font:500 13px var(--mono);letter-spacing:.04em;color:var(--muted);padding:16px 0 8px 44px}
.grp:first-child{padding-top:0}
.ev{display:grid;grid-template-columns:30px minmax(0,1fr);gap:14px;padding:0 0 12px;position:relative}
.ev::before{content:"";position:absolute;left:14.5px;top:30px;bottom:0;width:1px;background:var(--line)}
.dot{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font:600 13px var(--mono);background:var(--paper);color:var(--muted)}
.ev.ok .dot{background:var(--ok-s);color:var(--ok)}.ev.bad .dot{background:var(--bad-s);color:var(--bad)}.ev.warn .dot{background:var(--warn-s);color:var(--warn)}
.evb{background:var(--paper);border-radius:4px;padding:12px 16px;min-width:0}
.evh{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.evt{font-weight:700;color:var(--ink-2)}
.evtime{font:12px var(--mono);color:var(--muted);white-space:nowrap}
.evd{font-size:15px;margin-top:4px;overflow-wrap:anywhere}
.tech{display:none!important}
.tech-on .tech{display:block!important}
.tech-on .tech.chips{display:flex!important}
.tech-on span.tech,.tech-on code.tech{display:inline!important}
.techline{margin-top:8px;font:12px/1.5 var(--mono);color:var(--muted);overflow-wrap:anywhere;white-space:pre-wrap}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.chip{font:12px var(--mono);background:var(--canvas);border-radius:3px;padding:2px 7px;overflow-wrap:anywhere}
.waiting{display:flex;gap:10px;align-items:center;color:var(--muted);font:14px var(--mono);padding:2px 0 0 44px}

/* Tabs (behind the scenes) */
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{font:500 13px var(--mono);background:var(--canvas);border:0;border-radius:20px;padding:8px 14px;cursor:pointer;color:var(--ink)}
.tab.on{background:var(--btn);color:var(--btn-fg)}
.att{background:var(--canvas);border-radius:4px;padding:10px 14px;margin-bottom:8px}
.att>summary{cursor:pointer;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.att[open]>summary{margin-bottom:8px}
.kv{display:grid;grid-template-columns:150px minmax(0,1fr);gap:6px 14px;font-size:15px}
.kv dt{font:500 13px/1.6 var(--mono);color:var(--muted)}.kv dd{margin:0}
@media (max-width:600px){.kv{grid-template-columns:minmax(0,1fr)}.kv dd{margin-bottom:8px}}
.empty{color:var(--muted);text-align:center;padding:24px 0}
</style></head><body>
<div class="app" id="app">
<aside class="side" id="side" aria-label="Runs">
  <div class="brand">Repro<i>Agent</i> Runner</div>
  <button class="btn primary" onclick="newRun()">+ New run</button>
  <div class="lbl" style="padding:4px 8px 0">Past runs</div>
  <div class="hist" id="hist"></div>
  <div class="sidefoot"><span class="lbl">Theme</span><button class="btn small ghost" id="themeBtn" onclick="cycleTheme()">Auto</button></div>
</aside>
<div class="scrim" onclick="toggleSide(false)"></div>
<main>
  <div class="topbar"><button class="btn small" onclick="toggleSide(true)" aria-label="Show past runs">&#9776; Runs</button><span class="brand" style="font-size:19px">Repro<i>Agent</i></span><button class="btn small primary" onclick="newRun()">+ New</button></div>
  <div class="col">
  <div id="banner"></div>

  <!-- Mode A: new run -->
  <section id="newView" class="col" style="padding:0;max-width:none">
    <header>
      <div class="lbl">A small AI model running on this computer (qwen3:8b)</div>
      <h1 style="margin-top:8px"><em>Describe</em> a bug. Watch the agent prove it, then fix it.</h1>
      <p class="lead">You write a bug report the way a user would. The agent reads it, finds the code, and works step by step. You can follow every step live.</p>
    </header>
    <ol class="how">
      <li><b>1 · Prove it</b>It writes a small test that fails because of the bug.</li>
      <li><b>2 · Find it</b>It ranks the functions most likely to hold the mistake.</li>
      <li><b>3 · Fix it</b>It edits the code and checks the fix with the project's tests.</li>
    </ol>
    <div class="card composer" id="composer">
      <div class="step">
        <div class="steph"><span class="n">1</span><h3>Which code should it work on?</h3></div>
        <div class="choices" id="targets"></div>
        <div id="pick"></div>
      </div>
      <div class="step">
        <div class="steph"><span class="n">2</span><h3>Describe the bug</h3></div>
        <label class="f"><span class="lbl">Title: one line, what is wrong?</span><input id="title" placeholder="e.g. Discount is applied to small orders"></label>
        <label class="f"><span class="lbl">Report: what you did, what you expected, what happened instead</span>
          <textarea id="body" placeholder="Describe the bug like a user would report it…"></textarea></label>
      </div>
      <div class="step">
        <div class="steph"><span class="n">3</span><h3>Agent setup</h3></div>
        <div class="choices cfg" id="cfgs"></div>
        <details class="adv"><summary>Advanced options</summary>
          <div class="advbody">
            <label class="chk"><input type="checkbox" id="fix" checked><span>Also try to write a fix <span class="hint">(turn off to only reproduce the bug)</span></span></label>
            <label class="chk" id="gradeWrap" hidden><input type="checkbox" id="grade" checked><span>Grade the result against the developers' real fix and the benchmark's hidden tests <span class="hint">(the agent never sees these; adds a few minutes)</span></span></label>
            <ul class="legend" id="cfgLegend"></ul>
          </div>
        </details>
      </div>
      <div class="actions">
        <div class="hint" id="eta"></div>
        <button class="btn primary" id="runBtn" onclick="run()">Run agent &rarr;</button>
      </div>
      <div class="hint" style="margin-top:-14px;text-align:right"><kbd>&#8984;</kbd>/<kbd>Ctrl</kbd> + <kbd>Enter</kbd> also runs it</div>
      <div id="formErr"></div>
    </div>
  </section>

  <!-- Mode B: one run -->
  <section id="runView" class="col" style="padding:0;max-width:none" hidden>
    <div class="runtop"><button class="btn small" onclick="newRun()">&larr; New run</button>
      <label class="switch" title="Show raw categories, scores, file paths and log lines"><input type="checkbox" id="techT" onchange="setTech(this.checked)"> Technical details</label></div>
    <div id="rvHead" class="rhead"></div>
    <div id="rvStatus"></div>
    <div id="rvReport"></div>
    <div id="rvTimeline"></div>
    <div id="rvMore"></div>
  </section>
  </div>
</main></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=s=>{if(s==null||isNaN(s))return"";s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
 return h?`${h}h ${String(m).padStart(2,"0")}m`:m?`${m}m ${String(x).padStart(2,"0")}s`:`${x}s`};
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
let ROOT="";
/* Never show the absolute home path: paths inside the project become relative, anything else under a home folder becomes ~ */
const rel=s=>{s=String(s??"");if(ROOT){s=s.split(ROOT+"/").join("").split(ROOT).join(".")}return s.replace(/\/(?:Users|home)\/[^/\s]+/g,"~")};

const TARGETS={
 shopcart:{name:"ShopCart demo",d:"A small online-shop app with planted bugs. Runs on this computer.",t:"About 2–6 min · no Docker",eta:"A ShopCart run usually takes about 2–6 minutes.",max:360},
 swebench:{name:"SWE-bench Lite",d:"A real bug from an open-source Python project, in its own container.",t:"About 5–15 min · uses Docker",eta:"A SWE-bench run usually takes about 5–15 minutes (longer the first time, while Docker downloads the project).",max:900},
 folder:{name:"Your own project",d:"Any Python project folder on this computer that has tests.",t:"Time depends on the project",eta:"Time depends on the size of the project; a few minutes is typical.",max:900}};
const CFG={
 A:{name:"Basic",d:"Finds code by keyword search only. The simplest version, kept for comparison.",tech:"A · baseline: keyword search, test-guided repair"},
 C:{name:"Smarter search",d:"Also matches function names and error messages to find the right code.",tech:"C · knowledge: + hybrid search (BM25, names, traceback)"},
 D:{name:"Code map",d:"Also follows which functions call which, to rank where the bug probably is.",tech:"D · code graph: + graph re-ranking, search feeds the localiser"},
 E:{name:"Full agent",d:"Everything in Code map, plus an independent review of each fix and a guard that stops it repeating a failed idea.",tech:"E · full design: D + independent review + loop controller",rec:1}};
const cfgName=k=>CFG[k]?`${CFG[k].name} (${k})`:`setup ${k||"?"}`;
const STAGES=[["Understand the report","Reads the report: what should happen, and what happens instead."],
 ["Find the code","Searches the project for the functions the report is about."],
 ["Show the bug with a test","Writes a small test that should fail because of the bug, runs it, and retries if the test itself is wrong."],
 ["Find the faulty function","Ranks the functions most likely to contain the mistake."],
 ["Write the fix","Edits the code; up to 3 tries."],
 ["Check the fix","Re-runs the bug's test and the project's existing tests; a reviewer double-checks the change."],
 ["Verdict","Reports what worked. Benchmark bugs are also graded against the developers' real fix."]];
const TAGSTAGE={runner:0,spec:0,explore:1,relax:2,attempt:2,localize:3,"re-localize":3,regression:3,patch:4,sweep:5,review:5,blackboard:5,f2p:6};
const TEST_CAT={reproduced:["Bug shown","ok","The test fails exactly the way the report describes."],
 invalid_test:["Test had a mistake of its own","warn","The test itself was broken (for example it called a function the wrong way), so it never reached the bug."],
 wrong_failure:["Failed for a different reason","warn","The test failed, but not in the way the report describes, so it does not prove this bug."],
 test_passed:["Bug not triggered","warn","The test passed, so it did not show the bug."]};
const FIX_CAT={plausible:["Passes the checks","ok","The bug's test now passes and the project's existing tests still pass."],
 partial:["Partly fixed","warn","Some failing checks now pass. The change is kept while the agent looks for the remaining place."],
 repro_still_fails:["Bug still there","bad","After this change the bug's test still fails."],
 regression:["Broke other tests","bad","The change broke existing tests that passed before."],
 not_applicable:["Edit did not fit the code","bad","The edit could not be applied to the file."],
 review_rejected:["Rejected by the reviewer","bad","The independent reviewer found a problem with the change."]};
const KIND={wrong_value:"a wrong result",exception:"a crash or error",other:"unexpected behaviour"};
const TICK={ok:"✓",bad:"✕",warn:"!",info:"•",skip:"–"};
const SCRIBBLE=`<svg class="scribble" viewBox="0 0 120 40" preserveAspectRatio="none" aria-hidden="true"><path d="M14 30 C 3 22, 9 8, 38 5 C 66 2, 108 4, 115 17 C 121 30, 92 37, 60 37 C 30 37, 6 33, 8 20 C 10 12, 22 8, 30 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>`;

let OPT=null,target="shopcart",cfg="E",JOB=null,lines=[],timer=null,jobState=null,polls=0,liveElsewhere=false,tabSel=0,tech=store.get("ra-tech")==="1"||new URLSearchParams(location.search).get("tech")==="1";

function tagOf(s){const m=s.match(/^\[([a-z][a-z\- ]*?)(?: \d+)?\]/i);return m?m[1].split(" ")[0].toLowerCase():null}
function setHTML(el,h){if(el._h!==h){el.innerHTML=h;el._h=h}}

/* ---------- theme ---------- */
function applyTheme(t){if(t==="light"||t==="dark")document.documentElement.dataset.theme=t;else delete document.documentElement.dataset.theme;$("themeBtn").textContent=t==="light"?"Light":t==="dark"?"Dark":"Auto"}
function cycleTheme(){const cur=document.documentElement.dataset.theme||"auto";const nx=cur==="auto"?"light":cur==="light"?"dark":"auto";store.set("ra-theme",nx);applyTheme(nx)}
applyTheme(new URLSearchParams(location.search).get("theme")||store.get("ra-theme")||"auto");
function toggleSide(on){$("app").classList.toggle("open",on)}

/* ---------- new-run form ---------- */
function renderChoices(){
 $("targets").innerHTML=Object.entries(TARGETS).map(([k,t])=>`<button class="choice ${k===target?"on":""}" data-t="${k}" aria-pressed="${k===target}"><span class="cn">${t.name}</span><span class="cd">${t.d}</span><span class="ct">${t.t}</span></button>`).join("");
 $("cfgs").innerHTML=Object.entries(CFG).map(([k,c])=>`<button class="choice ${k===cfg?"on":""}" data-c="${k}" aria-pressed="${k===cfg}" title="${esc(c.tech)}"><span class="cn"><span>${c.name} <span class="ct">(${k})</span></span>${c.rec?`<span class="rec">Recommended</span>`:""}</span><span class="cd">${c.d}</span></button>`).join("");
 $("cfgLegend").innerHTML=`<li>Setup letters as used in the project report:</li>`+Object.values(CFG).map(c=>`<li>${esc(c.tech)}</li>`).join("");
 $("eta").textContent=TARGETS[target].eta}
$("cfgs").onclick=e=>{const c=e.target.closest(".choice");if(!c)return;cfg=c.dataset.c;renderChoices()};
$("targets").onclick=e=>{const t=e.target.closest(".choice");if(!t)return;target=t.dataset.t;renderChoices();renderPick()};
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key==="Enter"&&!$("newView").hidden){e.preventDefault();run()}});

function renderPick(){if(!OPT)return;$("gradeWrap").hidden=target!=="swebench";let h="";
 if(target==="shopcart")h=`<label class="f"><span class="lbl">Starting bug (fills in the report below; edit it freely)</span><select id="bug">${OPT.shopcart.map(b=>`<option value="${esc(b.id)}">${esc(b.title)}  ·  ${esc(b.id)}</option>`).join("")}</select></label>`;
 else if(target==="swebench")h=`<label class="f"><span class="lbl">Bug (${OPT.swebench.length} available · ● = project already downloaded, starts faster)</span><input id="bugq" placeholder="Filter: django, sympy, flask…"><select id="bug" size="6">${sweOpts("")}</select></label>`;
 else h=`<div class="choices cfg"><label class="f"><span class="lbl">Project folder (a Python project with tests)</span><input id="repo" placeholder="/path/to/project"></label><label class="f"><span class="lbl">Fixed version folder (optional, used only for grading)</span><input id="fixedp" placeholder="/path/to/fixed"></label></div>`;
 $("pick").innerHTML=h;
 if($("bug")){$("bug").onchange=prefill;if($("bug").options.length){$("bug").selectedIndex=0;prefill()}}
 if($("bugq"))$("bugq").oninput=e=>{$("bug").innerHTML=sweOpts(e.target.value);if($("bug").options.length){$("bug").selectedIndex=0;prefill()}};
 if(target==="folder"){$("title").value="";$("body").value=""}}
function sweOpts(q){q=q.toLowerCase();return OPT.swebench.filter(b=>!q||b.id.toLowerCase().includes(q)||b.title.toLowerCase().includes(q)).map(b=>`<option value="${esc(b.id)}">${b.image_ready?"● ":"   "}${esc(b.id)} · ${esc(b.title.slice(0,70))}</option>`).join("")}
function prefill(){const list=target==="shopcart"?OPT.shopcart:OPT.swebench;const b=list.find(x=>x.id===$("bug").value);if(b){$("title").value=b.title;$("body").value=b.body}}

async function loadOptions(){OPT=await (await fetch("/api/options")).json();if(OPT.root)ROOT=OPT.root;renderPick();
 renderBanner();
 if(!JOB&&!location.hash&&OPT.active)openJob(OPT.active)}
function renderBanner(){if(!OPT)return;let b="";if(OPT.night_running)b+=`<div class="note">The overnight benchmark is using the same AI model and Docker right now. A run here still works, but it will be slower.</div>`;
 if(OPT.active&&OPT.active!==JOB)b+=`<div class="note" style="margin-top:8px">A run is in progress. Only one runs at a time. <a href="#${esc(OPT.active)}">Open it</a></div>`;
 $("banner").innerHTML=b;$("runBtn").disabled=!!OPT.active}

/* ---------- history ---------- */
function badgeOf(r){if(r.status==="running")return["run","Running"];
 if(r.error==="stopped by user"||r.status==="stopped")return["","Stopped"];
 if(r.error)return["bad","Failed"];
 if(r.status==="interrupted")return["","Unfinished"];
 if(r.fix_status==="fixed"&&r.resolved===false)return["warn","Fixed, hidden tests fail"];
 if(r.fix_status==="fixed")return["ok","Fixed"];
 if(r.verdict==="reproduced"&&r.fix_status==="not_fixed")return["warn","Reproduced, not fixed"];
 if(r.verdict==="reproduced")return["ok","Reproduced"];
 if(r.verdict==="needs_more_information")return["warn","Needs more detail"];
 if(r.verdict==="not_reproduced")return["bad","Not reproduced"];
 return["",r.status||"?"]}
function whenOf(id){const m=String(id).match(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)/);if(!m)return"";const d=new Date(+m[1],m[2]-1,+m[3],+m[4],+m[5]);
 const t=`${m[4]}:${m[5]}`;const today=new Date();if(d.toDateString()===today.toDateString())return`Today ${t}`;
 return`${d.toLocaleDateString(undefined,{day:"numeric",month:"short"})}, ${t}`}
async function loadHistory(){const h=await (await fetch("/api/history")).json();
 $("hist").innerHTML=h.map(r=>{const[c,l]=badgeOf(r);
 return `<button class="hitem ${JOB===r.id?"on":""}" data-j="${esc(r.id)}" title="${esc(r.title)}"><span class="ht">${esc(r.title)}</span><span class="hm"><span class="badge ${c}">${esc(l)}</span>${whenOf(r.id)}${r.wall_s!=null?` · ${fmt(r.wall_s)}`:""} · ${esc(CFG[r.config]?.name||r.config)}</span></button>`}).join("")||`<div class="hint" style="padding:6px 10px">No runs yet</div>`}
$("hist").onclick=e=>{const h=e.target.closest(".hitem");if(h){location.hash=h.dataset.j;toggleSide(false)}};

/* ---------- modes ---------- */
function showMode(m){$("newView").hidden=m!=="new";$("runView").hidden=m!=="run"}
function newRun(){JOB=null;clearInterval(timer);showMode("new");renderBanner();toggleSide(false);if(location.hash)history.replaceState(null,"",location.pathname+location.search);loadHistory();loadOptions();window.scrollTo(0,0)}
window.addEventListener("hashchange",()=>{const id=location.hash.slice(1);if(id&&id!==JOB)openJob(id);else if(!id&&JOB)newRun()});

async function run(){if($("runBtn").disabled)return;$("formErr").innerHTML="";
 const body={target,config:cfg,title:$("title").value,body:$("body").value,fix:$("fix").checked,grade:$("grade").checked};
 if(target!=="folder")body.bug_id=$("bug")?.value;else{body.repo=$("repo").value;body.fixed=$("fixedp").value}
 $("runBtn").disabled=true;
 try{const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json();
  if(!r.ok)throw new Error(j.error||r.status);location.hash=j.id}catch(e){$("formErr").innerHTML=`<div class="errbox">${esc(e.message)}</div>`;$("runBtn").disabled=false}}

function openJob(id){JOB=id;lines=[];jobState=null;polls=0;liveElsewhere=false;tabSel=0;clearInterval(timer);showMode("run");
 if(location.hash.slice(1)!==id)history.replaceState(null,"","#"+id);
 ["rvHead","rvStatus","rvReport","rvTimeline","rvMore"].forEach(k=>setHTML($(k),""));
 setHTML($("rvStatus"),`<div class="card empty">Loading…</div>`);
 $("techT").checked=tech;$("runView").classList.toggle("tech-on",tech);
 renderBanner();window.scrollTo(0,0);   /* open at the top: status first. (The old page auto-scrolled to the bottom here.) */
 poll();timer=setInterval(poll,1000);loadHistory()}
function setTech(on){tech=on;store.set("ra-tech",on?"1":"0");$("runView").classList.toggle("tech-on",on)}

async function poll(){if(!JOB)return;const id=JOB;let s;try{s=await (await fetch(`/api/job/${encodeURIComponent(id)}?from=${lines.length}`)).json()}catch(e){return}
 if(id!==JOB)return;
 if(s.error){clearInterval(timer);setHTML($("rvStatus"),`<div class="errbox">No run called “${esc(id)}” was found.</div>`);return}
 if(s.root)ROOT=s.root;
 if(s.status==="interrupted"&&polls>0&&s.lines.length)liveElsewhere=true;   /* log still growing: another runner owns this job */
 polls++;lines.push(...s.lines);jobState=s;renderJob();
 if(s.status!=="running"){clearInterval(timer);
  if(s.status==="interrupted")timer=setInterval(poll,4000);   /* slow check in case it is still being written */
  if(polls===1||s.status!=="interrupted"){loadHistory();loadOptions()}}}
function stopJob(){if(confirm("Stop this run? The agent will end where it is."))fetch(`/api/job/${encodeURIComponent(JOB)}/stop`,{method:"POST"})}

/* ---------- reading the log ---------- */
function analyse(){const a={tests:[],fixes:[],stopRepro:false,stopFix:false,review:null,f2p:null,localized:false,spec:false,explore:false};
 for(const l of lines){const t=tagOf(l.s),s=l.s;if(!t)continue;let m;
  if(t==="spec")a.spec=true;else if(t==="explore")a.explore=true;
  else if(t==="attempt"){m=s.match(/-> (\S+)/);a.tests.push(m?m[1]:"?")}
  else if(t==="localize"||t==="re-localize"||t==="regression")a.localized=true;
  else if(t==="patch"){m=s.match(/-> (\S+)/);a.fixes.push(m?m[1]:"?")}
  else if(t==="review")a.review=/APPROVE/.test(s)?"approve":"reject";
  else if(t==="control"&&/stopping/.test(s)){if(a.localized)a.stopFix=true;else a.stopRepro=true}
  else if(t==="f2p")a.f2p=s}
 a.testOk=a.tests.includes("reproduced");a.fixOk=a.fixes.includes("plausible");return a}
const hasOracle=sp=>sp.target==="shopcart"||(sp.target==="folder"&&!!sp.fixed)||(sp.target==="swebench"&&!!sp.grade);

/* One plain sentence for "what is happening now" and "what comes next". */
function nowState(a,sp){const B=+sp.budget||4,F=3,lt=a.tests[a.tests.length-1],lf=a.fixes[a.fixes.length-1];
 if(!a.spec)return{st:0,head:"Reading the bug report",sub:"The AI model is working out what should happen and what actually happens.",next:"search the code"};
 if(!a.explore)return{st:1,head:"Searching the code",sub:"Looking for the functions the report talks about.",next:"write a test that shows the bug"};
 if(!a.testOk&&!a.stopRepro&&a.tests.length<B){const n=a.tests.length+1;
  return{st:2,head:"Writing a test that shows the bug",tail:`attempt ${n} of up to ${B}`,
   sub:lt&&TEST_CAT[lt]?`Attempt ${n-1}: ${TEST_CAT[lt][0].toLowerCase()}. ${TEST_CAT[lt][2]} Trying again.`:"A good test fails on the buggy code for exactly the reason the report gives.",
   next:sp.fix===false?"report the verdict":"find the faulty function"}}
 if(!a.testOk)return{st:6,head:"Wrapping up",sub:a.stopRepro?"The same failure kept repeating, so the agent stopped trying and is writing its report.":`None of the ${a.tests.length} tests showed the bug. Writing the report.`};
 if(sp.fix===false)return{st:6,head:"Wrapping up",sub:"The bug is shown. No fix was requested, so the agent is writing its report."};
 if(!a.localized)return{st:3,head:"Finding the faulty function",sub:"The test proves the bug. Now the agent ranks functions by how likely they are to hold the mistake.",next:"write the fix"};
 if(!a.fixOk&&!a.stopFix&&a.fixes.length<F){const n=a.fixes.length+1;
  return{st:4,head:"Writing the fix",tail:`attempt ${n} of up to ${F}`,
   sub:lf&&FIX_CAT[lf]?`Fix ${n-1}: ${FIX_CAT[lf][0].toLowerCase()}. ${FIX_CAT[lf][2]} Trying again.`:"It edits the likely faulty function, then re-runs the bug's test and the existing tests.",next:"check the fix"}}
 if(a.fixOk)return{st:6,head:hasOracle(sp)?"Grading the result":"Wrapping up",sub:hasOracle(sp)?"The fix passed its checks. Now the test is compared with the developers' real fix. This is for grading only; the agent never sees it.":"The fix passed its checks. Writing the report."};
 return{st:6,head:"Wrapping up",sub:"No fix passed the checks. Writing the report."}}

function trackStates(a,s,running,ns){
 if(running)return STAGES.map((_,i)=>i<ns.st?"done":i===ns.st?"active":"todo");
 const r=s.result,S=Array(7).fill("skip");
 if(!r||r.error){const st=nowState(a,s.spec||{}).st,bad=r&&r.error&&r.error!=="stopped by user";   /* where it stopped */
  return STAGES.map((_,i)=>i<st?"done":i===st?(bad?"fail":"halt"):"skip")}
 if(a.spec)S[0]="done";if(a.explore)S[1]="done";
 if(a.tests.length)S[2]=a.testOk?"done":"fail";
 if(a.localized)S[3]="done";
 if(a.fixes.length)S[4]=a.fixOk?"done":"fail";
 if(a.fixOk)S[5]=a.review==="reject"?"fail":"done";
 const v=verdictOf(s,a);S[6]=v.tone==="ok"?"done":v.tone==="bad"?"fail":"halt";
 return S}

function trackHTML(S){const cur=S.findIndex(x=>x==="active");const capI=cur>=0?cur:Math.max(0,S.reduce((k,x,i)=>x!=="skip"?i:k,0));
 return `<ol class="track" aria-label="Progress">${STAGES.map((st,i)=>{const c=S[i];const sym=c==="done"?"✓":c==="fail"?"✕":c==="skip"?"–":c==="halt"?"■":String(i+1);
  return `<li class="st ${c}" title="${esc(st[1])}"><span class="sn">${sym}</span><span class="sl">${c==="active"?SCRIBBLE:""}${st[0]}</span></li>`}).join("")}</ol>
  <div class="trackcap">Step ${capI+1} of 7 · ${STAGES[capI][0]}</div>`}

function plainErr(e){e=String(e||"");
 if(/11434|ollama/i.test(e))return"The agent could not reach the AI model on this computer (Ollama, port 11434). Check that Ollama is running and the model is installed, then run again.";
 if(/docker/i.test(e))return"There was a Docker problem. Check that Docker Desktop is running, then run again.";
 if(/not found/i.test(e))return"A file or folder the run needed was not found.";
 return"Something went wrong inside the agent. The technical message is below."}

function verdictOf(s,a){const r=s.result,sp=s.spec||{};
 if(!r)return liveElsewhere?{tone:"",head:"Still running in another window",sum:"This run belongs to another copy of the runner, which is still writing to it. Progress updates here every few seconds."}
  :{tone:"",head:"This run did not finish",sum:"No result was saved. The run was stopped, or the runner was restarted before it finished."};
 if(r.error==="stopped by user"||s.status==="stopped")return{tone:"",head:"Stopped",sum:"The run was stopped before it finished."};
 if(r.error)return{tone:"bad",head:"The run failed",sum:plainErr(r.error)};
 const n=r.attempts??a.tests.length;
 if(r.verdict==="reproduced"){
  if(r.fix_status==="fixed"&&r.resolved===false)return{tone:"warn",head:"Bug reproduced; fix not confirmed",sum:"The fix passes the agent's own checks but fails the benchmark's hidden tests."};
  if(r.fix_status==="fixed")return{tone:"ok",head:"Bug reproduced and fixed",sum:"The agent wrote a test that shows the bug, then a fix that makes the test pass without breaking the existing tests."+(r.resolved?" The benchmark's hidden tests confirm it.":"")};
  if(r.fix_status==="not_fixed")return{tone:"warn",head:"Bug reproduced, not fixed",sum:"The agent proved the bug with a failing test, but none of its fixes passed the checks."};
  return{tone:"ok",head:"Bug reproduced",sum:"The agent wrote a test that shows the bug."+(sp.fix===false?" No fix was requested.":"")}}
 if(r.verdict==="needs_more_information")return{tone:"warn",head:"The report needs more detail",sum:"Nothing in the code matched the report well enough to write a meaningful test."};
 return{tone:"bad",head:"Could not reproduce the bug",sum:`After ${n} attempt${n===1?"":"s"}, no test failed the way the report describes`+(a.stopRepro?". It stopped early because the same failure kept repeating.":".")}}

function checkRows(s,a){const r=s.result||{},sp=s.spec||{},out=[],n=r.attempts??a.tests.length;
 out.push(r.verdict==="reproduced"?["ok","Showed the bug with a failing test",`Yes. It took ${n} attempt${n===1?"":"s"}.`]:["bad","Showed the bug with a failing test",`No, not after ${n} attempt${n===1?"":"s"}.`]);
 if(r.verdict==="reproduced"&&r.fail_to_pass!=null)out.push(r.fail_to_pass?["ok","The test is genuine","It fails on the buggy code and passes on the developers' fixed version. (Grading only: the agent never saw the fix.)"]
  :["warn","The test is genuine","Not confirmed: the test also fails on the developers' fixed version, so it may not target this exact bug."]);
 if(r.fix_status==="fixed")out.push(["ok","Wrote a fix that passes the checks","The bug's test now passes and the existing tests still pass"+(a.review==="approve"?"; an independent reviewer approved the change.":".")]);
 else if(r.fix_status==="not_fixed")out.push(["bad","Wrote a fix that passes the checks",a.fixes.length?`No. None of the ${a.fixes.length} fix attempt${a.fixes.length===1?"":"s"} passed.`:"No fix passed."]);
 else out.push(["skip","Wrote a fix",sp.fix===false?"Not requested for this run.":"Skipped: a fix needs a failing test first."]);
 if(r.resolved!=null)out.push(r.resolved?["ok","Benchmark's hidden tests pass","Independent confirmation that the fix solves the real issue."]:["bad","Benchmark's hidden tests pass","No. The fix did not pass the benchmark's hidden tests."]);
 return out}

/* Each log line becomes a plain-language event; the raw line stays available under Technical details. */
function parseEv(l,sp){const s=l.s,t=tagOf(s);let m;const raw=`<div class="tech techline">${esc(rel(s))}</div>`;const B=+sp.budget||4;
 const ev=(tone,title,body,extra="")=>({t,stage:TAGSTAGE[t],tone,title,body,tech:extra+raw});
 if(t==="runner"){m=s.match(/^\[runner\] local project (.*) \(config (\w+)\)/);
  if(m)return ev("info","Started",`Working on the project in <code>${esc(rel(m[1]))}</code>.`);
  m=s.match(/SWE-bench (\S+)/);return ev("info","Started",m?`Working on <code>${esc(m[1])}</code> inside a Docker container. The first run of a project downloads it, which can take a few minutes.`:esc(rel(s.replace(/^\[runner\] /,""))))}
 if(t==="spec"){m=s.match(/expected=(['"])(.*)\1 symbols=\[(.*?)\] kind=(\S+)/);if(!m)return ev("info","Read the bug report",esc(s));
  const sy=m[3].split(",").map(x=>x.trim().replace(/^'|'$/g,"")).filter(Boolean);
  return ev("ok","Read the bug report",`The bug is ${KIND[m[4]]||"unexpected behaviour"}. It should: <i>“${esc(m[2])}”</i>`,
   `<div class="tech chips">${sy.map(x=>`<span class="chip">${esc(x)}</span>`).join("")}<span class="chip">kind=${esc(m[4])}</span></div>`)}
 if(t==="explore"){const it=s.slice(10).split(", ").map(x=>x.match(/^(.*)\.([^.(]+)\(([-\d.]+)\)$/)).filter(Boolean);
  if(!it.length)return ev("warn","Searched the code","Nothing in the code matched the report closely.");
  return ev("ok","Found the code the report is about",`Best match: <code>${esc(it[0][2])}</code> in ${esc(it[0][1])}`+(it.length>1?`; also ${it.slice(1,4).map(x=>`<code>${esc(x[2])}</code>`).join(", ")}.`:"."),
   `<div class="tech chips" title="Relevance score: higher means a closer match">${it.map(x=>`<span class="chip">${esc(x[1])}.${esc(x[2])} · ${x[3]}</span>`).join("")}</div>`)}
 if(t==="relax")return ev("info","Removed a check the agent made up","The test checked for an error message the report never mentions, so that check was dropped.");
 if(t==="attempt"){m=s.match(/\[attempt (\d+)\] exec=(\S+) exc=(\S+) -> (\S+)/);if(!m)return ev("info","Test attempt",esc(s));
  const c=TEST_CAT[m[4]]||[m[4],"warn",""];
  return ev(c[1],`Attempt ${m[1]} of up to ${B}: ${c[0]}`,c[2],`<div class="tech techline">ran: ${esc(m[2])} · raised: ${esc(m[3])} · category: ${esc(m[4])}</div>`)}
 if(t==="f2p"){m=s.match(/passes-on-fixed=(\S+) valid-by-meaning=(\S+)/);const ok=m&&m[1]==="True",mean=m&&m[2]==="True";
  return ev(ok||mean?"ok":"bad","Graded against the developers' real fix",(ok?"The test fails on the buggy code and passes on the fixed code, so it targets the real bug.":mean?"On the fixed code the test only differs in message wording, so it is judged valid by meaning.":"The test also fails on the fixed code, so it may not target this exact bug.")+` <span class="muted">(Grading only: the agent never sees this.)</span>`)}
 if(t==="localize"||t==="re-localize"){let body=s.replace(/^\[[^\]]+\] /,""),lead="";
  if((m=body.match(/^(\d+) (?:failing repro test function\(s\)|still failing); (.*)$/))){lead=`${m[1]} part${m[1]==="1"?"":"s"} of the bug's test ${t==="localize"?"fail":"still fail"}. `;body=m[2]}
  const it=body.split(", ").map(x=>x.match(/^(.*):(.+)\(([-\d.]+)\)$/)).filter(Boolean);
  const head=t==="localize"?"Ranked the likely faulty functions":"Looked again after a partial fix";
  if(!it.length)return ev("info",head,lead+esc(rel(body)));
  return ev("ok",head,`${lead}Most likely: <code>${esc(it[0][2])}</code> in ${esc(it[0][1])}`+(it.length>1?`, then ${it.slice(1,3).map(x=>`<code>${esc(x[2])}</code>`).join(", ")}.`:"."),
   `<div class="tech chips" title="Suspicion score: higher means more likely">${it.map(x=>`<span class="chip">${esc(x[1])}:${esc(x[2])} · ${x[3]}</span>`).join("")}</div>`)}
 if(t==="regression"){const f=(s.match(/'([^']+)'/g)||[]).map(x=>x.slice(1,-1));
  return ev("info","Chose existing tests as a safety net",f.length?`${f.map(x=>`<code>${esc(x)}</code>`).join(", ")} will be re-run on every fix, so the fix cannot quietly break something else.`:"No existing tests matched, so only the bug's own test will check the fix.")}
 if(t==="patch"){m=s.match(/\[patch (\d+)\] -> (\S+)(?: \((.*)\))?/);if(!m)return ev("info","Fix attempt",esc(s));
  const c=FIX_CAT[m[2]]||[m[2],"warn",""];
  return ev(c[1],`Fix attempt ${m[1]} of up to 3: ${c[0]}`,c[2]+(m[3]?` Changed: ${m[3].split(", ").map(x=>`<code>${esc(rel(x))}</code>`).join(", ")}.`:""),`<div class="tech techline">category: ${esc(m[2])}</div>`)}
 if(t==="sweep"){const b=s.replace(/^\[sweep\] /,"");
  if(/^no similar/.test(b))return ev("info","Looked for the same mistake elsewhere","No similar code was found.");
  if(/^similar code/.test(b))return ev("info","Looked for the same mistake elsewhere",`Similar code to check: ${esc(rel(b.replace(/^similar code: /,"")))}`);
  if(/same mistake not present/.test(b))return ev("ok","Looked for the same mistake elsewhere","The similar code does not have the same mistake.");
  m=b.match(/-> (\S+)/);const c=m&&FIX_CAT[m[1]];return ev(c?c[1]:"info","Fixed the same mistake in similar code",c?c[2]:esc(rel(b)))}
 if(t==="review"){const ok=/APPROVE/.test(s);const why=s.split(": ").slice(1).join(": ");
  return ev(ok?"ok":"bad",ok?"Reviewer approved the fix":"Reviewer rejected the fix",ok?"An independent check of the change found no problems.":`Reason: ${esc(why||"not given")}. The agent will try again.`)}
 if(t==="control"){const b=s.replace(/^\[control\] /,"");
  if(/stopping/.test(b)){m=b.match(/(\d+) times/);return ev("warn","Stopped repeating itself",`The same failure happened ${m?m[1]+" times":"several times"} in a row, so the agent stopped this step instead of wasting time.`)}
  m=b.match(/focus on (.*)$/);return ev("info","Changed approach",m?`Now focusing on <code>${esc(rel(m[1]))}</code>.`:"Trying a different approach.")}
 if(t==="blackboard")return ev("bad","Safety rule blocked an unchecked fix","The fix changed after it was tested, so it is not reported as fixed.");
 if(t==="result")return null;
 return{t:"raw"}}

function diffView(d){const files=[];let cur=null;
 for(const l of String(d||"").split("\n")){
  if(l.startsWith("diff --git")){cur={name:l.replace(/^diff --git a\/(\S+) b\/.*$/,"$1"),lines:[]};files.push(cur);continue}
  if(!cur){cur={name:"",lines:[]};files.push(cur)}
  if(l.startsWith("+++ ")){if(!cur.name)cur.name=l.slice(4).replace(/^b\//,"");continue}
  if(/^(--- |index )/.test(l))continue;
  cur.lines.push(l)}
 while(files.length&&files[files.length-1].lines.length&&files[files.length-1].lines.at(-1)==="")files[files.length-1].lines.pop();
 return files.map(f=>`<div class="diff"><div class="dfile">${esc(rel(f.name))||"Change"}</div><pre>${f.lines.map(l=>{const c=l.startsWith("@@")?"hunk":l.startsWith("+")?"add":l.startsWith("-")?"del":"";return `<span class="dl ${c}">${esc(l)||" "}</span>`}).join("")}</pre></div>`).join("")}

function renderJob(){const s=jobState,sp=s.spec||{},running=s.status==="running";const a=analyse();const live=running||(!s.result&&liveElsewhere);
 const T=TARGETS[sp.target]||{name:sp.target||"Project"};
 /* header */
 setHTML($("rvHead"),`<div class="lbl">${esc(T.name)}${sp.bug_id?` · ${esc(sp.bug_id)}`:""} · ${esc(cfgName(sp.config))} · ${whenOf(s.id)}</div><h1>${esc(sp.title||"Untitled run")}</h1>`);
 /* status or verdict */
 let h;
 if(live){const ns=nowState(a,sp);const el=running&&s.started?s.now-s.started:null;
  const slow=el!=null&&T.max&&el>T.max;
  h=`<section class="card status running" aria-live="polite"><div class="lbl"><span class="pulse"></span>${running?"Running":"Running elsewhere"} · step ${ns.st+1} of 7</div>
   <p class="nowline">${esc(ns.head)}${ns.tail?`: <em>${esc(ns.tail)}</em>`:""}</p>
   <p class="nowsub">${esc(ns.sub)}</p>
   ${trackHTML(trackStates(a,s,true,ns))}
   <div class="nowfoot"><div class="lbl">${ns.next?`Next: ${esc(ns.next)}<br>`:""}${el!=null?`Running for ${fmt(el)}. `:""}${slow?"Taking longer than usual; the AI model may be busy.":esc(T.eta||"")}</div>
   ${running?`<button class="btn small stop" onclick="stopJob()">Stop run</button>`:""}</div></section>`}
 else{const v=verdictOf(s,a),r=s.result||{},d=s.detail;
  h=`<section class="card status tone-${v.tone}"><div class="lbl">${r.wall_s!=null?`Finished · took ${fmt(r.wall_s)}`:s.status==="interrupted"?"Not finished":"Finished"}${r.llm_calls!=null?` · ${r.llm_calls} AI model call${r.llm_calls===1?"":"s"}`:""}</div>
   <h2 class="vhead">${esc(v.head)}</h2><p class="vsum">${esc(v.sum)}</p>`;
  if(s.result&&!r.error)h+=`<ul class="checks">${checkRows(s,a).map(([c,b,x])=>`<li><span class="ck ${c}">${TICK[c]}</span><div><b>${esc(b)}</b><span>${esc(x)}</span></div></li>`).join("")}</ul>`;
  if(r.error)h+=`<div class="techline" style="display:block">${esc(rel(r.error))}</div>`;
  h+=trackHTML(trackStates(a,s,false));
  if(d&&!d.missing&&d.fix?.final_diff)h+=`<div style="display:grid;gap:8px"><h3>The fix</h3><div class="hint">Green lines were added, red lines removed.</div>${diffView(d.fix.final_diff)}</div>`;
  if(d&&!d.missing&&d.final_test)h+=`<details class="more"><summary>The test that shows the bug</summary><pre style="margin-top:8px">${esc(d.final_test)}</pre></details>`;
  h+=`<div class="tech techline">${esc(rel(JSON.stringify(r)))}</div>`;
  h+=`<div class="nowfoot"><span></span><button class="btn small" onclick="rerun()">Edit and run again</button></div></section>`}
 setHTML($("rvStatus"),h);
 setHTML($("rvReport"),`<details class="more"><summary>The bug report given to the agent</summary><div class="card" style="margin-top:10px;white-space:pre-wrap">${esc(sp.body)}</div></details>`);
 /* timeline */
 const evs=[];for(const l of lines){const e=parseEv(l,sp);if(!e||e.t==="raw")continue;if(e.stage==null)e.stage=evs.length?evs[evs.length-1].stage:0;e.time=l.t&&s.started?fmt(l.t-s.started):"";evs.push(e)}
 for(let i=0;i+1<evs.length;i++)if(evs[i].t==="review"&&evs[i+1].t==="patch"){const x=evs[i];evs[i]=evs[i+1];evs[i+1]=x;i++}   /* the worker logs the review before the fix it reviewed */
 evs.forEach((e,i)=>{if((e.t==="attempt"||e.t==="patch")&&e.tone!=="ok"&&evs.slice(i+1).some(x=>x.t===e.t))e.body+=" The agent tried again."});
 let tl="",prev=-1;for(const e of evs){if(e.stage!==prev){tl+=`<li class="grp">Step ${e.stage+1} · ${STAGES[e.stage][0]}</li>`;prev=e.stage}
  tl+=`<li class="ev ${e.tone}"><span class="dot" aria-hidden="true">${TICK[e.tone]||"•"}</span><div class="evb"><div class="evh"><span class="evt">${e.title}</span><span class="evtime">${e.time?"+"+e.time:""}</span></div><div class="evd">${e.body}</div>${e.tech}</div></li>`}
 if(live)tl+=`<li class="waiting"><span class="pulse"></span>working…</li>`;
 setHTML($("rvTimeline"),`<div style="display:grid;gap:14px"><h2>What the agent did</h2>${evs.length?`<ol class="tl">${tl}</ol>`:`<div class="hint">Nothing yet.</div>`}</div>`);
 renderMore()}

function renderMore(){const s=jobState,d=s.detail,tabs=[];
 if(d&&!d.missing){
  if(d.attempts?.length)tabs.push(["Test attempts",d.attempts.map(x=>{const c=TEST_CAT[x.category]||[x.category||"?","",""];return `<details class="att"><summary><span class="badge ${c[1]}">${esc(c[0])}</span> <b>Test ${x.n}</b> <span class="tech mono muted">${esc(x.status)} · ${esc(x.exception||"no exception")} · ${esc(x.category)}</span></summary>${x.feedback?`<p style="margin:0 0 8px"><span class="lbl">Note the agent gave itself:</span> ${esc(rel(x.feedback))}</p>`:""}${x.failure_line?`<p style="margin:0 0 8px"><span class="lbl">Failure:</span> <code>${esc(rel(x.failure_line))}</code></p>`:""}<pre>${esc(x.code)}</pre></details>`}).join("")]);
  if(d.fix?.patches?.length)tabs.push(["Fix attempts",d.fix.patches.map(p=>{const c=FIX_CAT[p.category]||[p.category||"?","",""];return `<details class="att"><summary><span class="badge ${c[1]}">${esc(c[0])}</span> <b>Fix ${p.n}</b> <span class="tech mono muted">${esc(p.category)}${p.kind?" · "+esc(p.kind):""}</span></summary>${p.feedback?`<p style="margin:0 0 8px">${esc(rel(p.feedback))}</p>`:""}${p.diff?diffView(p.diff):"<div class='hint'>No change produced.</div>"}</details>`}).join("")]);
  if(d.spec)tabs.push(["How it read the report",`<dl class="kv"><dt>Summary</dt><dd>${esc(d.spec.summary)}</dd><dt>Expected</dt><dd>${esc(d.spec.expected)}</dd><dt>Observed</dt><dd>${esc(d.spec.observed)}</dd><dt>Kind of bug</dt><dd>${esc(KIND[d.spec.failure_kind]||d.spec.failure_kind)}</dd><dt>Key words</dt><dd>${esc((d.spec.symbols||[]).join(", "))}</dd>${d.spec.example_inputs?.length?`<dt>Example steps</dt><dd>${esc(d.spec.example_inputs.join("; "))}</dd>`:""}</dl>`]);
  if(d.fix?.candidates?.length)tabs.push(["Suspect functions",`<ol style="margin:0;padding-left:20px">${d.fix.candidates.map(c=>`<li style="margin-bottom:6px;overflow-wrap:anywhere">${esc(rel(c))}</li>`).join("")}</ol>`])}
 tabs.push(["Raw log",`<div class="raw">${esc(rel(lines.map(l=>l.s).join("\n")))||"(empty)"}</div>`]);
 if(tabSel>=tabs.length)tabSel=0;
 setHTML($("rvMore"),`<section class="card" style="display:grid;gap:14px"><div><h2>Behind the scenes</h2><div class="hint">Everything the agent produced, for anyone who wants the detail.</div></div>
  <div class="tabs">${tabs.map((t,i)=>`<button class="tab ${i===tabSel?"on":""}" onclick="tabSel=${i};renderMore()">${t[0]}</button>`).join("")}</div><div>${tabs[tabSel][1]}</div></section>`)}

function rerun(){const sp=jobState.spec;newRun();target=sp.target in TARGETS?sp.target:"shopcart";renderChoices();renderPick();
 setTimeout(()=>{if(sp.bug_id&&$("bug")){if(target==="swebench"&&$("bugq")){$("bugq").value=sp.bug_id;$("bugq").oninput({target:$("bugq")})}$("bug").value=sp.bug_id}
  if(target==="folder"){$("repo").value=sp.repo||"";$("fixedp").value=sp.fixed||""}
  $("title").value=sp.title;$("body").value=sp.body;cfg=sp.config in CFG?sp.config:"E";renderChoices();$("fix").checked=sp.fix!==false;$("body").focus()},0)}

renderChoices();
if(location.hash.length>1)openJob(location.hash.slice(1));   /* open a linked run at once; options (docker query) can be slow */
loadOptions();loadHistory();
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8766)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"ReproAgent Runner: http://127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        for j in JOBS.values():
            j.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
