"""Simple local web UI for ReproAgent (standard library only, no extra install).

    python -m reproagent.webui            # then open http://127.0.0.1:8000

Paste a bug report (or a GitHub issue URL), give the project folder, and watch the agent reproduce and
optionally fix the bug. One run at a time (a single SLM is loaded). The project itself is never modified.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from .agent import FIX_MODES, MODES, AgentConfig, ReproAgent
from .issue_management import IssueRetrieval
from .llm import DEFAULT_MODEL, LLM, OLLAMA_URL
from .models import BugReport
from .report import save, to_markdown

ROOT = Path(__file__).resolve().parents[1]
SHIMS = ROOT / "benchmarks" / "bugsinpy" / "compat_shims"
OUT = ROOT / "results" / "webui"

JOBS: dict[str, dict] = {}
RUN_LOCK = threading.Lock()          # one agent run at a time (one model in memory)


def examples() -> list[dict]:
    """Benchmark bugs as one-click examples."""
    out = []
    for bench, manifest in (("shopcart", "bugs.json"), ("bugsinpy", "manifest.json"), ("multiloc", "bugs.json")):
        p = ROOT / "benchmarks" / bench / manifest
        if not p.exists():
            continue
        for b in json.loads(p.read_text(encoding="utf-8"))["bugs"]:
            work = ROOT / "benchmarks" / bench / "work" / b["id"]
            if b.get("excluded") or not (work / "buggy").exists():
                continue
            out.append({"id": b["id"], "bench": bench, "title": b["title"], "body": b["body"],
                        "repo": str(work / "buggy"),
                        "fixed": str(work / "fixed") if (work / "fixed").exists() else ""})
    return out


RESULTS = ROOT / "results"


def saved_index() -> list[dict]:
    """Bugs we already evaluated: which reproduction and fix modes have saved results."""
    bugs = {e["id"]: e for e in examples()}
    out = []
    for bench in ("shopcart", "bugsinpy", "multiloc"):
        rd = RESULTS / bench
        if not rd.exists():
            continue
        found: dict[str, dict] = {}
        for p in sorted(rd.glob("*.json")):
            bid, mode = p.stem.rsplit(".", 1)
            if mode in MODES:
                o = json.loads(p.read_text(encoding="utf-8"))
                found.setdefault(bid, {"repro": {}, "fix": {}})["repro"][mode] = o["verdict"]
        for p in sorted((RESULTS / "fix" / bench).glob("*.json")):
            bid, mode = p.stem.rsplit(".", 1)
            f = json.loads(p.read_text(encoding="utf-8"))
            tag = "correct" if f.get("correct") else ("plausible" if f["status"] == "fixed" else "not fixed")
            found.setdefault(bid, {"repro": {}, "fix": {}})["fix"][mode] = tag
        for bid, d in found.items():
            b = bugs.get(bid, {})
            out.append({"id": bid, "bench": bench, "title": b.get("title", ""), **d})
    return out


MANIFESTS = {"shopcart": "bugs.json", "bugsinpy": "manifest.json", "multiloc": "bugs.json"}


def summary() -> dict:
    """Headline tables of the whole evaluation, computed from the saved results."""
    repro, fix = [], []
    cells: dict = {}
    for bench, man in MANIFESTS.items():
        mp = ROOT / "benchmarks" / bench / man
        if not mp.exists():
            continue
        na = {b["id"] for b in json.loads(mp.read_text(encoding="utf-8"))["bugs"] if b.get("expected_verdict")}
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((RESULTS / bench).glob("*.json"))]
        for mode in MODES:
            rs = [r for r in rows if r["mode"] == mode and r["bug_id"] not in na]
            if not rs:
                continue
            valid = sum(bool(r["fail_to_pass"]) for r in rs)
            claimed = sum(r["verdict"] == "reproduced" for r in rs)
            meaning = sum(bool(r.get("fail_to_pass_semantic", r["fail_to_pass"])) for r in rs)
            repro.append({"bench": bench, "mode": mode, "bugs": len(rs), "valid": valid, "meaning": meaning,
                          "false_pos": claimed - valid,
                          "time": round(sum(r["total_time_s"] for r in rs) / len(rs))})
            for r in rs:
                cells.setdefault(f"{bench}/{r['bug_id']}", {})[mode] = (
                    "V" if r["fail_to_pass"] else "Vm" if r.get("fail_to_pass_semantic")
                    else "FP" if r["verdict"] == "reproduced" else "NR")
        frows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((RESULTS / "fix" / bench).glob("*.json"))]
        for r in frows:
            cells.setdefault(f"{bench}/{r['bug_id']}", {})[r["mode"]] = (
                "C" if r.get("correct") else "P" if r["status"] == "fixed" else "X")
        for r in frows:
            r["group"] = "multi-location" if bench == "multiloc" else "single-location"
        fix += frows
    fix_rows = []
    for group in ("single-location", "multi-location"):
        for mode in FIX_MODES:
            rs = [r for r in fix if r["group"] == group and r["mode"] == mode]
            if rs:
                fix_rows.append({"group": group, "mode": mode, "bugs": len(rs),
                                 "plausible": sum(r["status"] == "fixed" for r in rs),
                                 "correct": sum(bool(r.get("correct")) for r in rs),
                                 "time": round(sum(r["total_time_s"] for r in rs) / len(rs))})
    return {"repro": repro, "fix": fix_rows, "cells": cells,
            "repro_modes": list(MODES), "fix_modes": list(FIX_MODES)}


def load_saved(bench: str, bid: str, mode: str, fix_mode: str) -> dict:
    """A saved evaluation result, shaped like a finished live job (same rendering)."""
    if bench not in ("shopcart", "bugsinpy", "multiloc") or "/" in bid or "\\" in bid or mode not in MODES:
        raise ValueError("bad request")
    p = RESULTS / bench / f"{bid}.{mode}.json"
    if not p.exists():
        raise ValueError(f"No saved result for {bid} in mode {mode}")
    o = json.loads(p.read_text(encoding="utf-8"))
    o["fix"] = None
    note = ""
    if fix_mode in FIX_MODES:
        fp = RESULTS / "fix" / bench / f"{bid}.{fix_mode}.json"
        if fp.exists():
            o["fix"] = json.loads(fp.read_text(encoding="utf-8"))
            src = o["fix"].get("repro_from", "full")
            if src != mode:
                note = (f"The fix was evaluated with the reproduction test from mode `{src}`, "
                        f"not `{mode}`. Fixing always starts from an accepted reproduction.")
        else:
            note = f"No fix result for {bid}: fixing was only run on bugs the agent reproduced."
    md = (RESULTS / bench / f"{bid}.{mode}.md")
    b = next((e for e in examples() if e["id"] == bid), {})
    return {"status": "done", "saved_view": True, "outcome": o, "note": note,
            "report": {"title": b.get("title", ""), "body": b.get("body", "")},
            "markdown": md.read_text(encoding="utf-8") if md.exists() else "", "saved": str(p), "log": []}


def ollama_status() -> dict:
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
        names = [m["name"] for m in tags.get("models", [])]
        return {"ok": True, "models": names}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def run_job(job: dict, req: dict) -> None:
    log = job["log"]

    def say(s: str) -> None:
        log.append(f"[{time.strftime('%H:%M:%S')}] {s}")

    job["status"] = "queued"
    with RUN_LOCK:
        job["status"] = "running"
        try:
            repo = Path(req["repo"]).expanduser()
            if not repo.is_dir():
                raise ValueError(f"Project folder not found: {repo}")
            fixed = Path(req["fixed"]).expanduser() if req.get("fixed") else None
            if fixed is not None and not fixed.is_dir():
                raise ValueError(f"Fixed-version folder not found: {fixed}")
            if req.get("issue_url"):
                say(f"Fetching issue {req['issue_url']}")
                report = IssueRetrieval().load(req["issue_url"].strip())
            else:
                if not (req.get("title") or req.get("body")):
                    raise ValueError("Give a bug report (title/description) or a GitHub issue URL.")
                report = BugReport(req.get("id") or "webui_bug", req.get("title", ""), req.get("body", ""), "webui")
            extra = [str(SHIMS)] if "bugsinpy" in str(repo).lower() and SHIMS.exists() else None
            cfg = AgentConfig(mode=req.get("mode", "full"), budget=int(req.get("budget") or 4),
                              fix=bool(req.get("fix")), fix_mode=req.get("fix_mode", "fix_full"),
                              extra_paths=extra)
            say(f"Bug '{report.title[:80]}' | project {repo} | mode {cfg.mode}"
                + (f" | fixing ({cfg.fix_mode})" if cfg.fix else ""))
            agent = ReproAgent(LLM(req.get("model") or DEFAULT_MODEL), cfg, log=say)
            outcome = agent.run(report, repo, fixed)
            path = save(outcome, OUT)
            job["outcome"] = outcome.to_dict()
            job["markdown"] = to_markdown(outcome)
            job["saved"] = str(path)
            say(f"Done: {outcome.verdict.value}. Report saved to {path}")
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            say("ERROR: " + str(e))
            job["error"] = str(e)
            job["trace"] = traceback.format_exc()
            job["status"] = "error"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console quiet
        pass

    def _send(self, code: int, body, ctype="application/json") -> None:
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html")
        if self.path == "/api/info":
            return self._send(200, {"ollama": ollama_status(), "default_model": DEFAULT_MODEL,
                                    "modes": list(MODES), "fix_modes": list(FIX_MODES),
                                    "examples": examples(), "busy": RUN_LOCK.locked()})
        if self.path == "/api/summary":
            return self._send(200, summary())
        if self.path == "/api/saved":
            return self._send(200, saved_index())
        if self.path.startswith("/api/saved/"):
            try:
                _, _, _, bench, bid, mode, fmode = self.path.split("/")
                return self._send(200, load_saved(bench, bid, mode, fmode))
            except ValueError as e:
                return self._send(404, {"error": str(e)})
        if self.path.startswith("/api/job/"):
            job = JOBS.get(self.path.rsplit("/", 1)[-1])
            if job is None:
                return self._send(404, {"error": "unknown job"})
            return self._send(200, job)
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/run":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad JSON"})
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "status": "queued", "log": [], "started": time.time()}
        JOBS[jid] = job
        threading.Thread(target=run_job, args=(job, req), daemon=True).start()
        self._send(200, {"job": jid})


def main() -> None:
    ap = argparse.ArgumentParser(prog="reproagent.webui")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"ReproAgent web UI on http://{a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReproAgent</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#5d6678;--line:#dfe3ea;--acc:#2f5bd3;
      --ok:#1d7a46;--okbg:#e3f4ea;--bad:#b3261e;--badbg:#fbe7e6;--warn:#8a5a00;--warnbg:#fdf1d8;--code:#f1f3f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,sans-serif}
header{padding:18px 24px;background:var(--card);border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{margin:0;font-size:20px}header .sub{color:var(--mute);font-size:13px}
#ollama{margin-left:auto;font-size:13px;padding:4px 10px;border-radius:99px;background:var(--code)}
main{max-width:1200px;margin:0 auto;padding:20px 16px;display:grid;grid-template-columns:minmax(0,420px) minmax(0,1fr);gap:20px}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
h2{font-size:15px;margin:0 0 12px}h3{font-size:14px;margin:18px 0 6px}
label{display:block;font-size:13px;color:var(--mute);margin:10px 0 4px}
input,textarea,select{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
textarea{min-height:110px;resize:vertical}.row{display:flex;gap:10px}.row>div{flex:1}
.check{display:flex;gap:8px;align-items:center;margin-top:12px;color:var(--ink);font-size:14px}.check input{width:auto}
button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:6px;background:var(--acc);color:#fff;font:600 15px system-ui;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.hint{font-size:12px;color:var(--mute)}
.tabs{display:flex;gap:6px;margin-bottom:14px}.tab{margin:0;flex:1;background:var(--code);color:var(--ink);font-weight:500}
.tab.on{background:var(--acc);color:#fff}button.sec{background:var(--code);color:var(--acc);margin-top:8px}
.note{background:var(--warnbg);color:var(--warn);padding:8px 10px;border-radius:6px;font-size:13px}
pre{background:var(--code);padding:10px;border-radius:6px;overflow:auto;font:12.5px/1.45 Consolas,monospace;margin:0;white-space:pre}
#log{max-height:260px;white-space:pre-wrap}
.badge{display:inline-block;padding:4px 12px;border-radius:99px;font-weight:600;font-size:14px}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.warn{background:var(--warnbg);color:var(--warn)}
.diff .a{color:var(--ok);background:var(--okbg)}.diff .d{color:var(--bad);background:var(--badbg)}.diff .h{color:var(--acc)}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top}
.empty{color:var(--mute);text-align:center;padding:40px 0}
details summary{cursor:pointer;color:var(--acc);font-size:13px;margin-top:14px}
</style></head><body>
<header><h1>ReproAgent</h1><span class="sub">Bug report → failing test → validated patch, on a local SLM</span>
<span id="ollama">checking Ollama…</span></header>
<main>
<section class="card">
  <div class="tabs"><button id="tSaved" class="tab on">Our tested results</button><button id="tLive" class="tab">Run on a new bug</button><button id="tSum" class="tab">Evaluation summary</button></div>
  <div id="sumPane" style="display:none">
  <h2>Evaluation summary</h2>
  <div class="hint">All numbers from our saved runs (one run each, CPU, qwen2.5-coder:3b). Click any cell in the per-bug table to open that result.</div>
  <p class="hint"><b>Reproduction:</b> V = valid (fails on buggy, passes on fixed), Vm = valid by meaning only (error-message wording differs, same meaning), FP = claimed but invalid, NR = not reproduced.<br>
  <b>Fixing:</b> C = correct (hidden oracle passes), P = plausible but wrong, X = no accepted patch.</p>
  </div>
  <div id="savedPane">
  <h2>Results from our evaluation</h2>
  <div class="hint">Every bug we evaluated, with the saved reproduction and fix. Opens instantly, no model needed.</div>
  <label for="sbug">Bug</label><select id="sbug"></select>
  <div id="sdesc" class="hint" style="margin-top:6px"></div>
  <div class="row"><div><label for="smode">Reproduction mode</label><select id="smode"></select></div>
  <div><label for="sfix">Fix mode</label><select id="sfix"></select></div></div>
  <button id="sgo">Show result</button>
  <button id="srun" class="sec">Re-run this bug live</button>
  </div>
  <div id="livePane" style="display:none">
  <h2>1. Bug report and project</h2>
  <label for="ex">Load a benchmark example (optional)</label>
  <select id="ex"><option value="">— none —</option></select>
  <label for="title">Bug title</label><input id="title" placeholder="Discount is incorrectly applied to orders below ₹1,000">
  <label for="body">Description (expected vs. observed behaviour)</label>
  <textarea id="body" placeholder="What did you do, what did you expect, what happened instead?"></textarea>
  <label for="url">…or a GitHub issue URL</label><input id="url" placeholder="https://github.com/owner/repo/issues/123">
  <label for="repo">Project folder (buggy version)</label><input id="repo" placeholder="C:\path\to\project">
  <label for="fixed">Fixed version folder (optional, evaluation only)</label><input id="fixed" placeholder="leave empty for real use">
  <h2 style="margin-top:18px">2. Options</h2>
  <div class="row"><div><label for="mode">Reproduction mode</label><select id="mode"></select></div>
  <div><label for="budget">Attempts</label><input id="budget" type="number" min="1" max="8" value="4"></div></div>
  <label class="check"><input type="checkbox" id="fix" checked> Also fix the bug (after it is reproduced)</label>
  <label for="fixmode">Fix mode</label><select id="fixmode"></select>
  <label for="model">Model</label><input id="model">
  <button id="go">Run ReproAgent</button>
  <div class="hint" style="margin-top:8px">Runs on CPU: expect 1–5 minutes. Your project is never modified; the patch is shown as a diff.</div>
  </div>
</section>
<section class="card" id="out"><div class="empty">Results appear here.</div></section>
</main>
<script>
const $=id=>document.getElementById(id);let EX=[],timer=null;
const esc=s=>(s??"").toString().replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const VERD={reproduced:["REPRODUCED","ok"],not_reproduced:["NOT REPRODUCED","bad"],needs_more_information:["NEEDS MORE INFORMATION","warn"]};
function diffHtml(d){return '<pre class="diff">'+d.split("\n").map(l=>{let c=l.startsWith("+++")||l.startsWith("---")?"h":l.startsWith("+")?"a":l.startsWith("-")?"d":l.startsWith("@@")?"h":"";return `<span class="${c}">${esc(l)}</span>`}).join("\n")+"</pre>"}
async function info(){
  const r=await (await fetch("/api/info")).json();
  $("ollama").textContent=r.ollama.ok?("Ollama running · "+(r.ollama.models.includes(r.default_model)?r.default_model:"model "+r.default_model+" not pulled!")):"Ollama not reachable: start it with `ollama serve`";
  $("ollama").className=r.ollama.ok?"ok":"bad";
  $("mode").innerHTML=r.modes.map(m=>`<option ${m=="full"?"selected":""}>${m}</option>`).join("");
  $("fixmode").innerHTML=r.fix_modes.map(m=>`<option>${m}</option>`).join("");
  $("model").value=r.default_model;EX=r.examples;
  $("ex").innerHTML+=EX.map((e,i)=>`<option value="${i}">[${e.bench}] ${esc(e.id)} — ${esc(e.title.slice(0,60))}</option>`).join("");
}
$("ex").onchange=()=>{const e=EX[$("ex").value];if(!e)return;$("title").value=e.title;$("body").value=e.body;$("repo").value=e.repo;$("fixed").value=e.fixed;$("url").value=""};
$("fix").onchange=()=>$("fixmode").disabled=!$("fix").checked;
const VLAB={reproduced:"reproduced",not_reproduced:"not reproduced",needs_more_information:"needs more info"};
let SAVED=[];
function tab(w){w=w===true?"saved":w===false?"live":w;
  for(const[k,id]of[["saved","savedPane"],["live","livePane"],["sum","sumPane"]])$(id).style.display=k==w?"":"none";
  $("tSaved").classList.toggle("on",w=="saved");$("tLive").classList.toggle("on",w=="live");$("tSum").classList.toggle("on",w=="sum");
  if(w=="sum")showSummary()}
$("tSaved").onclick=()=>tab("saved");$("tLive").onclick=()=>tab("live");$("tSum").onclick=()=>tab("sum");
const CC={V:"ok",Vm:"ok",C:"ok",FP:"warn",P:"warn",NR:"bad",X:"bad"};
async function showSummary(){
  const d=await (await fetch("/api/summary")).json();
  const pct=(a,b)=>b?Math.round(100*a/b)+"%":"-";
  let h=`<h2>Bug reproduction</h2><table><tr><th>Benchmark</th><th>Mode</th><th>Bugs</th><th>Valid reproductions</th><th>Valid by meaning</th><th>False positives</th><th>Avg time</th></tr>`+
    d.repro.map(r=>`<tr><td>${r.bench}</td><td><code>${r.mode}</code></td><td>${r.bugs}</td><td><b>${r.valid} (${pct(r.valid,r.bugs)})</b></td><td>${r.meaning} (${pct(r.meaning,r.bugs)})</td><td>${r.false_pos}</td><td>${r.time} s</td></tr>`).join("")+"</table>";
  h+=`<h2 style="margin-top:22px">Bug fixing (on reproduced bugs)</h2><table><tr><th>Bug type</th><th>Mode</th><th>Bugs</th><th>Plausible</th><th>Correct</th><th>Avg time</th></tr>`+
    d.fix.map(r=>`<tr><td>${r.group}</td><td><code>${r.mode}</code></td><td>${r.bugs}</td><td>${r.plausible}</td><td><b>${r.correct} (${pct(r.correct,r.bugs)})</b></td><td>${r.time} s</td></tr>`).join("")+"</table>";
  const ms=[...d.repro_modes,...d.fix_modes].filter(m=>Object.values(d.cells).some(c=>c[m]));
  h+=`<h2 style="margin-top:22px">Per bug</h2><div style="overflow-x:auto"><table><tr><th>Bug</th>${ms.map(m=>`<th><code>${m}</code></th>`).join("")}</tr>`+
    Object.entries(d.cells).map(([k,c])=>`<tr><td>${esc(k)}</td>${ms.map(m=>c[m]?`<td><a href="#" class="badge ${CC[c[m]]}" style="font-size:12px;padding:1px 8px;text-decoration:none" data-k="${esc(k)}" data-m="${m}">${c[m]}</a></td>`:"<td>-</td>").join("")}</tr>`).join("")+"</table></div>";
  $("out").innerHTML=h;
  $("out").querySelectorAll("a[data-k]").forEach(a=>a.onclick=async ev=>{ev.preventDefault();
    const[bench,bid]=a.dataset.k.split("/"),m=a.dataset.m,c=d.cells[a.dataset.k];
    const isFix=m.startsWith("fix_"),rm=isFix?(c.full?"full":"full_v2"):m,fm=isFix?m:(c.fix_full?"fix_full":"none");
    const r=await fetch(`/api/saved/${bench}/${encodeURIComponent(bid)}/${rm}/${fm}`);const j=await r.json();
    if(r.ok){render(j);$("out").insertAdjacentHTML("afterbegin",'<p><a href="#" id="back">&larr; back to summary</a></p>');$("back").onclick=e=>{e.preventDefault();showSummary()}}});
}
const RANK={correct:3,plausible:2,"not fixed":1};
function best(s){return Object.keys(s.fix).sort((a,b)=>(RANK[s.fix[b]]||0)-(RANK[s.fix[a]]||0))[0]}
async function saved(){
  SAVED=await (await fetch("/api/saved")).json();
  $("sbug").innerHTML=SAVED.map((s,i)=>{const b=best(s);const fx=b?` · best fix: ${s.fix[b]} (${b})`:"";return `<option value="${i}">[${s.bench}] ${esc(s.id)} — repro: ${VLAB[s.repro.full]||"-"}${fx}</option>`}).join("");
  $("sbug").onchange();
}
$("sbug").onchange=()=>{const s=SAVED[$("sbug").value];if(!s)return;
  $("sdesc").textContent=s.title;
  $("smode").innerHTML=Object.entries(s.repro).map(([m,v])=>`<option value="${m}" ${m=="full"?"selected":""}>${m} (${VLAB[v]||v})</option>`).join("");
  const fx=Object.entries(s.fix);
  $("sfix").innerHTML=(fx.length?fx.map(([m,v])=>`<option value="${m}" ${m==best(s)?"selected":""}>${m} (${v})</option>`).join(""):"")+`<option value="none">${fx.length?"don't show fix":"not fixed (not reproduced)"}</option>`;
};
$("sgo").onclick=async()=>{const s=SAVED[$("sbug").value];if(!s)return;
  const r=await fetch(`/api/saved/${s.bench}/${encodeURIComponent(s.id)}/${$("smode").value}/${$("sfix").value}`);const j=await r.json();
  if(!r.ok){$("out").innerHTML=`<span class="badge bad">ERROR</span><p>${esc(j.error)}</p>`;return}render(j)};
$("srun").onclick=()=>{const s=SAVED[$("sbug").value];if(!s)return;const i=EX.findIndex(e=>e.id==s.id);
  if(i>=0){$("ex").value=i;$("ex").onchange()}$("mode").value=$("smode").value.startsWith("full")?$("smode").value:"full";
  if($("sfix").value!="none"){$("fix").checked=true;$("fixmode").value=$("sfix").value}tab(false)};
$("go").onclick=async()=>{
  const e=EX[$("ex").value];
  const req={id:e?e.id:"webui_bug",title:$("title").value,body:$("body").value,issue_url:$("url").value,repo:$("repo").value.trim(),
    fixed:$("fixed").value.trim(),mode:$("mode").value,budget:$("budget").value,fix:$("fix").checked,fix_mode:$("fixmode").value,model:$("model").value};
  $("go").disabled=true;$("out").innerHTML='<h2>Running… <span class="hint" id="el"></span></h2><h3>Agent log</h3><pre id="log"></pre>';
  const {job}=await (await fetch("/api/run",{method:"POST",body:JSON.stringify(req)})).json();
  const t0=Date.now();
  timer=setInterval(async()=>{
    const j=await (await fetch("/api/job/"+job)).json();
    if($("log")){$("log").textContent=j.log.join("\n");$("log").scrollTop=1e9;$("el").textContent=Math.round((Date.now()-t0)/1000)+" s"+(j.status=="queued"?" (waiting for another run)":"")}
    if(j.status=="done"||j.status=="error"){clearInterval(timer);$("go").disabled=false;render(j)}
  },1500);
};
function render(j){
  if(j.status=="error"){$("out").innerHTML=`<span class="badge bad">ERROR</span><p>${esc(j.error)}</p><h3>Agent log</h3><pre id="log">${esc(j.log.join("\n"))}</pre><details><summary>Traceback</summary><pre>${esc(j.trace)}</pre></details>`;return}
  const o=j.outcome,[vt,vc]=VERD[o.verdict]||[o.verdict,"warn"];let h="";
  if(j.saved_view)h+=`<div class="hint">Saved result from our evaluation · <code>${esc(o.bug_id)}</code> · mode <code>${esc(o.mode)}</code></div><h3>Bug report</h3><p><b>${esc(j.report.title)}</b><br>${esc(j.report.body)}</p>`;
  h+=`<span class="badge ${vc}">${vt}</span> <span class="hint">${o.attempts.length} attempt(s) · ${o.llm_calls} SLM calls · ${o.total_time_s}s</span>`;
  if(o.fail_to_pass!==null)h+=`<p>Fail-to-pass check on the fixed version: <b>${o.fail_to_pass?"PASS (valid reproduction)":"FAIL"}</b></p>`;
  if(!o.fail_to_pass&&o.message_checks&&o.message_checks.length)h+=`<p>Valid by meaning: <span class="badge ${o.fail_to_pass_semantic?"ok":"bad"}">${o.fail_to_pass_semantic?"YES":"NO"}</span> <span class="hint">the only failures on the fixed version are error-message wording differences</span></p><table><tr><th>Test expected</th><th>Fixed code says</th><th>Same meaning?</th><th>Decided by</th></tr>`+o.message_checks.map(c=>`<tr><td><code>${esc(c.expected)}</code></td><td><code>${esc(c.actual)}</code></td><td>${c.same?"yes":"no"}</td><td>${c.by=="slm"?"SLM judge":c.by=="report-rule"?"report rule: the message is the bug":c.by}: ${esc(c.why)}</td></tr>`).join("")+"</table>";
  if(o.spec)h+=`<h3>How the agent understood the bug</h3><table><tr><th>Expected</th><td>${esc(o.spec.expected)}</td></tr><tr><th>Observed</th><td>${esc(o.spec.observed)}</td></tr><tr><th>Symbols</th><td>${esc((o.spec.symbols||[]).join(", "))}</td></tr></table>`;
  if(o.final_test){const last=o.attempts[o.attempts.length-1];h+=`<h3>Reproduction test</h3><pre>${esc(o.final_test)}</pre><h3>Evidence: pytest output on the buggy version</h3><pre>${esc(last.execution.output.slice(-1800))}</pre>`}
  h+=`<h3>Reproduction attempts</h3><table><tr><th>#</th><th>Run</th><th>Result</th><th>Feedback</th></tr>`+o.attempts.map(a=>`<tr><td>${a.test.attempt}</td><td>${esc(a.execution.status)}</td><td>${esc(a.analysis.category)}</td><td>${esc((a.analysis.feedback||"").slice(0,220))}</td></tr>`).join("")+"</table>";
  const f=o.fix;
  if(f){const ok=f.status=="fixed";h+=`<h3 style="margin-top:24px">Bug resolution</h3><span class="badge ${ok?"ok":"bad"}">${ok?"VALIDATED PATCH FOUND":"NO VALID FIX"}</span> <span class="hint">${f.attempts.length} patch attempt(s) · ${f.total_time_s}s</span>`;
    if(f.correct!==null&&f.correct!==undefined&&ok)h+=` <span class="badge ${f.correct?"ok":"warn"}">hidden oracle: ${f.correct?"CORRECT":"plausible but WRONG"}</span>`;
    if(f.mode)h+=` <span class="hint">mode <code>${esc(f.mode)}</code></span>`;
    h+=`<p class="hint">Suspicious code (most likely first): ${f.candidates.slice(0,6).map(c=>"<code>"+esc(c.file+":"+c.name)+"</code>").join(", ")}<br>Regression tests run: ${esc(f.regression_tests.join(", ")||"(none found)")}</p>`;
    if(f.final_diff&&f.locations&&f.locations.length)h+=`<p>Changed locations (${f.locations.length}): ${f.locations.map(x=>"<code>"+esc(x)+"</code>").join(", ")}</p>`;
    if(f.final_diff)h+=`<h3>Proposed patch</h3>${diffHtml(f.final_diff)}<p class="hint">The reproduction test passes with this patch and the related existing tests did not regress. Your project was not modified.</p>`;
    h+=`<table><tr><th>Patch</th><th>Result</th><th>Feedback</th></tr>`+f.attempts.map(a=>`<tr><td>${a.patch.attempt}${a.patch.kind=="sweep"?" (sweep)":""}</td><td>${esc(a.check.category)}</td><td>${esc((a.check.feedback||"").slice(0,220))}</td></tr>`).join("")+"</table>"}
  else if(o.verdict!="reproduced"&&!j.saved_view&&$("fix").checked)h+=`<p class="hint">No fix attempted: fixing starts only after the bug is reproduced.</p>`;
  if(j.note)h+=`<p class="note">${esc(j.note)}</p>`;
  if(j.log.length)h+=`<details><summary>Agent log</summary><pre id="log">${esc(j.log.join("\n"))}</pre></details>`;
  h+=`<details><summary>Full Markdown report (saved to ${esc(j.saved)})</summary><pre>${esc(j.markdown)}</pre></details>`;
  $("out").innerHTML=h;
}
info().then(saved);
</script></body></html>
"""

if __name__ == "__main__":
    main()
