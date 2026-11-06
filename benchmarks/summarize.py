"""Summarise per-bug result JSONs into Markdown tables for the report.

usage: python benchmarks/summarize.py results/pilot
"""
import json
import sys
from collections import defaultdict
from pathlib import Path


def yn(v):
    return {True: "✓", False: "✗", None: "–"}[v]


def main(d):
    rows = [json.loads(p.read_text()) for p in sorted(Path(d).glob("*_*.json")) if not p.name.startswith("eval_")]
    print("| bug | mode | repro | valid (harness) | fail-to-pass | fix | regr-free | dev test | attempts R/F | time s | LLM calls |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['bug_id']} | {r['mode']} | {r['status']} | {yn(r['repro_valid'])} | {yn(r['fail_to_pass'])} | "
              f"{r['fix_status']} | {yn(r['regression_free'])} | {yn(r.get('gt_pass'))} | "
              f"{r['repro_attempts']}/{r['fix_attempts']} | {r['seconds']} | {r['llm'].get('calls')} |")
    agg = defaultdict(list)
    for r in rows:
        agg[r["mode"]].append(r)
    print("\n| mode | n | harness-valid repro | true repro (f2p) | fixed | fix passes dev test | mean time s |")
    print("|---|---|---|---|---|---|---|")
    for m, rs in agg.items():
        n = len(rs)
        c = lambda f: sum(1 for r in rs if f(r))
        print(f"| {m} | {n} | {c(lambda r: r['repro_valid'])} | {c(lambda r: r['fail_to_pass'])} | "
              f"{c(lambda r: r['fix_status'] == 'fixed')} | {c(lambda r: r.get('gt_pass'))} | "
              f"{sum(r['seconds'] for r in rs) / n:.0f} |")


if __name__ == "__main__":
    main(sys.argv[1])
