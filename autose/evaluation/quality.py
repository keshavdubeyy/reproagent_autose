"""Quality scorecard: every component is measured against a rubric of pass criteria.

    python -m autose.evaluation.quality --html ../quality.html          # 40-bug subset (~2 min)
    python -m autose.evaluation.quality --all --html ../quality.html    # every downloaded bug

Each criterion has a threshold, a measured value and the sample size. A component's score is the share
of its criteria that pass, and the gate is 80%. Criteria measure what each component promises:
safety rules must hold 100%, engineering checks must pass, and accuracy targets are set for what the
component can reach on its own. End-to-end results (resolved bugs) depend on the model and are tracked
but not gated. Uses the hidden fix only to score (evaluator side); writes results/quality.json.
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

from autose.agent.blackboard import SCHEMAS, patch_sha
from autose.agent.models import BugReport, BugSpec, Patch
from autose.agent.review import Reviewer, _BUILTINS
from autose.intel.flow import FunctionFlow
from autose.intel.index import build
from autose.run import PUBLIC, load_reports
from .evaluator import load_harness
from .localization import evaluate_bug

GATE = 0.80
RUN_BASED = {"R1", "T1", "B1", "B2", "L1"}    # measured on agent runs: few bugs until the night runs
SUBSET = Path("benchmarks/swe_bench_lite/subset.json")
COMPONENTS = [
    ("dataset", "Benchmark & dataset", "Bugs are valid, the fix stays hidden, the subset is reproducible."),
    ("index", "Knowledge package", "Every repository is indexed completely and the index points at real code."),
    ("search", "Search (retrieval)", "The bug report leads to the file and function the developers fixed."),
    ("flow", "Flow analysis (CFG, data flow, slicing)", "Works on every real function, fast, correct on known cases."),
    ("repro", "Reproduction (Bug Understanding)", "An accepted reproduction test is a real one: fails on the bug, passes on the fix."),
    ("review", "Review agent", "Approves correct patches, rejects broken ones."),
    ("board", "Blackboard", "Artifacts follow the schema; only tested, unrejected patches are frozen."),
    ("testing", "Testing (patch validation)", "A patch the agent accepts does not break existing behaviour."),
    ("control", "Loop controller", "No failure is repeated more than three times; decisions are deterministic."),
    ("eng", "Engineering", "Tests pass, code is covered by tests, no lint errors, decisions recorded."),
]


@dataclass
class Criterion:
    component: str
    id: str
    text: str
    threshold: str
    measured: str
    passed: bool | None          # None = could not be measured (no data yet)
    n: int | None = None
    note: str = ""


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}% ({a}/{b})" if b else "n/a"


def crit(component, cid, text, ok_ratio, threshold, a, b, note="") -> Criterion:
    if not b:
        return Criterion(component, cid, text, f"≥ {threshold:.0%}", "no data", None, 0, note)
    return Criterion(component, cid, text, f"≥ {threshold:.0%}", pct(a, b), a / b >= threshold, b, note)


# ------------------------------------------------------------------ helpers
def run_pytest() -> tuple[dict[str, tuple[int, int]], dict]:
    """Per test file (passed, total) and the coverage report."""
    tmp = Path(tempfile.mkdtemp(prefix="autose_quality_"))
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
                    f"--junitxml={tmp / 'junit.xml'}", "--cov=autose", f"--cov-report=json:{tmp / 'cov.json'}"],
                   capture_output=True, text=True)
    files: dict[str, list[int]] = {}
    for tc in ET.parse(tmp / "junit.xml").getroot().iter("testcase"):
        f = tc.get("classname", "").split(".")[1] if "." in tc.get("classname", "") else tc.get("classname", "")
        ok = not any(ch.tag in ("failure", "error") for ch in tc)
        files.setdefault(f, [0, 0])
        files[f][0] += ok
        files[f][1] += 1
    cov = json.loads((tmp / "cov.json").read_text())
    return {k: tuple(v) for k, v in files.items()}, cov


def unit(results, component, cid, text, files) -> Criterion:
    a = sum(results.get(f, (0, 0))[0] for f in files)
    b = sum(results.get(f, (0, 0))[1] for f in files)
    return crit(component, cid, text, None, 1.0, a, b, "unit tests: " + ", ".join(files))


def run_rows(run_dirs: list[Path]) -> list[tuple[Path, dict, dict | None]]:
    out = []
    for d in run_dirs:
        if not (d / "summary.jsonl").exists():
            continue
        for line in (d / "summary.jsonl").read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                f = sorted(d.glob(f"{row['instance_id']}.*.json"))
                out.append((d, row, json.loads(f[0].read_text()) if f else None))
    return out


def apply_gold(repo: Path, patch: str) -> dict[str, str] | None:
    """New contents of the files a patch changes (applied in a scratch folder)."""
    rels = re.findall(r"^diff --git a/(\S+) b/", patch, re.M)
    tmp = Path(tempfile.mkdtemp(prefix="autose_gold_"))
    for rel in rels:
        src = repo / rel
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            (tmp / rel).write_bytes(src.read_bytes())
    p = subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=tmp, capture_output=True)
    if p.returncode != 0:
        return None
    return {rel: (tmp / rel).read_text(encoding="utf-8", errors="replace") for rel in rels if (tmp / rel).exists()}


def added_lines(patch: str) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    cur, new = None, 0
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            cur = re.search(r" b/(\S+)$", line).group(1)
            out.setdefault(cur, set())
        elif line.startswith("@@"):
            new = int(re.search(r"\+(\d+)", line).group(1))
        elif cur is None or line.startswith(("---", "+++")):
            continue
        elif line.startswith("+"):
            out[cur].add(new)
            new += 1
        elif not line.startswith("-"):
            new += 1
    return out


def typo_mutant(new_src: str, lines: set[int], vocab: set[str]) -> str | None:
    """Rename one name the patch's added lines READ (variable, attribute or keyword) to a typo."""
    try:
        tree = ast.parse(new_src)
    except SyntaxError:
        return None
    src_lines = new_src.splitlines(keepends=True)
    spots = []
    for n in ast.walk(tree):
        if getattr(n, "lineno", None) not in lines:
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in _BUILTINS and n.id in vocab:
            spots.append((n.lineno, n.col_offset, n.id))
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.attr in vocab \
                and n.end_lineno == n.lineno:
            spots.append((n.lineno, n.end_col_offset - len(n.attr), n.attr))
        elif isinstance(n, ast.keyword) and n.arg and n.arg in vocab:
            spots.append((n.lineno, n.col_offset, n.arg))
    for ln, col, name in sorted(spots):
        line = src_lines[ln - 1]
        if line[col:col + len(name)] == name:
            src_lines[ln - 1] = line[:col] + name + "_zq" + line[col + len(name):]
            return "".join(src_lines)
    return None


