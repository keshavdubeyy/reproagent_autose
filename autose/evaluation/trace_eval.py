"""Evaluate execution traces (workspaces/<id>/trace.json) as localization evidence.
Evaluator-only: reads the gold patches, which agents must never see.

Per traced instance, with gold entities / files as in Layer 1 (innermost indexed entity around
each edited line; .py files the patch edits):
  executed   a gold entity ran when the report's code ran        (any call of it, or code inside it)
  new        ... and it did not run in the baseline (trivial use of the library)
  frame      a gold entity is in an exception traceback
  file       a gold file had code that ran (new)
  trace@k    a gold entity in the top k of the trace ranking: exception frames (innermost first),
             then new entities by BM25 similarity to the report
  layer2@k   the same for Layer 2's fused ranking (retrieval.json), for comparison
  fused@k    exception frames, then Layer 2 run only over the code that ran (retrieval_trace.json)
  either@10  gold in Layer 2 top 10 or trace top 10

  python -m autose.evaluation.trace_eval [--ids ...]
Writes results/trace_eval.json (metrics only, no gold names).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from autose.evaluation.layer1_eval import gold_metrics, patch_old_lines
from autose.retrieval.mentions import report_text
from autose.trace.rank import ranking

SPECS = Path("benchmarks/swe_bench_lite/private/test_specs.json")
PUBLIC = Path("benchmarks/swe_bench_lite/public")
WORKSPACES = Path("workspaces")
OUT = Path("results/trace_eval.json")
KS = (1, 5, 10, 20)


def evaluate(iid: str, spec: dict, report) -> dict | None:
    tpath = WORKSPACES / iid / "trace.json"
    if not tpath.exists():
        return None
    trace = json.loads(tpath.read_text(encoding="utf-8"))
    row = {"instance_id": iid, "status": trace["status"], "statements": trace.get("statements"),
           "ok_statements": trace.get("ok_statements"), "timed_out": trace.get("timed_out")}
    if trace["status"] != "ran":
        return row
    index = json.loads((WORKSPACES / iid / "repository_index.json").read_text(encoding="utf-8"))
    gold = set(gold_metrics(index, spec["gold_patch"])["gold_entities"])
    gfiles = {f for f, l in patch_old_lines(spec["gold_patch"]).items() if f.endswith(".py") and l is not None}
    file_of = {e["id"]: e["file"] for e in index["entities"]}
    order = ranking(trace, report_text(report), index, PUBLIC / "code" / iid)
    del index
    ex = set(trace["executed"])
    new = ex - set(trace["baseline"])
    inside = lambda s: any(e == g or e.startswith(g + ".") for e in s for g in gold)  # ran, or code inside it ran
    l2 = [e["id"] for e in json.loads((WORKSPACES / iid / "retrieval.json").read_text(encoding="utf-8"))["entities"]]
    hit = lambda o, k: int(any(e == g or e.startswith(g + ".") for e in o[:k] for g in gold))
    rt = WORKSPACES / iid / "retrieval_trace.json"
    fused = None
    if rt.exists():
        frames = list(reversed(trace.get("frames", [])))
        fused = list(dict.fromkeys(frames + [e["id"] for e in json.loads(rt.read_text(encoding="utf-8"))["entities"]]))
    row |= {"fused": {k: hit(fused, k) for k in KS} if fused is not None else None}
    row |= {"n_executed": len(ex), "n_new": len(new), "has_gold": bool(gold),
            "executed": int(inside(ex)), "new": int(inside(new)), "frame": int(inside(set(trace["frames"]))),
            "file": int(bool({file_of[e] for e in new if e in file_of} & gfiles)),
            "trace": {k: hit(order, k) for k in KS}, "layer2": {k: hit(l2, k) for k in KS + (50,)},
            "either@10": int(hit(order, 10) or hit(l2, 10)), "layer2_absent": int(not hit(l2, 50))}
    return row


def summarise(rows: list[dict]) -> dict:
    ran = [r for r in rows if r["status"] == "ran" and r["has_gold"]]
    out = {"instances": len(rows), "ran": len(ran), "statuses": {s: sum(r["status"] == s for r in rows)
                                                                 for s in sorted({r["status"] for r in rows})}}
    if not ran:
        return out
    m = lambda key: round(statistics.mean(r[key] for r in ran), 4)
    out |= {"timed_out": sum(bool(r.get("timed_out")) for r in ran),
            "median_new": statistics.median(r["n_new"] for r in ran),
            "executed": m("executed"), "new": m("new"), "frame": m("frame"), "file": m("file"),
            "trace_at": {k: round(statistics.mean(r["trace"][k] for r in ran), 4) for k in KS},
            "layer2_at": {k: round(statistics.mean(r["layer2"][k] for r in ran), 4) for k in KS + (50,)},
            "either@10": m("either@10")}
    f = [r for r in ran if r.get("fused")]
    if f:
        out["fused_at"] = {"n": len(f)} | {k: round(statistics.mean(r["fused"][k] for r in f), 4) for k in KS}
        out["layer2_at_same"] = {k: round(statistics.mean(r["layer2"][k] for r in f), 4) for k in KS}
    absent = [r for r in ran if r["layer2_absent"]]
    out["layer2_absent"] = {"n": len(absent), "trace_new": sum(r["new"] for r in absent),
                            "trace@10": sum(r["trace"][10] for r in absent),
                            "trace@20": sum(r["trace"][20] for r in absent)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    specs = {s["instance_id"]: s for s in json.loads(SPECS.read_text(encoding="utf-8"))}
    reports = {b["instance_id"]: b["bug_report"]
               for b in json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))}
    ids = a.ids or sorted(p.parent.name for p in WORKSPACES.glob("*/trace.json") if p.parent.name in specs)
    rows = [r for r in (evaluate(i, specs[i], reports[i]) for i in ids) if r]
    for r in rows:
        if r["status"] != "ran":
            print(f"{r['instance_id']:30} {r['status']}")
        else:
            print(f"{r['instance_id']:30} new={r['n_new']:4} gold ran={r['executed']} new={r['new']} frame={r['frame']} "
                  f"file={r['file']} trace@1/5/10={r['trace'][1]}{r['trace'][5]}{r['trace'][10]} "
                  f"layer2@1/5/10={r['layer2'][1]}{r['layer2'][5]}{r['layer2'][10]}"
                  + (f" fused@1/5/10={r['fused'][1]}{r['fused'][5]}{r['fused'][10]}" if r.get("fused") else ""))
    summary = summarise(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "instances": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
