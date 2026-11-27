"""Reproduction stage: write reproduction tests from each bug report and keep those that fail on the buggy code.

  python -m autose.repro --instance sympy__sympy-17139 [...] [--model qwen3:4b] [--skip-existing]
  python -m autose.repro --repo sympy/sympy
      -> reads public/bug_reports.json, public/code/<id>, workspaces/<id>/repository_index.json,
         agent_report.json (Layer 3) and trace.json; writes workspaces/<id>/repro.json
Layer 4 adds the kept tests to its checks and votes on candidates by how they behave on them.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from autose.agents.llm import LLM
from autose.repro.write import reproduce

PUBLIC = Path("benchmarks/swe_bench_lite/public")
WORKSPACES = Path("workspaces")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", nargs="*")
    ap.add_argument("--repo", nargs="*")
    ap.add_argument("--model", default="qwen3:4b")
    ap.add_argument("--tests", type=int, default=4, help="tests asked for per round")
    ap.add_argument("--skip-existing", action="store_true")
    a = ap.parse_args()
    bugs = {b["instance_id"]: b for b in json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))}
    ids = list(a.instance or []) + sorted(i for i, b in bugs.items() if a.repo and b["repo"] in a.repo)
    if not ids:
        ap.error("give --instance or --repo")
    for iid in ids:
        ws = WORKSPACES / iid
        out = ws / "repro.json"
        if a.skip_existing and out.exists():
            continue
        if not (ws / "agent_report.json").exists():
            print(f"{iid}: needs agent_report.json (Layer 3), skipped", flush=True)
            continue
        agent = json.loads((ws / "agent_report.json").read_text(encoding="utf-8"))
        index = json.loads((ws / "repository_index.json").read_text(encoding="utf-8"))
        tpath = ws / "trace.json"
        trace = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else None
        llm = LLM(model=a.model)
        t = time.time()
        print(f"{iid}:", flush=True)
        try:
            res = reproduce(bugs[iid]["repo"], bugs[iid]["bug_report"], agent, index, PUBLIC / "code" / iid, llm,
                            trace, a.tests, log=lambda m: print(m, flush=True))
        except Exception as e:
            res = {"status": "error", "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc(),
                   "kept": [], "rejected": []}
        del index
        out.write_text(json.dumps({"instance_id": iid, "model": a.model} | res, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        print(f"{iid}: {res['status']} kept={len(res['kept'])} rejected={len(res['rejected'])} {time.time() - t:.0f}s",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
