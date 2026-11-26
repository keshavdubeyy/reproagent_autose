"""Run the Layer 3 agents (explorer -> report -> reviewer -> problem location).

  python -m autose.agents --instance sympy__sympy-20049 [...] [--model qwen3:4b] [--skip-existing]
  python -m autose.agents --repo pallets/flask mwaskom/seaborn
      -> reads public/bug_reports.json, public/code/<id>, workspaces/<id>/repository_index.json
         and retrieval.json (and trace.json when present); writes workspaces/<id>/agent_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from autose.agents.llm import LLM
from autose.agents.pipeline import finalise_locations, run_instance
from autose.agents.tools import Tools

PUBLIC = Path("benchmarks/swe_bench_lite/public")
WORKSPACES = Path("workspaces")


def resnap(iid: str, out: Path) -> None:
    rep = json.loads(out.read_text(encoding="utf-8"))
    if rep.get("status") != "ok" or "problem_location_raw" not in rep:
        print(f"{iid}: nothing to re-snap", flush=True)
        return
    index = json.loads((WORKSPACES / iid / "repository_index.json").read_text(encoding="utf-8"))
    tools = Tools(index, PUBLIC / "code" / iid)
    rep["problem_location"] = finalise_locations(rep["problem_location_raw"], tools, rep.get("primary"),
                                                 rep.get("primary_ranked", []), rep.get("read", []),
                                                 rep.get("candidates", []), rep.get("edit_locations", []))
    out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    pl = rep["problem_location"]
    print(f"{iid}: re-snapped ({pl['source']}) methods={pl['methods']} dropped={pl['dropped']}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", nargs="*")
    ap.add_argument("--repo", nargs="*")
    ap.add_argument("--model", default="qwen3:4b")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--think", action="store_true", help="let qwen3 think before answering (slower)")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--no-trace", action="store_true", help="ignore workspaces/<id>/trace.json (execution evidence)")
    ap.add_argument("--resnap", action="store_true",
                    help="no model calls: re-apply the current location snapping to existing agent_report.json")
    a = ap.parse_args()
    reports = {b["instance_id"]: b for b in json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))}
    ids = list(a.instance or []) + sorted(i for i, b in reports.items() if a.repo and b["repo"] in a.repo)
    if not ids:
        ap.error("give --instance or --repo")
    for iid in ids:
        ws = WORKSPACES / iid
        out = ws / "agent_report.json"
        if a.resnap:
            if out.exists():
                resnap(iid, out)
            continue
        if a.skip_existing and out.exists():
            continue
        if not (ws / "repository_index.json").exists() or not (ws / "retrieval.json").exists():
            print(f"{iid}: needs repository_index.json and retrieval.json, skipped", flush=True)
            continue
        llm = LLM(model=a.model, num_ctx=a.num_ctx, think=a.think)
        index = json.loads((ws / "repository_index.json").read_text(encoding="utf-8"))
        retrieval = json.loads((ws / "retrieval.json").read_text(encoding="utf-8"))
        tpath = ws / "trace.json"
        trace = None if a.no_trace or not tpath.exists() else json.loads(tpath.read_text(encoding="utf-8"))
        t = time.time()
        try:
            result = run_instance(reports[iid]["bug_report"], index, PUBLIC / "code" / iid, retrieval, llm, trace)
            status = "ok"
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc(), "usage": llm.usage}
            status = "error"
        del index
        result = {"instance_id": iid, "model": a.model, "status": status} | result
        out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        u = result.get("usage", {})
        print(f"{iid}: {status} {time.time() - t:.0f}s primary={result.get('primary')} ({result.get('primary_source')}) "
              f"methods={result.get('problem_location', {}).get('methods')} calls={u.get('calls')} "
              f"tokens in/out={u.get('prompt_tokens')}/{u.get('output_tokens')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
