"""Live dashboard for a night run: watch every bug move through the pipeline in the browser.

    .venv-autose/bin/python scripts/live_dashboard.py --tag night1      # then open http://127.0.0.1:8765

Read-only: it only reads runs/<tag>.stdout, runs/<tag>.log, runs/<tag>_<cfg>/summary.jsonl and the
per-bug *.full.json reports, plus `docker ps`. Safe to start/stop at any time while the run is going.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
SUBSET = ROOT / "benchmarks/swe_bench_lite/subset.json"

TS = re.compile(r"^(\d\d:\d\d:\d\d) (.*)$")
DONE = re.compile(r"^\[(\d+)/(\d+)\] (\S+) (\w+): verdict=(\S+) f2p_valid=(\S+) fix=(\S+) resolved=(\S+)")
START = re.compile(r"^start: configs \[(.*?)\] on (\d+) bugs, seed (\d+), model (\S+);")


def clock_to_epoch(hms: str, now: datetime) -> float:
    t = datetime.combine(now.date(), datetime.strptime(hms, "%H:%M:%S").time())
    if t > now + timedelta(minutes=5):
        t -= timedelta(days=1)
    return t.timestamp()


def read_lines(p: Path) -> list[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def containers() -> list[dict]:
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("autose-"):
            rows.append({"name": parts[0], "status": parts[1], "image": parts[2].split("/")[-1]})
    return rows


def pid_alive(tag: str) -> tuple[int | None, bool]:
    try:
        pid = int((RUNS / f"{tag}.pid").read_text().strip())
    except Exception:
        return None, False
    try:
        os.kill(pid, 0)
        return pid, True
    except OSError:
        return pid, False


def state(tag: str) -> dict:
    now = datetime.now()
    stdout = read_lines(RUNS / f"{tag}.stdout") or read_lines(RUNS / f"{tag}.log")
    start = {}
    configs: list[str] = []
    blocks: dict[str, list[str]] = {}
    system: list[dict] = []
    pending: list[str] = []
    last_ts = None
    for line in stdout:
        m = TS.match(line)
        if not m:
            if line.strip():
                pending.append(line)
            continue
        hms, msg = m.groups()
        last_ts = clock_to_epoch(hms, now)
        d = DONE.match(msg)
        s = START.match(msg)
        if d:
            key = f"{d.group(3)}|{d.group(4)}"
            blocks[key] = pending + [line]
            pending = []
        else:
            if s:
                configs = [c.strip().strip("'\"") for c in s.group(1).split(",")]
                start = {"at": last_ts, "n": int(s.group(2)), "seed": int(s.group(3)), "model": s.group(4)}
            system.append({"at": last_ts, "msg": msg})

    bugs = json.loads(SUBSET.read_text()) if SUBSET.exists() else []
    if start.get("n") and len(bugs) > start["n"]:
        bugs = bugs[:start["n"]]
    if not configs:
        configs = sorted(p.name.split("_", 1)[1] for p in RUNS.glob(f"{tag}_*") if p.is_dir())
    cells: dict[str, dict] = {}
    for c in configs:
        for line in read_lines(RUNS / f"{tag}_{c}" / "summary.jsonl"):
            if line.strip():
                r = json.loads(line)
                cells[f"{r['instance_id']}|{c}"] = {k: r.get(k) for k in (
                    "verdict", "fail_to_pass", "fix_status", "resolved", "wall_s", "error", "attempts",
                    "llm_calls", "f2p", "p2p", "control_switches", "control_stops")}

    pid, alive = pid_alive(tag)
    current = None
    order = [(b, c) for b in bugs for c in configs]
    todo = [(b, c) for b, c in order if f"{b}|{c}" not in cells]
    if alive and todo:
        b, c = todo[0]
        current = {"iid": b, "cfg": c, "index": bugs.index(b) + 1, "since": last_ts, "lines": pending}
    walls = [v["wall_s"] for v in cells.values() if v.get("wall_s")]
    eta = (sum(walls) / len(walls)) * len(todo) if walls and todo else None
    return {
        "tag": tag, "pid": pid, "alive": alive, "now": now.timestamp(), "start": start,
        "configs": configs, "bugs": bugs, "cells": cells, "blocks": blocks, "current": current,
        "system": system[-12:], "containers": containers() if alive else [],
        "free_gb": round(shutil.disk_usage("/").free / 1e9, 1), "eta_s": eta,
        "done": len(cells), "total": len(order),
    }


def clip(s, n=4000):
    if not isinstance(s, str):
        return s
    return s if len(s) <= n else s[:n] + f"\n… ({len(s) - n} more chars)"


def bug_detail(tag: str, iid: str, cfg: str) -> dict:
    return detail_from_path(RUNS / f"{tag}_{cfg}" / f"{iid}.full.json")


def detail_from_path(p: Path) -> dict:
    if not p.exists():
        return {"missing": True}
    d = json.loads(p.read_text())
    attempts = [{
        "n": a.get("test", {}).get("attempt"),
        "code": clip(a.get("test", {}).get("code"), 2500),
        "status": a.get("execution", {}).get("status"),
        "exception": a.get("execution", {}).get("exception_type"),
        "failure_line": clip(a.get("execution", {}).get("failure_line"), 300),
        "category": a.get("analysis", {}).get("category"),
        "feedback": clip(a.get("analysis", {}).get("feedback"), 500),
    } for a in d.get("attempts") or []]
    fix = d.get("fix") or {}
    patches = [{
        "n": a.get("patch", {}).get("attempt"),
        "kind": a.get("patch", {}).get("kind"),
        "diff": clip(a.get("patch", {}).get("diff"), 3000),
        "category": a.get("check", {}).get("category"),
        "accepted": a.get("check", {}).get("accepted"),
        "feedback": clip(a.get("check", {}).get("feedback"), 500),
    } for a in fix.get("attempts") or []]
    return {
        "spec": d.get("spec"), "verdict": d.get("verdict"), "attempts": attempts,
        "final_test": clip(d.get("final_test"), 3000), "fail_to_pass": d.get("fail_to_pass"),
        "retrieved": [f"{r.get('module', '')}.{r.get('name', '')}" if isinstance(r, dict) else str(r)
                      for r in (d.get("retrieved") or [])][:8],
        "fix": {"status": fix.get("status"),
                "candidates": [f"{c.get('file')}:{c.get('name')} ({c.get('score')}) — {c.get('why', '')}"
                               for c in fix.get("candidates") or []][:8],
                "patches": patches, "final_diff": clip(fix.get("final_diff"), 4000)} if fix else None,
        "control_log": d.get("control_log") or [],
        "time_s": d.get("total_time_s"), "llm_calls": d.get("llm_calls"),
    }


class Handler(BaseHTTPRequestHandler):
    tag = "night1"

    def log_message(self, *_):
        pass

    def send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        tag = q.get("tag", [self.tag])[0]
        if not re.fullmatch(r"[\w.-]+", tag):
            return self.send(b"bad tag", "text/plain", 400)
        try:
            if u.path == "/api/state":
                return self.send(json.dumps(state(tag)).encode(), "application/json")
            if u.path == "/api/bug":
                iid, cfg = q.get("iid", [""])[0], q.get("cfg", [""])[0]
                if not re.fullmatch(r"[\w.-]+", iid) or not re.fullmatch(r"\w+", cfg):
                    return self.send(b"bad id", "text/plain", 400)
                return self.send(json.dumps(bug_detail(tag, iid, cfg)).encode(), "application/json")
            if u.path in ("/", "/index.html"):
                return self.send(PAGE.encode(), "text/html; charset=utf-8")
        except Exception as exc:  # keep the dashboard up whatever the files look like mid-write
            return self.send(json.dumps({"error": repr(exc)}).encode(), "application/json", 500)
        self.send(b"not found", "text/plain", 404)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReproAgent Live</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root{--bg:#f5f6f8;--surface:#fff;--s2:#eef0f4;--fg:#161b26;--muted:#5d6577;--line:#d6dae3;--acc:#2a56c6;--acc-s:#e3eaff;
--res:#11703f;--fix:#2f9e5c;--val:#1c8a8a;--rep:#c08a00;--nr:#9aa1ae;--err:#c22f4a;--run:#2a56c6;
--mono:"IBM Plex Mono",ui-monospace,monospace;--body:"IBM Plex Sans",system-ui,sans-serif;--disp:"Space Grotesk",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f131b;--surface:#171d29;--s2:#1f2635;--fg:#e5e9f1;--muted:#9aa4b8;--line:#2e3749;--acc:#8eaaff;--acc-s:#1f2a4a;
--res:#4fd18b;--fix:#7fdca5;--val:#5fd0d0;--rep:#f2c14e;--nr:#5d6577;--err:#ff7f96;--run:#8eaaff;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f131b;--surface:#171d29;--s2:#1f2635;--fg:#e5e9f1;--muted:#9aa4b8;--line:#2e3749;--acc:#8eaaff;--acc-s:#1f2a4a;
--res:#4fd18b;--fix:#7fdca5;--val:#5fd0d0;--rep:#f2c14e;--nr:#5d6577;--err:#ff7f96;--run:#8eaaff;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 var(--body)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 80px;display:grid;gap:22px}
h1{font:700 clamp(24px,4vw,34px)/1.1 var(--disp);margin:0;letter-spacing:-.01em}
h2{font:600 16px var(--disp);margin:0 0 10px}
.muted{color:var(--muted)}.mono{font-family:var(--mono)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}
.head{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;justify-content:space-between}
.badge{display:inline-flex;align-items:center;gap:7px;font:600 12px var(--mono);padding:4px 10px;border-radius:99px;background:var(--s2)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--nr)}
.live .dot{background:var(--res);box-shadow:0 0 0 0 var(--res);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--res) 60%,transparent)}100%{box-shadow:0 0 0 9px transparent}}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;font:13px var(--mono);color:var(--muted)}
.meta b{color:var(--fg);font-weight:500}
.prog{height:8px;background:var(--s2);border-radius:99px;overflow:hidden;margin-top:12px}
.prog i{display:block;height:100%;background:var(--acc);transition:width .6s}
.score{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.score .row{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:8px}
.kpi{background:var(--s2);border-radius:8px;padding:8px 6px;text-align:center}
.kpi b{display:block;font:600 20px var(--disp)}.kpi span{font-size:11px;color:var(--muted)}
.now-top{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:baseline}
.now-id{font:600 18px var(--mono)}
.stages{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;margin:14px 0}
.stage{position:relative;padding:10px 6px;border-radius:8px;background:var(--s2);text-align:center;font-size:12px;color:var(--muted);border:1px solid transparent}
.stage b{display:block;font:600 13px var(--disp);color:var(--fg);opacity:.55}
.stage.done{background:var(--acc-s);color:var(--fg)}.stage.done b{opacity:1}
.stage.active{border-color:var(--acc);background:var(--acc-s);animation:glow 1.4s ease-in-out infinite}
.stage.active b{opacity:1}
.stage.bad{background:color-mix(in srgb,var(--err) 14%,transparent)}
@keyframes glow{50%{box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 30%,transparent)}}
@media (max-width:720px){.stages{grid-template-columns:repeat(4,1fr)}}
.log{background:var(--s2);border-radius:8px;padding:10px 12px;font:12px/1.6 var(--mono);max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.log .t{font-weight:600}
.t-spec{color:var(--acc)}.t-explore{color:var(--val)}.t-attempt{color:var(--rep)}.t-f2p{color:var(--val)}
.t-localize,.t-re-localize,.t-regression{color:#8b5cf6}.t-patch,.t-sweep{color:var(--fix)}.t-control,.t-review{color:var(--err)}.t-done{color:var(--res)}
.wait{display:flex;gap:10px;align-items:center;color:var(--muted);font-size:13px;padding:8px 0}
.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.ctr{font:12px var(--mono);color:var(--muted);margin-top:8px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin-bottom:10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.grid{display:grid;grid-template-columns:minmax(0,1fr);gap:2px}
.grow{display:grid;align-items:center;gap:4px;padding:2px 4px;border-radius:6px}
.grow:hover{background:var(--s2)}
.gid{font:12px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell{height:22px;border-radius:5px;border:1px solid var(--line);cursor:pointer;font:600 10px var(--mono);display:grid;place-items:center;color:#fff}
.cell.pend{background:transparent;cursor:default}
.cell.run{background:var(--run);animation:glow 1.4s infinite}
.c-res{background:var(--res);border-color:var(--res)}.c-fix{background:var(--fix);border-color:var(--fix)}
.c-val{background:var(--val);border-color:var(--val)}.c-rep{background:var(--rep);border-color:var(--rep)}
.c-nr{background:var(--nr);border-color:var(--nr)}.c-err{background:var(--err);border-color:var(--err)}
.ghead{font:600 11px var(--mono);color:var(--muted);text-align:center}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--fg);padding:0;width:min(920px,calc(100vw - 32px));max-height:88vh}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dhead{position:sticky;top:0;background:var(--surface);padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;align-items:center}
.dbody{padding:16px 18px;display:grid;gap:16px}
.dbody h3{font:600 14px var(--disp);margin:0 0 6px}
pre{margin:0;background:var(--s2);border-radius:8px;padding:10px;font:12px/1.5 var(--mono);overflow:auto;white-space:pre;max-height:320px}
.pill{display:inline-block;font:600 11px var(--mono);padding:2px 8px;border-radius:99px;background:var(--s2)}
button{font:inherit;background:var(--s2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 11px;cursor:pointer}
.diffadd{color:var(--fix)}.diffdel{color:var(--err)}
.sys{font:12px var(--mono);color:var(--muted);display:grid;gap:2px}
.err{color:var(--err)}
</style></head><body><div class="wrap">

<div class="head">
  <div><div class="muted mono" style="font-size:12px;letter-spacing:.08em">REPROAGENT · NIGHT RUN</div><h1 id="title">Live run</h1></div>
  <span id="badge" class="badge"><span class="dot"></span><span id="badgeText">connecting…</span></span>
</div>

<div class="card">
  <div class="meta" id="meta"></div>
  <div class="prog"><i id="progbar" style="width:0"></i></div>
</div>

<div class="score" id="score"></div>

<div class="card" id="nowCard">
  <div class="now-top"><div><div class="muted" style="font-size:12px">NOW RUNNING</div><div class="now-id" id="nowId">–</div></div>
  <div class="mono muted" id="nowElapsed"></div></div>
  <div class="stages" id="stages"></div>
  <div id="nowWait"></div>
  <div class="log" id="nowLog" hidden></div>
  <div class="ctr" id="ctrs"></div>
</div>

<div class="card">
  <h2>Every bug × configuration</h2>
  <div class="legend">
    <span><i class="c-res"></i>resolved (hidden tests pass)</span><span><i class="c-fix"></i>own tests accept patch</span>
    <span><i class="c-val"></i>valid repro test</span><span><i class="c-rep"></i>reproduced (not valid)</span>
    <span><i class="c-nr"></i>not reproduced</span><span><i class="c-err"></i>error</span><span><i style="background:var(--run)"></i>running</span>
  </div>
  <div class="grid" id="grid"></div>
  <p class="muted" style="font-size:12px;margin:10px 0 0">Click any finished cell to see the agent's spec, generated tests, patches and log.</p>
</div>

<div class="card"><h2>Runner events</h2><div class="sys" id="sys"></div></div>
</div>

<dialog id="dlg"><div class="dhead"><div><div class="mono" id="dTitle"></div><div id="dSub" class="muted" style="font-size:12px"></div></div><button onclick="dlg.close()">Close</button></div><div class="dbody" id="dBody"></div></dialog>

<script>
const STAGES=[["understand","Understand","bug report → spec",["spec"]],["search","Search","find code",["explore"]],
["reproduce","Reproduce","write failing test",["relax","attempt"]],["oracle","Oracle","fails buggy, passes fixed",["f2p"]],
["localize","Localize","pick function",["localize","re-localize","regression"]],["patch","Patch","edit + own tests",["patch","sweep"]],
["review","Review","control / review",["review","control"]],["grade","Grade","hidden SWE-bench tests",["done"]]];
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const tagOf=l=>{const m=l.match(/^\[([a-z\- ]+?)(?: \d+)?\]/i);if(m)return m[1].split(" ")[0].toLowerCase();if(/^\d\d:\d\d:\d\d \[\d+\/\d+\]/.test(l))return"done";return null};
const fmt=s=>{if(s==null)return"–";s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}h ${m}m`:m?`${m}m ${String(x).padStart(2,"0")}s`:`${x}s`};
const params=new URLSearchParams(location.search);const TAG=params.get("tag")||"";
let S=null,lastFetch=0;

function cls(c){if(!c)return"";if(c.error)return"c-err";if(c.resolved)return"c-res";if(c.fix_status==="fixed")return"c-fix";
 if(c.fail_to_pass)return"c-val";if(c.verdict==="reproduced")return"c-rep";return"c-nr"}
function label(c){if(!c)return"";if(c.error)return"ERR";if(c.resolved)return"✓✓";if(c.fix_status==="fixed")return"FIX";if(c.fail_to_pass)return"VAL";if(c.verdict==="reproduced")return"REP";return"–"}

function renderLog(lines){return lines.map(l=>{const t=tagOf(l);let h=esc(l);
 if(t){h=h.replace(/^(\[[^\]]+\]|\d\d:\d\d:\d\d \[\d+\/\d+\])/,m=>`<span class="t t-${t}">${m}</span>`)}return h}).join("\n")}

function stagesFrom(lines,finished){const seen=new Set(lines.map(tagOf).filter(Boolean));let last=-1;
 STAGES.forEach((s,i)=>{if(s[3].some(t=>seen.has(t)))last=i});
 const bad=new Set();lines.forEach(l=>{if(/-> (wrong_failure|not_reproduced|syntax|regression|no_failure)/.test(l)){bad.add(/\[patch|\[sweep/.test(l)?5:2)}});
 return STAGES.map((s,i)=>{const hit=s[3].some(t=>seen.has(t));let c=hit?"done":"";if(!finished&&i===last)c="active";
  if(bad.has(i)&&!(i===2&&seen.has("f2p")))c+=" bad";return `<div class="stage ${c}"><b>${s[1]}</b>${s[2]}</div>`}).join("")}

function render(){if(!S)return;const now=S.now+(Date.now()-lastFetch)/1000;
 $("title").textContent=`Live run · ${S.tag}`;document.title=`${S.done}/${S.total} · ReproAgent Live`;
 $("badge").className="badge"+(S.alive?" live":"");$("badgeText").textContent=S.alive?`running · pid ${S.pid}`:(S.done>=S.total?"finished":"not running");
 const el=S.start.at?now-S.start.at:null;
 $("meta").innerHTML=[["progress",`${S.done} / ${S.total}`],["configs",S.configs.join(" · ")],["model",S.start.model||"?"],["elapsed",fmt(el)],
  ["ETA",S.alive&&S.eta_s?fmt(S.eta_s):"–"],["disk free",`${S.free_gb} GB`]].map(([k,v])=>`<span>${k} <b>${esc(v)}</b></span>`).join("");
 $("progbar").style.width=(S.total?100*S.done/S.total:0)+"%";
 $("score").innerHTML=S.configs.map(c=>{const rows=Object.entries(S.cells).filter(([k])=>k.endsWith("|"+c)).map(([,v])=>v);
  const n=rows.length,k=(f)=>rows.filter(f).length;
  return `<div class="card"><div style="display:flex;justify-content:space-between"><h2 style="margin:0">Config ${c}</h2><span class="mono muted">${n}/${S.bugs.length}</span></div>
  <div class="row">${[["reproduced",k(r=>r.verdict==="reproduced")],["valid test",k(r=>r.fail_to_pass)],["own tests ok",k(r=>r.fix_status==="fixed")],
  ["resolved",k(r=>r.resolved)],["errors",k(r=>r.error)]].map(([l,v])=>`<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join("")}</div></div>`}).join("");

 const cur=S.current;
 if(cur){$("nowId").innerHTML=`${esc(cur.iid)} <span class="pill">config ${esc(cur.cfg)}</span> <span class="muted" style="font-size:13px">bug ${cur.index}/${S.bugs.length}</span>`;
  $("nowElapsed").textContent=cur.since?`running for ${fmt(now-cur.since)}`:"";
  $("stages").innerHTML=stagesFrom(cur.lines,false);
  if(cur.lines.length){$("nowLog").hidden=false;const box=$("nowLog");const stick=box.scrollTop+box.clientHeight>=box.scrollHeight-20;
   box.innerHTML=renderLog(cur.lines);if(stick)box.scrollTop=box.scrollHeight;$("nowWait").innerHTML=""}
  else{$("nowLog").hidden=true;$("nowWait").innerHTML=`<div class="wait"><div class="spin"></div>Agent is working. No stage lines yet. If the run started before the log-flush fix, they appear all at once when this config finishes.</div>`}
  $("ctrs").innerHTML=S.containers.length?"containers: "+S.containers.map(c=>`${esc(c.name)} (${esc(c.status)})`).join(" · "):"no agent containers right now (between steps / LLM thinking)";
 }else{$("nowId").textContent=S.done>=S.total?"All done 🎉":"Idle";$("nowElapsed").textContent="";$("stages").innerHTML=stagesFrom([],true);
  $("nowLog").hidden=true;$("nowWait").innerHTML=S.alive?"":`<div class="wait">The runner process is not running.</div>`;$("ctrs").textContent=""}

 const cols=`minmax(150px,240px) repeat(${S.configs.length},minmax(44px,70px))`;
 let g=`<div class="grow" style="grid-template-columns:${cols}"><span class="ghead" style="text-align:left">bug</span>${S.configs.map(c=>`<span class="ghead">${c}</span>`).join("")}</div>`;
 S.bugs.forEach(b=>{g+=`<div class="grow" style="grid-template-columns:${cols}"><span class="gid" title="${esc(b)}">${esc(b)}</span>`+
  S.configs.map(c=>{const v=S.cells[b+"|"+c];const run=cur&&cur.iid===b&&cur.cfg===c;
   if(run)return `<div class="cell run" title="running">…</div>`;
   if(!v)return `<div class="cell pend"></div>`;
   return `<div class="cell ${cls(v)}" data-b="${esc(b)}" data-c="${esc(c)}" title="${esc(`${v.verdict} · fix ${v.fix_status} · resolved ${v.resolved} · ${Math.round(v.wall_s||0)}s`)}">${label(v)}</div>`}).join("")+`</div>`});
 $("grid").innerHTML=g;
 $("sys").innerHTML=S.system.slice().reverse().map(s=>`<div>${new Date(s.at*1000).toLocaleTimeString()} · ${esc(s.msg)}</div>`).join("")||"–";
}

async function poll(){try{const r=await fetch("/api/state"+(TAG?"?tag="+encodeURIComponent(TAG):""),{cache:"no-store"});const j=await r.json();
 if(j.error)throw new Error(j.error);S=j;lastFetch=Date.now();render()}catch(e){$("badgeText").textContent="dashboard error: "+e.message}}
setInterval(poll,2500);setInterval(render,1000);poll();

$("grid").addEventListener("click",e=>{const el=e.target.closest(".cell[data-b]");if(el)openBug(el.dataset.b,el.dataset.c)});
const diff=d=>esc(d||"").split("\n").map(l=>l.startsWith("+")&&!l.startsWith("+++")?`<span class="diffadd">${l}</span>`:l.startsWith("-")&&!l.startsWith("---")?`<span class="diffdel">${l}</span>`:l).join("\n");
async function openBug(b,c){const v=S.cells[b+"|"+c]||{};$("dTitle").textContent=`${b} · config ${c}`;
 $("dSub").textContent=`verdict ${v.verdict} · valid test ${v.fail_to_pass} · fix ${v.fix_status} · resolved ${v.resolved} · f2p ${v.f2p??"–"} · ${Math.round(v.wall_s||0)}s`;
 $("dBody").innerHTML='<div class="wait"><div class="spin"></div>loading…</div>';dlg.showModal();
 const q=new URLSearchParams({iid:b,cfg:c});if(TAG)q.set("tag",TAG);
 const d=await (await fetch("/api/bug?"+q)).json();const log=S.blocks[b+"|"+c]||[];let h="";
 h+=`<div><h3>Pipeline</h3><div class="stages" style="margin:0">${stagesFrom(log,true)}</div></div>`;
 if(log.length)h+=`<div><h3>Agent log</h3><div class="log">${renderLog(log)}</div></div>`;
 if(v.error)h+=`<div><h3 class="err">Error</h3><pre>${esc(v.error)}</pre></div>`;
 if(!d.missing){const sp=d.spec||{};
  h+=`<div><h3>1 · Bug understood as</h3><div><b>Expected:</b> ${esc(sp.expected)}</div><div><b>Observed:</b> ${esc(sp.observed)}</div><div class="muted">kind ${esc(sp.failure_kind)} · symbols ${esc((sp.symbols||[]).join(", "))}</div></div>`;
  if(d.retrieved?.length)h+=`<div><h3>2 · Code search found</h3><div class="mono" style="font-size:12px">${d.retrieved.map(esc).join("<br>")}</div></div>`;
  h+=`<div><h3>3 · Reproduction attempts</h3>${d.attempts.map(a=>`<details ${a===d.attempts[d.attempts.length-1]?"open":""}><summary><span class="pill">#${a.n}</span> ${esc(a.status)} · ${esc(a.exception||"no exception")} → <b>${esc(a.category)}</b></summary>
   ${a.failure_line?`<div class="muted mono" style="font-size:12px;margin:4px 0">${esc(a.failure_line)}</div>`:""}${a.feedback?`<div style="font-size:12px;margin:4px 0">${esc(a.feedback)}</div>`:""}<pre>${esc(a.code)}</pre></details>`).join("")}</div>`;
  h+=`<div><h3>4 · Oracle check</h3>Fails on buggy code and passes on fixed code: <b>${d.fail_to_pass?"yes ✓":"no"}</b></div>`;
  if(d.fix){h+=`<div><h3>5 · Fault localisation</h3><div class="mono" style="font-size:12px">${d.fix.candidates.map(esc).join("<br>")||"–"}</div></div>`;
   h+=`<div><h3>6 · Patches (status: ${esc(d.fix.status)})</h3>${d.fix.patches.map(p=>`<details><summary><span class="pill">#${p.n}</span> ${esc(p.kind||"patch")} → <b>${esc(p.category)}</b>${p.accepted?" ✓":""}</summary>${p.feedback?`<div style="font-size:12px;margin:4px 0">${esc(p.feedback)}</div>`:""}<pre>${diff(p.diff)}</pre></details>`).join("")||"–"}</div>`;
   if(d.fix.final_diff)h+=`<div><h3>Final patch</h3><pre>${diff(d.fix.final_diff)}</pre></div>`}
  if(d.control_log?.length)h+=`<div><h3>Loop controller</h3><div class="mono" style="font-size:12px">${d.control_log.map(c=>esc(`${c.stage}: ${c.action} — ${c.fingerprint}`)).join("<br>")}</div></div>`;
  h+=`<div><h3>7 · Grade</h3>Hidden SWE-bench tests: <b>${v.resolved?"resolved ✓":v.resolved===false?"not resolved":"not graded"}</b> ${v.f2p?`· FAIL_TO_PASS ${esc(v.f2p)} · PASS_TO_PASS ${esc(v.p2p)}`:""}</div>`}
 else h+=`<div class="muted">No per-bug report file yet.</div>`;
 $("dBody").innerHTML=h}
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="night1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    Handler.tag = a.tag
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"ReproAgent live dashboard for '{a.tag}': http://127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