# ------------------------------------------------------------------ the rubric
def evaluate(ids: list[str], run_dirs: list[Path], quick: bool) -> list[Criterion]:
    reports, harness = load_reports(), load_harness()
    validation = json.loads(Path("results/benchmark_validation.json").read_text())["bugs"]
    tests, cov = run_pytest()
    C: list[Criterion] = []

    # --- dataset
    n_valid = sum(1 for v in validation.values() if v.get("status") == "VALID")
    C.append(crit("dataset", "D1", "Benchmark bugs validated (fail on the bug, pass with the fix)", None, 0.95,
                  n_valid, len(reports)))
    leak_keys = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text"}
    leaks = sum(1 for r in reports.values() if leak_keys & set(r) or {"RootCause", "Title"} & set(r["bug_report"]))
    C.append(crit("dataset", "D2", "Public bug reports carry no fix, hidden tests or root cause", None, 1.0,
                  len(reports) - leaks, len(reports)))
    tracked = subprocess.run(["git", "ls-files", "benchmarks"], capture_output=True, text=True).stdout.split()
    private = [t for t in tracked if "/private/" in t]
    C.append(Criterion("dataset", "D3", "Hidden ground truth never committed to git", "0 files",
                       f"{len(private)} files", not private, len(tracked)))
    sys.path.insert(0, "scripts")
    from make_subset import select     # noqa: E402
    again, _ = select(list(harness.values()), validation)
    C.append(Criterion("dataset", "D4", "40-bug subset is reproducible from its seed", "identical",
                       "identical" if again == json.loads(SUBSET.read_text()) else "differs",
                       again == json.loads(SUBSET.read_text()), len(again)))
    clean = sum(1 for i in ids if (PUBLIC / "code" / i).exists() and not (PUBLIC / "code" / i / ".git").exists())
    C.append(crit("dataset", "D5", "Code snapshots have no git history (the fix cannot be looked up)", None, 1.0,
                  clean, len(ids)))

    # --- knowledge package
    ready = files = failed = 0
    sym_ok = sym_n = 0
    rng = random.Random(7)
    for i in ids:
        row = reports[i]
        path = build(PUBLIC / "code" / i, row["repo"], row["base_commit"])
        con = sqlite3.connect(path)
        status = con.execute("select value from meta where key='status'").fetchone()[0]
        m = json.loads(con.execute("select value from meta where key='manifest'").fetchone()[0])
        ready += status == "READY"
        files += m["files"]
        failed += m["parse_failures"]
        rows = con.execute("select path, lineno, end_lineno from symbols where kind != 'module'").fetchall()
        for p, a, b in rng.sample(rows, min(20, len(rows))):
            try:
                n_lines = len((PUBLIC / "code" / i / p).read_text(encoding="utf-8", errors="replace").splitlines())
                sym_ok += 1 <= a <= b <= n_lines
            except OSError:
                pass
            sym_n += 1
        con.close()
    C.append(crit("index", "K1", "Repositories indexed with status READY", None, 0.95, ready, len(ids)))
    C.append(crit("index", "K2", "Source files parsed into the index", None, 0.98, files - failed, files))
    C.append(crit("index", "K3", "Indexed symbols point at a real line range in their file (sampled)", None, 1.0,
                  sym_ok, sym_n))

    # --- search: the agent's default (graph) against the mid-term keyword search, same gold, same run
    found = {"graph": [0, 0], "explorer": [0, 0], "held": [0, 0]}     # [file@5, function@10]
    for i in ids:
        r = evaluate_bug(i, reports[i], harness[i], "full")
        for mode in ("graph", "explorer"):
            found[mode][0] += bool(r[mode]["file_rank"] and r[mode]["file_rank"] <= 5)
            found[mode][1] += bool(r[mode]["symbol_rank"] and r[mode]["symbol_rank"] <= 10)
        if reports[i]["repo"] == "sympy/sympy":
            found["held"][0] += bool(r["graph"]["file_rank"] and r["graph"]["file_rank"] <= 5)
            found["held"][1] += 1
    n_held = found["held"][1]
    C.append(crit("search", "S1", "Right file in the top 5", None, 0.80, found["graph"][0], len(ids)))
    C.append(crit("search", "S2", "Right file in the top 5 on sympy (never used for tuning)", None, 0.80,
                  found["held"][0], n_held))
    C.append(crit("search", "S3", "Right function in the top 10", None, 0.60, found["graph"][1], len(ids),
                  "from the bug report alone; the reproduction test's traceback adds the rest"))
    base = found["explorer"][1]
    ratio = found["graph"][1] / base if base else None
    C.append(Criterion("search", "S4", "Better than the mid-term keyword search (function in top 10)", "≥ 1.5×",
                       f"{ratio:.1f}× ({found['graph'][1]} vs {base})" if ratio else "no data",
                       ratio >= 1.5 if ratio else None, len(ids)))

    # --- flow analysis
    nf = bad = 0
    t0 = time.time()
    for i in ([x for x in ids if x.startswith("django")][:1] + [x for x in ids if x.startswith("sympy")][:1]):
        for p in (PUBLIC / "code" / i).rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, RecursionError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nf += 1
                    try:
                        FunctionFlow(node).slice_from_returns()
                    except Exception:
                        bad += 1
    ms = 1000 * (time.time() - t0) / max(nf, 1)
    C.append(crit("flow", "F1", "Real functions analysed without error (django + sympy)", None, 0.995, nf - bad, nf))
    C.append(Criterion("flow", "F2", "Speed per function (parse + CFG + data flow + slice)", "≤ 5 ms",
                       f"{ms:.2f} ms", ms <= 5, nf))
    C.append(unit(tests, "flow", "F3", "Slices correct on hand-checked programs", ["test_flow"]))

    # --- runs (reproduction, testing, blackboard, loop controller)
    rows = run_rows(run_dirs)
    acc = [(r, o) for _, r, o in rows if r.get("verdict") == "reproduced" and r.get("fail_to_pass") is not None]
    C.append(crit("repro", "R1", "Accepted reproduction tests that are valid (pass on the fixed code)", None, 0.80,
                  sum(1 for r, _ in acc if r["fail_to_pass"]), len(acc),
                  "small sample until the night runs; from " + ", ".join(d.name for d in run_dirs)))
    C.append(unit(tests, "repro", "R2", "Guards: invented error wording, wrong-reason failures",
                  ["test_repro_guards", "test_message_meaning"]))
    C.append(unit(tests, "repro", "R3", "Reproduction loop components (spec, explorer, analyzer)",
                  ["test_components"]))

    graded = [r for _, r, _ in rows if r.get("fix_status") == "fixed" and r.get("p2p")]
    no_break = sum(1 for r in graded if r["p2p"].split("/")[0] == r["p2p"].split("/")[1])
    C.append(crit("testing", "T1", "Accepted patches that keep every existing test passing (hidden P2P)", None,
                  0.80, no_break, len(graded)))
    C.append(unit(tests, "testing", "T2", "Patch validation, regression selection, multi-location repair",
                  ["test_resolution", "test_multiloc", "test_swebench_glue"]))

    frozen = backed = arts = valid_arts = 0
    for d in run_dirs:
        for bug in (d / "blackboard").glob("*") if (d / "blackboard").exists() else []:
            items = [json.loads(p.read_text()) for p in sorted(bug.glob("*.json"))]
            for it in items:
                arts += 1
                schema = SCHEMAS.get(it.get("kind"), {})
                valid_arts += bool(schema) and all(k in it and isinstance(it[k], t) for k, t in schema.items())
                if it.get("kind") == "FROZEN_PATCH":
                    frozen += 1
                    sha = it["patch_sha"]
                    backed += (patch_sha(it["diff"]) == sha
                               and any(x["kind"] == "TEST_REPORT" and x["patch_sha"] == sha and x["accepted"] for x in items)
                               and not any(x["kind"] == "REVIEW_REPORT" and x["patch_sha"] == sha
                                           and x["decision"] == "REJECT" for x in items))
    C.append(crit("board", "B1", "Frozen patches backed by an accepted test of the same hash, not rejected",
                  None, 1.0, backed, frozen))
    C.append(crit("board", "B2", "Artifacts on disk match their schema", None, 1.0, valid_arts, arts))
    C.append(unit(tests, "board", "B3", "Blackboard rules (schema, hash binding, freeze)", ["test_blackboard"]))

    logs = [o.get("control_log") for _, _, o in rows if o and o.get("control_log")]
    within = 0
    for log in logs:
        longest = cur = 0
        prev = None
        for e in log:
            cur = cur + 1 if e["fingerprint"] == prev else 1
            prev = e["fingerprint"]
            longest = max(longest, cur)
        within += longest <= 3
    C.append(crit("control", "L1", "Bugs where no failure repeated more than 3 times", None, 1.0, within, len(logs)))
    C.append(unit(tests, "control", "L2", "Fingerprints, switch and stop decisions", ["test_control"]))

    # --- review agent on the developers' own fixes and on typo mutants of them
    rev = Reviewer()
    approve = n_gold = rejected = n_mut = 0
    rep0, spec0 = BugReport("q", "", ""), BugSpec("", "", "")
    for i in ids:
        repo = PUBLIC / "code" / i
        files_new = apply_gold(repo, harness[i]["patch"])
        if not files_new:
            continue
        gold = Patch(0, "", diff=harness[i]["patch"], files=files_new, changed=[])
        n_gold += 1
        approve += rev.review(rep0, spec0, repo, gold).decision != "REJECT"
        vocab = Reviewer.vocabulary(repo)
        lines = added_lines(harness[i]["patch"])
        for rel, src in files_new.items():
            mut = typo_mutant(src, lines.get(rel, set()), vocab)
            if mut:
                n_mut += 1
                rejected += rev.review(rep0, spec0, repo, Patch(0, "", diff="m", files={**files_new, rel: mut},
                                                                changed=[])).decision == "REJECT"
                break
        if quick and n_gold >= 40:
            break
    C.append(crit("review", "V1", "Developers' real fixes approved (no false alarms)", None, 0.95, approve, n_gold))
    C.append(crit("review", "V2", "Real fixes with one injected typo rejected", None, 0.80, rejected, n_mut))
    C.append(unit(tests, "review", "V3", "Review rules and the repair-loop hand-off", ["test_review"]))

    # --- engineering
    total_ok = sum(v[0] for v in tests.values())
    total = sum(v[1] for v in tests.values())
    C.append(crit("eng", "E1", "Unit tests passing", None, 1.0, total_ok, total))
    core = [(f, d["summary"]) for f, d in cov["files"].items()
            if f.startswith(("autose/agent/", "autose/intel/"))]
    cov_l = sum(s["covered_lines"] for _, s in core)
    cov_t = sum(s["num_statements"] for _, s in core)
    C.append(crit("eng", "E2", "Line coverage of the agent and repository intelligence", None, 0.80, cov_l, cov_t,
                  "weakest: " + ", ".join(f"{Path(f).name} {s['percent_covered']:.0f}%"
                                         for f, s in sorted(core, key=lambda t: t[1]['percent_covered'])[:3])))
    lint = subprocess.run([sys.executable, "-m", "ruff", "check", "autose", "tests", "scripts", "--select", "F",
                           "--output-format", "concise", "-q"], capture_output=True, text=True)
    n_lint = len([l for l in lint.stdout.splitlines() if l.strip()])
    C.append(Criterion("eng", "E3", "Lint errors (pyflakes: unused/undefined names, dead code)", "0",
                       str(n_lint), n_lint == 0, None))
    adrs = sorted(Path("docs/adr").glob("*.md"))
    C.append(Criterion("eng", "E4", "Design decisions recorded as ADRs", "≥ 6", str(len(adrs)), len(adrs) >= 6,
                       len(adrs)))
    return C


