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
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from live_dashboard import detail_from_path  # noqa: E402

UI_RUNS = ROOT / "runs" / "ui"
SHOP = ROOT / "benchmarks" / "shopcart"
PUBLIC = ROOT / "benchmarks" / "swe_bench_lite" / "public"
PY = ROOT / ".venv-autose" / "bin" / "python"


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
            "active": active().id if active() else None}


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
    out = {"id": jid, "status": status, "started": started, "now": time.time(),
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
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
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
                return self.send(PAGE, ctype="text/html; charset=utf-8")
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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root{--bg:#f5f6f8;--surface:#fff;--s2:#eef0f4;--fg:#161b26;--muted:#5d6577;--line:#d6dae3;--acc:#2a56c6;--acc-s:#e3eaff;
--ok:#1f8a4c;--ok-s:#e1f3e8;--warn:#b07d00;--warn-s:#fdf1cc;--bad:#c22f4a;--bad-s:#fbe5e9;--vio:#7048e8;--vio-s:#eee8ff;--teal:#1c8a8a;--teal-s:#ddf3f3;
--mono:"IBM Plex Mono",ui-monospace,monospace;--body:"IBM Plex Sans",system-ui,sans-serif;--disp:"Space Grotesk",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f131b;--surface:#171d29;--s2:#1f2635;--fg:#e5e9f1;--muted:#9aa4b8;--line:#2e3749;--acc:#8eaaff;--acc-s:#1f2a4a;
--ok:#5fd394;--ok-s:#17321f;--warn:#f2c14e;--warn-s:#3a3010;--bad:#ff7f96;--bad-s:#3d1c24;--vio:#a58bff;--vio-s:#2b2444;--teal:#5fd0d0;--teal-s:#123333;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f131b;--surface:#171d29;--s2:#1f2635;--fg:#e5e9f1;--muted:#9aa4b8;--line:#2e3749;--acc:#8eaaff;--acc-s:#1f2a4a;
--ok:#5fd394;--ok-s:#17321f;--warn:#f2c14e;--warn-s:#3a3010;--bad:#ff7f96;--bad-s:#3d1c24;--vio:#a58bff;--vio-s:#2b2444;--teal:#5fd0d0;--teal-s:#123333;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 var(--body)}
.app{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:100vh}
@media (max-width:860px){.app{grid-template-columns:1fr}.side{display:none}}
.side{border-right:1px solid var(--line);background:var(--surface);padding:18px 12px;display:flex;flex-direction:column;gap:10px;position:sticky;top:0;height:100vh;overflow:auto}
.brand{font:700 18px var(--disp);padding:0 6px}.brand span{color:var(--acc)}
.newbtn{width:100%}
.hist{display:grid;gap:2px}
.hitem{padding:8px 8px;border-radius:8px;cursor:pointer;font-size:13px;display:grid;gap:2px}
.hitem:hover,.hitem.on{background:var(--s2)}
.hitem .ht{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hitem .hm{font:11px var(--mono);color:var(--muted)}
main{padding:28px 16px 120px}
.col{max-width:860px;margin:0 auto;display:grid;gap:18px}
h1{font:700 clamp(24px,4vw,32px)/1.15 var(--disp);margin:0}
.muted{color:var(--muted)}.mono{font-family:var(--mono)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px}
.composer{display:grid;gap:12px;padding:16px;border-radius:16px;background:var(--surface);border:1px solid var(--line);box-shadow:0 6px 24px rgba(0,0,0,.05)}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{border:1px solid var(--line);background:var(--s2);border-radius:99px;padding:6px 12px;cursor:pointer;font-size:13px;color:var(--fg)}
.tab.on{background:var(--acc);border-color:var(--acc);color:#fff}
label.f{display:grid;gap:4px;font-size:12px;color:var(--muted);font-weight:600}
input,select,textarea{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:9px 11px;width:100%}
textarea{min-height:170px;resize:vertical;font-size:14px;line-height:1.5}
input:focus,select:focus,textarea:focus{outline:2px solid var(--acc);outline-offset:-1px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.row>*{flex:1 1 200px}
.cfgs{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
@media (max-width:600px){.cfgs{grid-template-columns:repeat(2,1fr)}}
.cfg{border:1px solid var(--line);border-radius:10px;padding:8px;cursor:pointer;background:var(--bg)}
.cfg b{font:600 14px var(--disp)}.cfg div{font-size:11px;color:var(--muted);line-height:1.35}
.cfg.on{border-color:var(--acc);background:var(--acc-s)}
.chk{display:flex;gap:6px;align-items:center;font-size:13px;flex:0 0 auto}
.chk input{width:auto}
button{font:inherit;background:var(--s2);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600;padding:10px 20px}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:not-allowed}
.actions{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.warn{background:var(--warn-s);color:var(--fg);border-radius:10px;padding:9px 12px;font-size:13px}
.errbox{background:var(--bad-s);border-radius:10px;padding:9px 12px;font-size:13px}
.prompt{background:var(--acc-s);border-radius:14px;padding:14px 16px;margin-left:auto;max-width:92%}
.prompt .pt{font-weight:600}.prompt .pb{white-space:pre-wrap;font-size:13px;color:var(--fg);opacity:.9;max-height:140px;overflow:auto;margin-top:4px}
.stages{display:grid;grid-template-columns:repeat(8,1fr);gap:6px}
@media (max-width:720px){.stages{grid-template-columns:repeat(4,1fr)}}
.stage{padding:9px 4px;border-radius:9px;background:var(--s2);text-align:center;font-size:11px;color:var(--muted);border:1px solid transparent}
.stage b{display:block;font:600 12px var(--disp);color:var(--fg);opacity:.5}
.stage.done{background:var(--acc-s)}.stage.done b{opacity:1}
.stage.active{border-color:var(--acc);background:var(--acc-s);animation:glow 1.4s ease-in-out infinite}.stage.active b{opacity:1}
.stage.good{background:var(--ok-s)}.stage.bad{background:var(--bad-s)}
@keyframes glow{50%{box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 30%,transparent)}}
.statusbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;font:600 12px var(--mono);padding:3px 10px;border-radius:99px;background:var(--s2)}
.pill.run{background:var(--acc-s);color:var(--acc)}.pill.ok{background:var(--ok-s);color:var(--ok)}.pill.bad{background:var(--bad-s);color:var(--bad)}
.spin{width:12px;height:12px;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:spin 1s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.tl{display:grid;gap:0;position:relative}
.ev{display:grid;grid-template-columns:34px minmax(0,1fr);gap:10px;padding:6px 0;animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}}
.ico{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-size:15px;background:var(--s2);position:relative}
.ev:not(:last-child) .ico::after{content:"";position:absolute;top:32px;left:14px;width:2px;height:calc(100% + 0px);background:var(--line);z-index:0}
.evb{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:10px 12px;min-width:0}
.evh{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.evt{font-weight:600}.evtime{font:11px var(--mono);color:var(--muted);white-space:nowrap}
.evd{font-size:13px;margin-top:3px;word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.chip{font:11px var(--mono);background:var(--s2);border-radius:6px;padding:2px 7px}
.tag{font:600 11px var(--mono);padding:1px 8px;border-radius:99px}
.tag.ok{background:var(--ok-s);color:var(--ok)}.tag.bad{background:var(--bad-s);color:var(--bad)}.tag.warn{background:var(--warn-s);color:var(--warn)}.tag.info{background:var(--acc-s);color:var(--acc)}
.k-spec .ico{background:var(--acc-s)}.k-search .ico{background:var(--teal-s)}.k-test .ico{background:var(--warn-s)}
.k-loc .ico{background:var(--vio-s)}.k-patch .ico{background:var(--ok-s)}.k-ctl .ico{background:var(--bad-s)}
.thinking{display:flex;gap:10px;align-items:center;color:var(--muted);font-size:13px;padding:8px 0 0 44px}
.dots span{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--muted);margin-right:3px;animation:b 1.2s infinite}
.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes b{40%{transform:translateY(-4px);opacity:1}0%,80%{opacity:.4}}
pre{margin:0;background:var(--s2);border-radius:8px;padding:10px;font:12px/1.5 var(--mono);overflow:auto;white-space:pre;max-height:360px}
.add{color:var(--ok)}.del{color:var(--bad)}
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.vk{background:var(--s2);border-radius:10px;padding:10px}
.vk span{font-size:11px;color:var(--muted);display:block}.vk b{font:600 16px var(--disp)}
.rtabs{display:flex;gap:6px;margin:12px 0 8px;flex-wrap:wrap}
details summary{cursor:pointer;font-size:13px;padding:4px 0}
.raw{font:12px/1.55 var(--mono);white-space:pre-wrap;background:var(--s2);border-radius:8px;padding:10px;max-height:300px;overflow:auto}
.empty{text-align:center;color:var(--muted);padding:30px 0 10px}
</style></head><body><div class="app">
<aside class="side">
  <div class="brand">Repro<span>Agent</span> Runner</div>
  <button class="newbtn" onclick="newRun()">+ New run</button>
  <div class="muted" style="font-size:11px;font-weight:600;letter-spacing:.06em;padding:6px 6px 0">HISTORY</div>
  <div class="hist" id="hist"></div>
</aside>
<main><div class="col">
  <div><div class="muted mono" style="font-size:12px;letter-spacing:.08em">LOCAL SLM · QWEN3:8B</div>
  <h1>Describe a bug. Watch the agent reproduce and fix it.</h1></div>

  <div id="banner"></div>

  <div class="composer" id="composer">
    <div class="tabs" id="targets">
      <button class="tab on" data-t="shopcart">ShopCart demo · fast, no Docker</button>
      <button class="tab" data-t="swebench">SWE-bench Lite · Docker</button>
      <button class="tab" data-t="folder">My own project folder</button>
    </div>
    <div id="pick"></div>
    <label class="f">Bug title<input id="title" placeholder="One line: what is wrong?"></label>
    <label class="f">Bug report (your prompt): steps, expected, actual
      <textarea id="body" placeholder="Describe the bug like a user would report it…"></textarea></label>
    <div><div class="muted" style="font-size:12px;font-weight:600;margin-bottom:4px">Agent configuration</div>
    <div class="cfgs" id="cfgs"></div></div>
    <div class="actions">
      <div class="row" style="gap:16px;flex:1">
        <label class="chk"><input type="checkbox" id="fix" checked> also write a fix</label>
        <label class="chk" id="gradeWrap" hidden><input type="checkbox" id="grade" checked> oracle + hidden-test grading</label>
      </div>
      <button class="primary" id="runBtn" onclick="run()">Run agent ▶</button>
    </div>
    <div id="formErr"></div>
  </div>

  <div id="runView" hidden></div>
  <div class="empty" id="empty">Pick a bug or write your own report above, then press <b>Run</b> (⌘/Ctrl + Enter).</div>
</div></main></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=s=>{if(s==null||isNaN(s))return"";s=Math.max(0,Math.round(s));const m=Math.floor(s/60),x=s%60;return m?`${m}m ${String(x).padStart(2,"0")}s`:`${x}s`};
const CFG=[["A","A · baseline","keyword search, test-guided repair"],["C","C · knowledge","+ hybrid search (BM25, names, traceback)"],
["D","D · code graph","+ graph re-ranking, search → localiser"],["E","E · full design","D + independent review + loop controller"]];
const STAGES=[["Understand","report → spec",["spec"]],["Search","find code",["explore"]],["Reproduce","failing test",["relax","attempt"]],
["Oracle","buggy ✗ fixed ✓",["f2p"]],["Localize","pick function",["localize","re-localize","regression"]],["Patch","edit + own tests",["patch","sweep"]],
["Review","review / control",["review","control"]],["Result","verdict",["result"]]];
let OPT=null,target="shopcart",cfg="E",JOB=null,lines=[],timer=null,jobState=null;

function tagOf(s){const m=s.match(/^\[([a-z][a-z\- ]*?)(?: \d+)?\]/i);return m?m[1].split(" ")[0].toLowerCase():null}

$("cfgs").innerHTML=CFG.map(([k,t,d])=>`<div class="cfg ${k===cfg?"on":""}" data-c="${k}"><b>${t}</b><div>${d}</div></div>`).join("");
$("cfgs").onclick=e=>{const c=e.target.closest(".cfg");if(!c)return;cfg=c.dataset.c;[...$("cfgs").children].forEach(x=>x.classList.toggle("on",x.dataset.c===cfg))};
$("targets").onclick=e=>{const t=e.target.closest(".tab");if(!t)return;target=t.dataset.t;[...$("targets").children].forEach(x=>x.classList.toggle("on",x===t));renderPick()};
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key==="Enter")run()});

function renderPick(){if(!OPT)return;$("gradeWrap").hidden=target!=="swebench";let h="";
 if(target==="shopcart")h=`<label class="f">Starting bug (prefills the report; ShopCart has a seeded mutation per bug)<select id="bug">${OPT.shopcart.map(b=>`<option value="${b.id}">${esc(b.id)} · ${esc(b.title)}</option>`).join("")}</select></label>`;
 else if(target==="swebench")h=`<label class="f">SWE-bench bug (${OPT.swebench.length} with code snapshots · ● image already pulled)<input id="bugq" placeholder="filter: django, sympy, flask…" style="margin-bottom:4px"><select id="bug" size="6">${sweOpts("")}</select></label>`;
 else h=`<div class="row"><label class="f">Project folder (Python repo with tests)<input id="repo" placeholder="/path/to/project"></label><label class="f">Fixed version folder (optional, for the oracle check)<input id="fixedp" placeholder="/path/to/fixed"></label></div>`;
 $("pick").innerHTML=h;
 if($("bug")){$("bug").onchange=prefill;if($("bug").options.length){$("bug").selectedIndex=0;prefill()}}
 if($("bugq"))$("bugq").oninput=e=>{$("bug").innerHTML=sweOpts(e.target.value);if($("bug").options.length){$("bug").selectedIndex=0;prefill()}};
 if(target==="folder"){$("title").value="";$("body").value=""}}
function sweOpts(q){q=q.toLowerCase();return OPT.swebench.filter(b=>!q||b.id.toLowerCase().includes(q)||b.title.toLowerCase().includes(q)).map(b=>`<option value="${b.id}">${b.image_ready?"● ":"   "}${esc(b.id)} · ${esc(b.title.slice(0,70))}</option>`).join("")}
function prefill(){const list=target==="shopcart"?OPT.shopcart:OPT.swebench;const b=list.find(x=>x.id===$("bug").value);if(b){$("title").value=b.title;$("body").value=b.body}}

async function loadOptions(){OPT=await (await fetch("/api/options")).json();renderPick();
 $("banner").innerHTML=OPT.night_running?`<div class="warn">⚠︎ The overnight benchmark run is using the same local model and Docker right now. A run here works but will be slower, and it adds a little time to the overnight run's timing numbers.</div>`:"";
 if(OPT.active&&!JOB)openJob(OPT.active)}
async function loadHistory(){const h=await (await fetch("/api/history")).json();
 $("hist").innerHTML=h.map(r=>{const st=r.status==="running"?"⏳":r.error?"⚠︎":r.resolved?"✅":r.fix_status==="fixed"?"🩹":r.verdict==="reproduced"?"🧪":r.verdict?"·":"…";
 return `<div class="hitem ${JOB===r.id?"on":""}" data-j="${r.id}"><div class="ht">${st} ${esc(r.title)}</div><div class="hm">${esc(r.bug_id||r.target)} · ${r.config} · ${fmt(r.wall_s)||r.status}</div></div>`}).join("")||`<div class="muted" style="font-size:12px;padding:6px">No runs yet</div>`}
$("hist").onclick=e=>{const h=e.target.closest(".hitem");if(h)openJob(h.dataset.j)};

function newRun(){JOB=null;clearInterval(timer);$("runView").hidden=true;$("composer").hidden=false;$("empty").hidden=false;loadHistory();window.scrollTo({top:0,behavior:"smooth"})}

async function run(){if($("runBtn").disabled)return;$("formErr").innerHTML="";
 const body={target,config:cfg,title:$("title").value,body:$("body").value,fix:$("fix").checked,grade:$("grade").checked};
 if(target!=="folder")body.bug_id=$("bug")?.value;else{body.repo=$("repo").value;body.fixed=$("fixedp").value}
 $("runBtn").disabled=true;
 try{const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json();
  if(!r.ok)throw new Error(j.error||r.status);openJob(j.id)}catch(e){$("formErr").innerHTML=`<div class="errbox">${esc(e.message)}</div>`}
 finally{$("runBtn").disabled=false}}

function openJob(id){JOB=id;lines=[];jobState=null;clearInterval(timer);$("composer").hidden=true;$("empty").hidden=true;$("runView").hidden=false;
 $("runView").innerHTML=`<div class="empty"><span class="spin"></span></div>`;poll();timer=setInterval(poll,1000);loadHistory()}

async function poll(){if(!JOB)return;const id=JOB;let s;try{s=await (await fetch(`/api/job/${id}?from=${lines.length}`)).json()}catch(e){return}
 if(id!==JOB||s.error)return;lines.push(...s.lines);jobState=s;renderJob();
 if(s.status!=="running"){clearInterval(timer);loadHistory();loadOptions()}}

function stopJob(){fetch(`/api/job/${JOB}/stop`,{method:"POST"})}

function parseEv(l){const s=l.s;const t=tagOf(s);let m;
 if(t==="runner")return{k:"info",i:"🚀",h:"Started",d:esc(s.replace(/^\[runner\] /,""))};
 if(t==="spec"){m=s.match(/expected=(['"])(.*)\1 symbols=\[(.*?)\] kind=(\S+)/);
  return{k:"spec",i:"🧠",h:"Understood the bug report",d:m?`<b>Expected:</b> ${esc(m[2])}<div class="chips">${m[3].split(",").filter(x=>x.trim()).map(x=>`<span class="chip">${esc(x.trim().replace(/^'|'$/g,""))}</span>`).join("")}<span class="tag info">${esc(m[4])}</span></div>`:esc(s)}}
 if(t==="explore")return{k:"search",i:"🔎",h:"Searched the code base",d:`<div class="chips">${s.slice(10).split(", ").map(x=>`<span class="chip">${esc(x)}</span>`).join("")}</div>`};
 if(t==="relax")return{k:"test",i:"✂︎",h:"Dropped invented checks",d:esc(s.replace(/^\[relax\] /,""))};
 if(t==="attempt"){m=s.match(/\[attempt (\d+)\] exec=(\S+) exc=(\S+) -> (\S+)/);const ok=m&&m[4]==="reproduced";
  return{k:"test",i:"🧪",h:`Reproduction test · attempt ${m?m[1]:""}`,d:m?`ran: <b>${esc(m[2])}</b>${m[3]!=="None"?` · raised <span class="mono">${esc(m[3])}</span>`:""} → <span class="tag ${ok?"ok":"bad"}">${esc(m[4])}</span>${ok?" the test fails for the reason the report describes":""}`:esc(s)}}
 if(t==="f2p"){m=s.match(/passes-on-fixed=(\S+) valid-by-meaning=(\S+)/);const ok=m&&(m[1]==="True"||m[2]==="True");
  return{k:"test",i:"⚖︎",h:"Oracle check against the developers' fix",d:m?`fails on buggy ✓ · passes on fixed: <span class="tag ${m[1]==="True"?"ok":"bad"}">${m[1]}</span> · valid by meaning: <span class="tag ${m[2]==="True"?"ok":"bad"}">${m[2]}</span>`:esc(s)}}
 if(t==="localize"||t==="re-localize")return{k:"loc",i:"📍",h:t==="localize"?"Fault localisation":"Re-localised on the patched code",d:/\(\d/.test(s)&&!/failing repro/.test(s)?`<div class="chips">${s.replace(/^\[[^\]]+\] /,"").split(", ").slice(0,8).map(x=>`<span class="chip">${esc(x)}</span>`).join("")}</div>`:esc(s.replace(/^\[[^\]]+\] /,""))};
 if(t==="regression")return{k:"loc",i:"🛡",h:"Existing tests used as a safety net",d:esc(s.replace(/^\[regression tests\] /,""))};
 if(t==="patch"){m=s.match(/\[patch (\d+)\] -> (\S+)(.*)/);const ok=m&&/fixed|accepted|partial/.test(m[2]);
  return{k:"patch",i:"🩹",h:`Patch · attempt ${m?m[1]:""}`,d:m?`<span class="tag ${ok?"ok":"bad"}">${esc(m[2])}</span> <span class="mono muted">${esc(m[3].trim())}</span>`:esc(s)}}
 if(t==="sweep")return{k:"patch",i:"🧹",h:"Consistency sweep",d:esc(s.replace(/^\[sweep\] /,""))};
 if(t==="review")return{k:"ctl",i:"👀",h:"Independent review",d:esc(s.replace(/^\[review\] /,""))};
 if(t==="control")return{k:"ctl",i:"🔁",h:"Loop controller",d:esc(s.replace(/^\[control\] /,""))};
 if(t==="blackboard")return null;
 if(t==="result")return null;
 return{k:"raw",raw:s}}

function stageStrip(running){const seen=new Set(lines.map(l=>tagOf(l.s)).filter(Boolean));let last=-1;STAGES.forEach((s,i)=>{if(s[2].some(t=>seen.has(t)))last=i});
 const res=jobState?.result;return STAGES.map((s,i)=>{let c=s[2].some(t=>seen.has(t))?"done":"";if(running&&i===last)c="active";
  if(i===7&&res){c=res.error?"bad":res.resolved||res.fix_status==="fixed"?"good":res.verdict==="reproduced"?"done":"bad"}
  return `<div class="stage ${c}"><b>${s[0]}</b>${s[1]}</div>`}).join("")}

const diff=d=>esc(d||"").split("\n").map(l=>l.startsWith("+")&&!l.startsWith("+++")?`<span class="add">${l}</span>`:l.startsWith("-")&&!l.startsWith("---")?`<span class="del">${l}</span>`:l).join("\n");

function renderJob(){const s=jobState,sp=s.spec||{},running=s.status==="running";const t0=lines.find(l=>l.t)?.t||s.started;
 const evs=[],raw=[];for(const l of lines){const e=parseEv(l);if(!e)continue;if(e.k==="raw"){raw.push(l.s);continue}e.t=l.t?fmt(l.t-s.started):"";evs.push(e)}
 const res=s.result;const pill=running?`<span class="pill run"><span class="spin"></span> running · ${fmt(s.now-s.started)}</span>`:
  res?.error?`<span class="pill bad">${esc(s.status==="stopped"?"stopped":"error")}</span>`:`<span class="pill ok">finished${res?.wall_s?" · "+fmt(res.wall_s):""}</span>`;
 let h=`<div class="prompt"><div class="pt">${esc(sp.title)}</div><div class="muted mono" style="font-size:11px">${esc(sp.bug_id||sp.repo||"")} · config ${esc(sp.config)}${sp.fix?" · fix":""}</div><div class="pb">${esc(sp.body)}</div></div>`;
 h+=`<div class="card" style="display:grid;gap:12px"><div class="statusbar">${pill}<div class="row" style="flex:0 0 auto;gap:6px">${running?`<button class="danger" onclick="stopJob()">■ Stop</button>`:`<button onclick="rerun()">↻ Edit & rerun</button>`}</div></div><div class="stages">${stageStrip(running)}</div></div>`;
 h+=`<div class="tl">${evs.map(e=>`<div class="ev k-${e.k}"><div class="ico">${e.i}</div><div class="evb"><div class="evh"><span class="evt">${e.h}</span><span class="evtime">${e.t?"+"+e.t:""}</span></div><div class="evd">${e.d}</div></div></div>`).join("")}</div>`;
 if(running)h+=`<div class="thinking"><span class="dots"><span></span><span></span><span></span></span> ${thinkingText()}</div>`;
 if(res)h+=resultCard(res,s.detail);
 if(raw.length)h+=`<details><summary class="muted">Raw output (${raw.length} lines)</summary><div class="raw">${esc(raw.join("\n"))}</div></details>`;
 const atBottom=window.innerHeight+window.scrollY>=document.body.scrollHeight-80;
 $("runView").innerHTML=h;if(running&&atBottom)window.scrollTo(0,document.body.scrollHeight)}

function thinkingText(){const t=[...lines].reverse().map(l=>tagOf(l.s)).find(Boolean);
 return({runner:"reading the bug report with the local model…",spec:"searching the code base…",explore:"writing a reproduction test and running it…",relax:"running the test…",
 attempt:"analysing the result and deciding the next step…",f2p:"localising the fault…",localize:"writing a patch…",regression:"writing a patch…",patch:"validating the patch with the tests…",
 control:"trying a different strategy…",review:"finishing up…",sweep:"checking similar code…"})[t]||"working…"}

function resultCard(r,d){if(r.error&&!d)return `<div class="errbox"><b>${esc(r.error)}</b></div>`;
 const v=[["Reproduced",r.verdict==="reproduced"?"yes ✓":esc(r.verdict||"–")],["Valid test (oracle)",r.fail_to_pass==null?"not checked":r.fail_to_pass?"yes ✓":"no"],
 ["Patch",r.fix_status==null?"–":r.fix_status==="fixed"?"own tests pass ✓":esc(r.fix_status)],["Hidden tests",r.resolved==null?"not graded":r.resolved?"resolved ✓":"not resolved"],["Model calls",r.llm_calls??"–"]];
 let h=`<div class="card"><h2 style="font:600 17px var(--disp);margin:0 0 10px">Result</h2>${r.error?`<div class="errbox" style="margin-bottom:10px">${esc(r.error)}</div>`:""}<div class="verdict">${v.map(([k,x])=>`<div class="vk"><span>${k}</span><b>${x}</b></div>`).join("")}</div>`;
 if(d&&!d.missing){const tabs=[];
  if(d.final_test)tabs.push(["Reproduction test",`<pre>${esc(d.final_test)}</pre>`]);
  if(d.fix?.final_diff)tabs.push(["Final patch",`<pre>${diff(d.fix.final_diff)}</pre>`]);
  if(d.attempts?.length)tabs.push(["All test attempts",d.attempts.map(a=>`<details><summary><b>#${a.n}</b> ${esc(a.status)} · ${esc(a.exception||"no exception")} → ${esc(a.category)}</summary>${a.feedback?`<div style="font-size:12px;margin:4px 0">${esc(a.feedback)}</div>`:""}<pre>${esc(a.code)}</pre></details>`).join("")]);
  if(d.fix?.patches?.length)tabs.push(["All patches",d.fix.patches.map(p=>`<details><summary><b>#${p.n}</b> → ${esc(p.category)}${p.accepted?" ✓":""}</summary>${p.feedback?`<div style="font-size:12px;margin:4px 0">${esc(p.feedback)}</div>`:""}<pre>${diff(p.diff)}</pre></details>`).join("")]);
  if(d.spec)tabs.push(["Bug spec",`<div><b>Expected:</b> ${esc(d.spec.expected)}</div><div><b>Observed:</b> ${esc(d.spec.observed)}</div><div class="muted">kind ${esc(d.spec.failure_kind)} · symbols ${esc((d.spec.symbols||[]).join(", "))}</div>`]);
  if(tabs.length){window._tabs=tabs;h+=`<div class="rtabs">${tabs.map((t,i)=>`<button class="tab ${i===0?"on":""}" onclick="showTab(${i},this)">${t[0]}</button>`).join("")}</div><div id="tabBody">${tabs[0][1]}</div>`}}
 return h+`</div>`}
function showTab(i,el){$("tabBody").innerHTML=window._tabs[i][1];[...el.parentNode.children].forEach(x=>x.classList.toggle("on",x===el))}

function rerun(){const sp=jobState.spec;newRun();target=sp.target;[...$("targets").children].forEach(x=>x.classList.toggle("on",x.dataset.t===target));renderPick();
 setTimeout(()=>{if(sp.bug_id&&$("bug")){if(target==="swebench"&&$("bugq")){$("bugq").value=sp.bug_id;$("bugq").oninput({target:$("bugq")})}$("bug").value=sp.bug_id}
  if(target==="folder"){$("repo").value=sp.repo||"";$("fixedp").value=sp.fixed||""}
  $("title").value=sp.title;$("body").value=sp.body;cfg=sp.config;[...$("cfgs").children].forEach(x=>x.classList.toggle("on",x.dataset.c===cfg));$("fix").checked=sp.fix;$("body").focus()},0)}

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
