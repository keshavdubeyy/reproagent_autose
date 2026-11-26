"""Trace SWE-bench Lite instances: run each bug report's code and record the functions that ran.

  python -m autose.trace --repo sympy/sympy [--skip-existing]
  python -m autose.trace --instance sympy__sympy-13177 [...]
      -> reads public/bug_reports.json, public/code/<id>, workspaces/<id>/repository_index.json
      -> writes workspaces/<id>/trace.json
Needs the repo's trace interpreter (envs/trace-sympy for sympy: Python 3.9 + mpmath), or
AUTOSE_TRACE_PYTHON.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from autose.trace import statements, trace_instance

PUBLIC = Path("benchmarks/swe_bench_lite/public")
WORKSPACES = Path("workspaces")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", nargs="*")
    ap.add_argument("--repo", nargs="*")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--deadline", type=int, default=60)
    a = ap.parse_args()
    reports = {b["instance_id"]: b for b in json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))}
    ids = list(a.instance or []) + sorted(i for i, b in reports.items() if a.repo and b["repo"] in a.repo)
    if not ids:
        ap.error("give --instance or --repo")
    for iid in ids:
        out = WORKSPACES / iid / "trace.json"
        if a.skip_existing and out.exists():
            continue
        b = reports[iid]
        index = json.loads((WORKSPACES / iid / "repository_index.json").read_text(encoding="utf-8"))
        stmts = statements(b["bug_report"])
        t = time.time()
        res = trace_instance(b["repo"], b["bug_report"], PUBLIC / "code" / iid, index, stmts, a.deadline)
        res = {"instance_id": iid, "seconds": round(time.time() - t, 1), "code": stmts} | res
        out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        ex = res.get("executed", {})
        print(f"{iid}: {res['status']} {res['seconds']}s statements={len(stmts)} ok={res.get('ok_statements')} "
              f"executed={len(ex)} new={len(set(ex) - set(res.get('baseline', [])))} frames={len(res.get('frames', []))}"
              f"{' TIMEOUT' if res.get('timed_out') else ''}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