def tracked(run_dirs: list[Path]) -> dict:
    rows = [r for _, r, _ in run_rows(run_dirs)]
    return {"bugs": len(rows), "resolved": sum(1 for r in rows if r.get("resolved")),
            "valid_reproductions": sum(1 for r in rows if r.get("fail_to_pass")),
            "patches_accepted": sum(1 for r in rows if r.get("fix_status") == "fixed")}


def summarise(C: list[Criterion]) -> list[dict]:
    out = []
    for key, name, promise in COMPONENTS:
        cs = [c for c in C if c.component == key]
        meas = [c for c in cs if c.passed is not None]
        score = sum(c.passed for c in meas) / len(meas) if meas else None
        out.append({"key": key, "name": name, "promise": promise, "score": score,
                    "passed": score is not None and score >= GATE, "criteria": [asdict(c) for c in cs]})
    return out


# ------------------------------------------------------------------ HTML
def to_html(res: dict) -> str:
    e = html.escape
    comps = res["components"]
    ok = sum(1 for c in comps if c["passed"])
    cards = []
    for c in comps:
        sc = "–" if c["score"] is None else f"{100 * c['score']:.0f}%"
        cls = "na" if c["score"] is None else "ok" if c["passed"] else "bad"
        rows = "".join(
            f"<tr><td><b>{e(x['id'])}</b> {e(x['text'])}{'<br><small>' + e(x['note']) + '</small>' if x['note'] else ''}"
            f"{'<br><span class=small>small sample: confirm on the night runs</span>' if x['id'] in RUN_BASED and (x['n'] or 0) < 10 else ''}</td>"
            f"<td class='num'>{e(x['threshold'])}</td><td class='num'>{e(x['measured'])}</td>"
            f"<td class='c {'na' if x['passed'] is None else 'ok' if x['passed'] else 'bad'}'>"
            f"{'·' if x['passed'] is None else '✓' if x['passed'] else '✗'}</td></tr>" for x in c["criteria"])
        cards.append(f"""<article class="card {cls}"><div class="head"><div><h3>{e(c['name'])}</h3>
<p>{e(c['promise'])}</p></div><span class="score">{sc}</span></div><div class="bar"><i style="width:{0 if c['score'] is None else 100 * c['score']:.0f}%"></i><b style="left:{100 * GATE:.0f}%"></b></div>
<div class="tw"><table><thead><tr><th>Criterion</th><th>Needs</th><th>Measured</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></article>""")
    t = res["tracked"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quality Scorecard</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root{{--bg:#f6f7fa;--surface:#fff;--fg:#172033;--muted:#5a6478;--line:#c9d0dd;--acc:#2a56c6;--ok:#1f7a4a;--ok-soft:#e1f3e8;--bad:#b4233c;--bad-soft:#fbe5e9;
--display:"Space Grotesk",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#10141d;--surface:#182031;--fg:#e4e8f1;--muted:#9aa5ba;--line:#34405a;--acc:#8eaaff;--ok:#6fd69c;--ok-soft:#173325;--bad:#ff8a9c;--bad-soft:#3d1c24;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#10141d;--surface:#182031;--fg:#e4e8f1;--muted:#9aa5ba;--line:#34405a;--acc:#8eaaff;--ok:#6fd69c;--ok-soft:#173325;--bad:#ff8a9c;--bad-soft:#3d1c24;color-scheme:dark}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 var(--body)}}
.wrap{{max-width:1100px;margin:0 auto;padding:40px 16px 72px;display:grid;gap:32px}}
h1{{font:700 clamp(28px,5vw,40px)/1.1 var(--display);margin:0}}h3{{font:600 17px var(--display);margin:0}}p{{margin:0}}
.eyebrow{{font:500 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--acc)}}.lede{{color:var(--muted);max-width:78ch}}
.nav a{{font:500 13px var(--body);background:var(--surface);border:1px solid var(--line);color:var(--fg);padding:6px 12px;border-radius:8px;text-decoration:none;margin-right:6px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.stat{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:2px}}
.stat .k{{font:500 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}.stat .v{{font:600 26px var(--display)}}.stat .d{{font-size:13px;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,500px),1fr));gap:14px}}
.card{{background:var(--surface);border:1px solid var(--line);border-top:5px solid var(--c);border-radius:10px;padding:14px 16px;display:grid;gap:10px;align-content:start;min-width:0}}
.card.ok{{--c:var(--ok)}}.card.bad{{--c:var(--bad)}}.card.na{{--c:var(--line)}}
.head{{display:flex;justify-content:space-between;gap:12px}}.head p{{color:var(--muted);font-size:13.5px}}.score{{font:700 26px var(--display);color:var(--c)}}
.bar{{position:relative;height:9px;border-radius:5px;background:var(--line)}}.bar i{{display:block;height:100%;border-radius:5px;background:var(--c)}}
.bar b{{position:absolute;top:-4px;width:2px;height:17px;background:var(--fg)}}
.tw{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13.5px;min-width:440px}}th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font:500 11px var(--mono);color:var(--muted)}}td small{{color:var(--muted)}}td.num{{font-family:var(--mono);font-size:12.5px;white-space:nowrap}}
span.small{{font:500 11px var(--mono);color:var(--bad)}}td.c{{text-align:center;font-weight:800}}td.ok{{color:var(--ok);background:var(--ok-soft)}}td.bad{{color:var(--bad);background:var(--bad-soft)}}td.na{{color:var(--muted)}}
</style></head><body><div class="wrap">
<header style="display:grid;gap:12px"><div class="nav"><a href="architecture-audit.html">← Architecture</a><a href="pipeline.html">Pipeline health</a><a href="index.html">Simple guide</a></div>
<div class="eyebrow">Measured {e(res['when'])} · {res['n_bugs']} bugs · gate {GATE:.0%}</div><h1>Quality Scorecard</h1>
<p class="lede">Every component has a rubric: criteria with a threshold, measured on real data. A component passes when at least {GATE:.0%} of its criteria pass (black tick on each bar). Safety rules must hold 100%. Re-run with <code>python -m autose.evaluation.quality --html ../quality.html</code>.</p></header>
<div class="stats"><div class="stat"><span class="k">Components passing</span><span class="v">{ok} / {len(comps)}</span></div>
<div class="stat"><span class="k">Criteria passing</span><span class="v">{res['criteria_passed']} / {res['criteria_measured']}</span></div>
<div class="stat"><span class="k">End to end (tracked, not gated)</span><span class="v">{t['resolved']} / {t['bugs']}</span><span class="d">resolved · {t['valid_reproductions']} valid reproductions · {t['patches_accepted']} patches accepted</span></div></div>
<div class="grid">{''.join(cards)}</div></div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="every downloaded bug instead of the 40-bug subset")
    ap.add_argument("--runs", nargs="*", type=Path, default=[Path("runs/e2e_full_23nov")],
                    help="agent runs made with the current code (reproduction, testing, blackboard checks)")
    ap.add_argument("--html", type=Path)
    a = ap.parse_args()
    if a.all:
        validation = json.loads(Path("results/benchmark_validation.json").read_text())["bugs"]
        ids = sorted(i for i in load_reports() if (PUBLIC / "code" / i).exists()
                     and validation.get(i, {}).get("status") == "VALID")
    else:
        ids = json.loads(SUBSET.read_text())
    C = evaluate(ids, a.runs, quick=not a.all)
    comps = summarise(C)
    meas = [c for c in C if c.passed is not None]
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "n_bugs": len(ids), "gate": GATE, "components": comps,
           "criteria_passed": sum(c.passed for c in meas), "criteria_measured": len(meas),
           "tracked": tracked(a.runs)}
    Path("results/quality.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    for c in comps:
        sc = "  –  " if c["score"] is None else f"{100 * c['score']:4.0f}%"
        print(f"{'PASS' if c['passed'] else 'FAIL'} {sc}  {c['name']}")
        for x in c["criteria"]:
            mark = "·" if x["passed"] is None else "✓" if x["passed"] else "✗"
            small = "  [small sample]" if x["id"] in RUN_BASED and (x["n"] or 0) < 10 else ""
            print(f"        {mark} {x['id']} {x['text'][:62]:<62} {x['measured']}  (needs {x['threshold']}){small}")
    if a.html:
        a.html.write_text(to_html(res), encoding="utf-8")
        print("->", a.html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
