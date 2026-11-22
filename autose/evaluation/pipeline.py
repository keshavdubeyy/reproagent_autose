"""Pipeline health: how well each component works, and whether its output reaches the next one intact.

For every bug in a run (runs/<name>/), the agent's saved outcome is checked against the developers' fix
(evaluator only) at ten checkpoints along the information flow:

  intake -> search (file, function) -> reproduction -> valid reproduction -> fault localiser (file,
  function) -> patch applies -> patch edits the fixed function -> agent's own tests accept -> resolved

Three views:
  stage health   share of bugs that pass each checkpoint (inside one component)
  handoffs       P(next checkpoint passes | previous one passed): is the connection working?
  first break    the first checkpoint each bug fails: where the pipeline loses most bugs

  python -m autose.evaluation.pipeline runs/e2e_test_22nov --html ../pipeline.html --js ../pipeline-data.js
Writes results/pipeline_<run>.json (ids, booleans, rates only; no patch content).
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from autose.run import PUBLIC
from .evaluator import load_harness
from .localization import gold_symbols

# key, label, architecture component
STAGES = [
    ("intake", "Report understood (actionable spec)", "Bug report intake"),
    ("search_file", "Search finds the right file", "Bug Understanding · search"),
    ("search_func", "Search finds the right function", "Bug Understanding · search"),
    ("repro", "Reproduction test fails on the buggy code", "Bug Understanding · reproduce"),
    ("repro_valid", "…and passes on the fixed code (valid)", "Testing · oracle check"),
    ("loc_file", "Fault localiser picks the right file", "Code Modification · localise"),
    ("loc_func", "Fault localiser picks the right function", "Code Modification · localise"),
    ("patch_applies", "Patch is valid Python and applies", "Code Modification · patch"),
    ("patch_gold", "Patch edits the function the developers fixed", "Code Modification · patch"),
    ("self_check", "Agent's own tests accept the patch", "Testing · validate"),
    ("resolved", "Hidden SWE-bench tests pass (resolved)", "Evaluation"),
]
# connections between components: (from, to)
HANDOFFS = [
    ("intake", "search_file", "Intake → Search"),
    ("search_file", "repro", "Search → Reproduction"),
    ("repro", "repro_valid", "Reproduction → Oracle check"),
    ("search_func", "loc_func", "Search → Fault localiser (right function)"),
    ("repro_valid", "loc_file", "Valid test → Fault localiser"),
    ("loc_file", "loc_func", "Right file → right function"),
    ("loc_func", "patch_gold", "Localiser → Patch location"),
    ("patch_applies", "self_check", "Patch → Own tests"),
    ("self_check", "resolved", "Own tests → Hidden tests (trust)"),
]
CHAIN = ["intake", "search_file", "repro", "repro_valid", "loc_file", "loc_func", "patch_applies",
         "patch_gold", "self_check", "resolved"]


def _leaf(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def check_bug(row: dict, outcome: dict | None, gold_patch: str) -> dict:
    iid = row["instance_id"]
    repo = PUBLIC / "code" / iid
    gfiles, gsyms = gold_symbols(repo, gold_patch)
    gfuncs = {(f, _leaf(n)) for f, n in gsyms}
    r: dict = {"instance_id": iid, "error": row.get("error")}
    if outcome is None:
        r.update({k: None for k, *_ in STAGES})
        r["resolved"] = row.get("resolved")
        return r
    spec = outcome.get("spec") or {}
    r["intake"] = bool(spec) and spec.get("is_actionable", True)
    got = [x.rsplit(":", 1) for x in outcome.get("retrieved") or []]
    if got:
        r["search_file"] = any(f in gfiles for f, _ in got)
        r["search_func"] = any((f, _leaf(n)) in gfuncs for f, n in got)
    else:
        r["search_file"] = r["search_func"] = None          # mode without retrieval, or older run
    r["repro"] = outcome.get("verdict") == "reproduced"
    f2p = outcome.get("fail_to_pass")
    if not r["repro"]:
        r["repro_valid"] = False
    else:
        r["repro_valid"] = None if f2p is None else bool(f2p)     # None: run without --check-f2p
    fix = outcome.get("fix")
    if fix:
        cands = [(c["file"], _leaf(c["name"])) for c in fix.get("candidates") or []]
        r["loc_file"] = any(f in gfiles for f, _ in cands)
        r["loc_func"] = any(c in gfuncs for c in cands)
        atts = fix.get("attempts") or []
        r["patch_applies"] = any(a["check"]["category"] != "not_applicable" for a in atts)
        diff = fix.get("final_diff") or ""
        if diff:
            _f, mine = gold_symbols(repo, diff)
            r["patch_gold"] = bool({(f, _leaf(n)) for f, n in mine} & gfuncs)
        else:
            r["patch_gold"] = False
        r["self_check"] = fix.get("status") == "fixed"
    else:
        for k in ("loc_file", "loc_func", "patch_applies", "patch_gold", "self_check"):
            r[k] = None
    r["resolved"] = row.get("resolved")
    if r["resolved"] is None and not (fix and fix.get("final_diff")):
        r["resolved"] = False                    # no patch produced (or no reproduction): not resolved
    r["first_break"] = next((k for k in CHAIN if r.get(k) is False), None if r.get("resolved") else "unknown")
    return r


def summarise(bugs: list[dict]) -> dict:
    stages = []
    for key, label, comp in STAGES:
        vals = [b[key] for b in bugs if b.get(key) is not None]
        stages.append({"key": key, "label": label, "component": comp, "n": len(vals), "pass": sum(vals)})
    hand = []
    for a, b, label in HANDOFFS:
        base = [x for x in bugs if x.get(a) is True and x.get(b) is not None]
        hand.append({"from": a, "to": b, "label": label, "n": len(base), "pass": sum(1 for x in base if x[b])})
    breaks: dict[str, int] = {}
    for x in bugs:
        k = x.get("first_break")
        if k:
            breaks[k] = breaks.get(k, 0) + 1
    false_conf = sum(1 for x in bugs if x.get("self_check") and x.get("resolved") is False)
    return {"n": len(bugs), "stages": stages, "handoffs": hand, "first_break": breaks,
            "false_confidence": false_conf}


def analyse(run_dir: Path) -> dict:
    harness = load_harness()
    rows = [json.loads(l) for l in (run_dir / "summary.jsonl").read_text().splitlines() if l.strip()]
    bugs = []
    for row in rows:
        iid = row["instance_id"]
        files = sorted(run_dir.glob(f"{iid}.*.json"))
        outcome = json.loads(files[0].read_text()) if files else None
        bugs.append(check_bug(row, outcome, harness[iid]["patch"]))
    return {"run": run_dir.name, **summarise(bugs), "bugs": bugs}


# ------------------------------------------------------------------ HTML view
def _cls(p: int, n: int) -> str:
    if not n:
        return "na"
    r = p / n
    return "ok" if r >= 0.7 else "nx" if r >= 0.4 else "bad"


def to_html(res: dict) -> str:
    e = html.escape
    label = {k: l for k, l, _ in STAGES}
    flow = []
    hand = {h["to"]: h for h in res["handoffs"]}
    for s in res["stages"]:
        h = hand.get(s["key"])
        if h:
            pct = f"{round(100 * h['pass'] / h['n'])}%" if h["n"] else "—"
            flow.append(f'<div class="edge {_cls(h["pass"], h["n"])}"><span class="pipe"></span>'
                        f'<span class="elab"><b>{e(h["label"])}</b> handoff: {pct} '
                        f'<small>({h["pass"]}/{h["n"]} bugs that reached the previous step)</small></span></div>')
        pct = f"{round(100 * s['pass'] / s['n'])}%" if s["n"] else "n/a"
        w = round(100 * s["pass"] / s["n"]) if s["n"] else 0
        flow.append(f'<div class="node {_cls(s["pass"], s["n"])}"><div class="nt"><span class="comp">{e(s["component"])}</span>'
                    f'<b>{e(s["label"])}</b></div><div class="nb"><div class="bar"><i style="width:{w}%"></i></div>'
                    f'<span class="pct">{pct}</span><small>{s["pass"]}/{s["n"]}</small></div></div>')
    brk = sorted(res["first_break"].items(), key=lambda t: -t[1])
    mx = max([v for _, v in brk] or [1])
    breaks = "".join(f'<div class="brow"><span>{e(label.get(k, "not graded yet"))}</span><div class="bar bad"><i style="width:{100 * v / mx:.0f}%"></i></div>'
                     f'<b>{v}</b></div>' for k, v in brk) or "<p>No breaks: every bug resolved.</p>"
    head = "".join(f"<th title='{e(l)}'>{i + 1}</th>" for i, (k, l, _) in enumerate(STAGES))
    mark = {True: '<td class="c ok">✓</td>', False: '<td class="c bad">✗</td>', None: '<td class="c na">·</td>'}
    grid = "".join(f"<tr><td>{e(b['instance_id'])}</td>" + "".join(mark[b.get(k)] for k, *_ in STAGES) + "</tr>"
                   for b in res["bugs"])
    legend = "".join(f"<li><b>{i + 1}</b> {e(l)}</li>" for i, (_, l, _) in enumerate(STAGES))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pipeline Health</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root{{--bg:#f6f7fa;--surface:#fff;--fg:#172033;--muted:#5a6478;--line:#c9d0dd;--acc:#2a56c6;
--ok:#1f7a4a;--ok-soft:#e1f3e8;--nx:#946c00;--nx-soft:#fdf1bf;--nx-line:#d4a20a;--bad:#b4233c;--bad-soft:#fbe5e9;
--display:"Space Grotesk",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#10141d;--surface:#182031;--fg:#e4e8f1;--muted:#9aa5ba;--line:#34405a;--acc:#8eaaff;
--ok:#6fd69c;--ok-soft:#173325;--nx:#f5cd4f;--nx-soft:#3a3010;--nx-line:#d9ad22;--bad:#ff8a9c;--bad-soft:#3d1c24;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#10141d;--surface:#182031;--fg:#e4e8f1;--muted:#9aa5ba;--line:#34405a;--acc:#8eaaff;
--ok:#6fd69c;--ok-soft:#173325;--nx:#f5cd4f;--nx-soft:#3a3010;--nx-line:#d9ad22;--bad:#ff8a9c;--bad-soft:#3d1c24;color-scheme:dark}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 var(--body)}}
.wrap{{max-width:1000px;margin:0 auto;padding:40px 16px 72px;display:grid;gap:40px}}
h1{{font:700 clamp(28px,5vw,40px)/1.1 var(--display);margin:0}}h2{{font:600 21px var(--display);margin:0}}p{{margin:0}}
.eyebrow{{font:500 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--acc)}}
.lede{{color:var(--muted);font-size:16px;max-width:75ch}}section{{display:grid;gap:14px;min-width:0}}
.nav a{{font:500 13px var(--body);background:var(--surface);border:1px solid var(--line);color:var(--fg);padding:6px 12px;border-radius:8px;text-decoration:none;margin-right:6px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.stat{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:2px}}
.stat .k{{font:500 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}.stat .v{{font:600 26px var(--display)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted)}}.sw{{display:inline-block;width:14px;height:14px;border-radius:3px;vertical-align:-2px;margin-right:6px}}
.flow{{display:grid;gap:0}}
.node{{display:grid;grid-template-columns:1fr 260px;gap:14px;align-items:center;background:var(--surface);border:1.5px solid var(--c);border-left:6px solid var(--c);border-radius:10px;padding:10px 14px}}
.nt{{display:grid}}.comp{{font:500 11px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}}
.nb{{display:grid;grid-template-columns:1fr 48px 44px;gap:8px;align-items:center}}.nb small{{color:var(--muted);font:12px var(--mono)}}
.pct{{font:700 15px var(--mono);color:var(--c);text-align:right}}
.bar{{height:9px;border-radius:5px;background:var(--line);overflow:hidden}}.bar i{{display:block;height:100%;background:var(--c)}}
.edge{{display:grid;grid-template-columns:40px 1fr;align-items:center;min-height:46px}}
.pipe{{justify-self:center;width:4px;height:100%;min-height:46px;background:var(--c);border-radius:2px}}
.elab{{font-size:13px;color:var(--fg)}}.elab small{{color:var(--muted)}}
.ok{{--c:var(--ok)}}.nx{{--c:var(--nx-line)}}.bad{{--c:var(--bad)}}.na{{--c:var(--line)}}
.brow{{display:grid;grid-template-columns:minmax(0,1fr) 200px 30px;gap:10px;align-items:center;font-size:14px}}.brow .bar i{{background:var(--bad)}}
.tablewrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}}
th{{font:500 11px var(--mono);color:var(--muted)}}td.c{{text-align:center;font-weight:700}}td.ok{{color:var(--ok);background:var(--ok-soft)}}td.bad{{color:var(--bad);background:var(--bad-soft)}}td.na{{color:var(--muted)}}
ol.k{{columns:2;font-size:13px;color:var(--muted);margin:0;padding-left:0;list-style:none}}@media(max-width:700px){{ol.k{{columns:1}}.node{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<header style="display:grid;gap:12px"><div class="nav"><a href="architecture-audit.html">← Architecture</a><a href="index.html">Simple guide</a></div>
<div class="eyebrow">Run: {e(res['run'])} · {res['n']} bugs</div><h1>Pipeline Health</h1>
<p class="lede">Each bug's report flows down this pipe. A box shows how many bugs pass that checkpoint (is the component working inside?). A line between boxes shows the handoff: of the bugs that passed the step above, how many also pass the next one (is the connection working?). Checked against the developers' real fix, which the agent never sees.</p>
<div class="legend"><span><span class="sw" style="background:var(--ok)"></span>≥ 70% healthy</span><span><span class="sw" style="background:var(--nx-line)"></span>40–70% weak</span><span><span class="sw" style="background:var(--bad)"></span>&lt; 40% broken</span><span><span class="sw" style="background:var(--line)"></span>not measured in this run</span></div></header>
<section><div class="stats">
<div class="stat"><span class="k">Bugs</span><span class="v">{res['n']}</span></div>
<div class="stat"><span class="k">Resolved</span><span class="v">{sum(1 for b in res['bugs'] if b.get('resolved'))}</span></div>
<div class="stat"><span class="k">False confidence</span><span class="v">{res['false_confidence']}</span><span class="k" style="text-transform:none">own tests said fixed, hidden tests disagreed</span></div>
</div></section>
<section><h2>Information flow, checkpoint by checkpoint</h2><div class="flow">{''.join(flow)}</div></section>
<section><h2>Where bugs drop out (first broken checkpoint)</h2><div style="display:grid;gap:8px">{breaks}</div></section>
<section><h2>Every bug, every checkpoint</h2><div class="tablewrap"><table><thead><tr><th>Bug</th>{head}</tr></thead><tbody>{grid}</tbody></table></div><ol class="k">{legend}</ol></section>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path, help="one or more runs/<name> folders (merged)")
    ap.add_argument("--html", type=Path, help="also write the visual report here")
    ap.add_argument("--js", type=Path, help="also write window.PIPELINE = {...} here (read by architecture-audit.html)")
    a = ap.parse_args()
    parts = [analyse(r) for r in a.runs]
    bugs = [b for p in parts for b in p["bugs"]]
    res = {"run": " + ".join(p["run"] for p in parts), **summarise(bugs), "bugs": bugs}
    out = Path("results") / f"pipeline_{'+'.join(p['run'] for p in parts)}.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    for s in res["stages"]:
        print(f"{s['label']:<48} {s['pass']:>3}/{s['n']:<3}")
    print("first break:", res["first_break"])
    if a.js:
        slim = {k: v for k, v in res.items() if k != "bugs"}
        slim["resolved"] = sum(1 for b in bugs if b.get("resolved"))
        a.js.write_text("window.PIPELINE = " + json.dumps(slim) + ";\n", encoding="utf-8")
        print("->", a.js)
    if a.html:
        a.html.write_text(to_html(res), encoding="utf-8")
        print("->", a.html)
    print("->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
