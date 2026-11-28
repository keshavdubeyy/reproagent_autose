"""Run the Layer 4 patcher on bugs that have a Layer 3 report.

  python -m autose.patch --instance sympy__sympy-20049 [...] [--model qwen3:4b] [--skip-existing]
  python -m autose.patch --repo sympy/sympy --model qwen3:14b      (profile picked from the size)
  python -m autose.patch --repo sympy/sympy --model my-model --profile slm8
  python -m autose.patch --instance ... --model qwen3:4b --mode tools   (tool calls instead of JSON)
  python -m autose.patch --repo sympy/sympy
      -> reads public/bug_reports.json, public/code/<id>, workspaces/<id>/repository_index.json,
         agent_report.json (and trace.json when present);
         writes workspaces/<id>/patch.diff and patch_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from autose.agents.llm import LLM
from autose.patch.loop import patch_instance
from autose.patch.profiles import PROFILES, for_model

PUBLIC = Path("benchmarks/swe_bench_lite/public")
WORKSPACES = Path("workspaces")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", nargs="*")
    ap.add_argument("--repo", nargs="*")
    ap.add_argument("--model", default="qwen3:4b")
    ap.add_argument("--profile", default="auto", choices=["auto", *PROFILES],
                    help="patch strategy; auto picks it from the model size in its name (default)")
    ap.add_argument("--mode", choices=["json", "tools"], help="override the profile's edit mode")
    ap.add_argument("--candidates", type=int, help="sample this many candidates and vote (JSON mode)")
    ap.add_argument("--alt-locations", type=int, help="of the candidates, how many go to other suspects (default 0)")
    ap.add_argument("--repro", action=argparse.BooleanOptionalAction, default=None,
                    help="use the reproduction stage's tests (workspaces/<id>/repro.json) in the checks")
    ap.add_argument("--line-hints", action=argparse.BooleanOptionalAction, default=None,
                    help="show the located lines that ran when the example failed (default: on)")
    ap.add_argument("--fix-rules", action=argparse.BooleanOptionalAction, default=None,
                    help="add context-engineering-v2's fix guidelines to the prompt (default: the profile's)")
    ap.add_argument("--tie-break", choices=["smallest", "size"],
                    help="order of tied candidates: the smaller patch, or the one changing more code")
    ap.add_argument("--propagate", action=argparse.BooleanOptionalAction, default=None,
                    help="also try each edit at the other identical places in the located functions")
    ap.add_argument("--hypotheses", type=int, help="one thinking call names this many root causes for the candidates")
    ap.add_argument("--rewrite-hint", action=argparse.BooleanOptionalAction, default=None,
                    help="after a search mismatch, let the retry rewrite the whole function")
    ap.add_argument("--num-ctx", type=int, help="override the profile's context window")
    ap.add_argument("--max-attempts", type=int, help="override the profile's attempts")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--plan", action="store_true", default=None,
                    help="first ask for fix steps (context-engineering-v2 style), whatever the profile says")
    a = ap.parse_args()
    prof = for_model(a.model, a.profile, num_ctx=a.num_ctx, max_attempts=a.max_attempts, plan=a.plan,
                    edit_mode=a.mode, fix_rules=a.fix_rules, candidates=a.candidates, line_hints=a.line_hints,
                    repro=a.repro, alt_locations=a.alt_locations, tie_break=a.tie_break, propagate=a.propagate,
                    hypotheses=a.hypotheses, rewrite_hint=a.rewrite_hint)
    print(f"profile {prof.name}: {prof}", flush=True)
    bugs = {b["instance_id"]: b for b in json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))}
    ids = list(a.instance or []) + sorted(i for i, b in bugs.items() if a.repo and b["repo"] in a.repo)
    if not ids:
        ap.error("give --instance or --repo")
    for iid in ids:
        ws = WORKSPACES / iid
        out, diff_out = ws / "patch_report.json", ws / "patch.diff"
        if a.skip_existing and out.exists():
            continue
        if not (ws / "agent_report.json").exists():
            print(f"{iid}: needs agent_report.json (Layer 3), skipped", flush=True)
            continue
        agent = json.loads((ws / "agent_report.json").read_text(encoding="utf-8"))
        if agent.get("status") != "ok":
            print(f"{iid}: Layer 3 failed, skipped", flush=True)
            continue
        index = json.loads((ws / "repository_index.json").read_text(encoding="utf-8"))
        tpath = ws / "trace.json"
        trace = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else None
        rpath = ws / "retrieval.json"
        retrieval = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else None
        xpath = ws / "repro.json"
        repro = json.loads(xpath.read_text(encoding="utf-8")) if xpath.exists() else None
        llm = LLM(model=a.model, num_ctx=prof.num_ctx, num_gpu=prof.num_gpu)
        t = time.time()
        print(f"{iid}:", flush=True)
        try:
            result = patch_instance(bugs[iid]["repo"], bugs[iid]["bug_report"], agent, index, PUBLIC / "code" / iid,
                                    llm, trace, log=lambda m: print(m, flush=True), profile=prof,
                                    retrieval=retrieval, repro=repro)
        except Exception as e:
            result = {"status": "error", "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc(),
                      "diff": "", "usage": llm.usage}
        del index
        result = {"instance_id": iid, "model": a.model} | result
        diff_out.write_text(result.get("diff", ""), encoding="utf-8", newline="\n")
        out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        u = result.get("usage", {})
        print(f"{iid}: {result['status']} verified={result.get('verified')} best={result.get('best')} "
              f"{time.time() - t:.0f}s calls={u.get('calls')} diff_lines={len(result.get('diff', '').splitlines())}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
