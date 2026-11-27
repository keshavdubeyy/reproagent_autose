"""Evaluate Layer 4 patches (workspaces/<id>/patch.diff) with the benchmark's hidden tests.
Evaluator-only: uses the private SWE-bench harness records, which the patcher never sees.

Each patch runs in the official SWE-bench Docker image of its bug (evaluator.py):
  resolved   every FAIL_TO_PASS and PASS_TO_PASS test passes (the published SWE-bench metric)
  applied    the patch applies to the buggy code
  f2p / p2p  hidden tests that pass, of those that should
Also reported against Layer 3: resolved among bugs whose located functions were right
(results/layer3_eval.json strict entity hit), so patching and localization errors separate.

  python -m autose.evaluation.layer4_eval [--ids ...] [--label l4_v1] [--patches runs/<saved run>] [--redo]
Needs Docker Desktop running. Writes runs/layer4/<label>/<id>/ (logs) and results/layer4_eval.json
(metrics only, no patch text).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import docker

from autose.evaluation.evaluator import evaluate, load_harness

WORKSPACES = Path("workspaces")
RUNS = Path("runs/layer4")
OUT = Path("results/layer4_eval.json")
MAX_CRASHES = 3
VALIDATION = Path("results/benchmark_validation.json")
LAYER3 = Path("results/layer3_eval.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--label", default="l4")
    ap.add_argument("--patches", default=str(WORKSPACES),
                    help="folder with <id>/patch.diff and patch_report.json (a saved run, e.g. runs/layer4_pilot_8b)")
    ap.add_argument("--redo", action="store_true", help="re-run bugs already evaluated under this label")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    src = Path(a.patches)
    ids = a.ids or sorted(p.parent.name for p in src.glob("*/patch_report.json"))
    valid = {i for i, b in json.loads(VALIDATION.read_text(encoding="utf-8"))["bugs"].items() if b["status"] == "VALID"}
    harness = load_harness()
    allruns = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    allruns = allruns if "runs" in allruns else {"runs": {}}
    results = allruns["runs"].get(a.label, {}).get("instances", {})
    client = docker.from_env(timeout=a.timeout)
    crashes = 0
    for iid in ids:
        if iid not in valid:
            print(f"{iid}: not a valid benchmark bug, skipped", flush=True)
            continue
        if iid in results and not a.redo:
            continue
        rep_path = src / iid / "patch_report.json"
        rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
        patch = (src / iid / "patch.diff").read_text(encoding="utf-8") if (src / iid / "patch.diff").exists() else ""
        if not patch.strip():
            results[iid] = {"resolved": False, "applied": None, "empty": True, "verified": rep.get("verified")}
            print(f"{iid}: no patch", flush=True)
            continue
        r = evaluate(harness[iid], patch, a.label, RUNS / a.label / iid, client, a.timeout).to_dict()
        if (r["error"] or "").startswith("crash"):
            # a Docker failure is not a result: nothing is recorded, so a re-run scores this bug again
            crashes += 1
            print(f"{iid}: NOT SCORED ({r['error'][:160]})", flush=True)
            if crashes >= MAX_CRASHES:
                print(f"stopping: {crashes} Docker failures in a row (is Docker Desktop running?)", flush=True)
                return 3
            continue
        crashes = 0
        results[iid] = {"resolved": r["resolved"], "applied": r["patch_applied"], "empty": False,
                        "f2p": f"{r['f2p_passed']}/{r['f2p_total']}", "p2p": f"{r['p2p_passed']}/{r['p2p_total']}",
                        "verified": rep.get("verified"), "error": r["error"], "seconds": r["runtime_s"]}
        print(f"{iid}: resolved={r['resolved']} applied={r['patch_applied']} f2p={results[iid]['f2p']} "
              f"p2p={results[iid]['p2p']} verified={rep.get('verified')} {r['error'] or ''}", flush=True)
        allruns["runs"][a.label] = {"patches": a.patches, "instances": results}
        OUT.write_text(json.dumps(allruns, indent=1), encoding="utf-8")
    l3 = {r["instance_id"]: r for r in json.loads(LAYER3.read_text(encoding="utf-8")).get("instances", [])} \
        if LAYER3.exists() else {}

    def strict_hit(i):
        return (l3.get(i, {}).get("strict") or {}).get("agent", {}).get("entity_hit")
    n = len(results)
    res = [i for i, r in results.items() if r["resolved"]]
    found = [i for i in results if strict_hit(i)]
    ver = [i for i, r in results.items() if r.get("verified")]
    summary = {
        "instances": n,
        "resolved": len(res), "resolved_rate": round(len(res) / n, 4) if n else 0,
        "applied": sum(1 for r in results.values() if r.get("applied")),
        "empty": sum(1 for r in results.values() if r.get("empty")),
        "layer3_found": len(found), "resolved_when_layer3_found": sum(1 for i in found if i in res),
        "verified": len(ver), "resolved_when_verified": sum(1 for i in ver if i in res),
        "resolved_when_not_verified": sum(1 for i in res if i not in ver),
    }
    allruns["runs"][a.label] = {"patches": a.patches, "summary": summary, "instances": results}
    OUT.write_text(json.dumps(allruns, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
