"""Evaluate Layer 2 retrieval (workspaces/<id>/retrieval.json) against the gold patches.
Evaluator-only: reads the gold patches, which agents must never see.

Gold, per instance (from the developers' fix, buggy-side lines):
  files     edited .py files that exist at the buggy commit
  entities  innermost indexed function / method / class around each edited line
  classes   the same at class level (a method counts as its class), like CE-v2's
            evaluate_localization class / method granularity
Per ranked list (fused, and each source alone: mentions, bm25, graph):
  hit@k     any gold item in the top k          (k = 1, 5, 10, 20)
  recall@k  share of gold items in the top k    (k = 10, 20)
  mrr       1 / rank of the first gold item

  python -m autose.evaluation.layer2_eval --ids <instance_id> ...   (retrieval.json must exist)
  python -m autose.evaluation.layer2_eval --built
Writes results/layer2_eval.json (metrics only, no patch text or gold ids).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from autose.evaluation.layer1_eval import gold_metrics, patch_old_lines
from autose.retrieval.retrieve import fuse

SPECS = Path("benchmarks/swe_bench_lite/private/test_specs.json")
WORKSPACES = Path("workspaces")
OUT = Path("results/layer2_eval.json")
KS = (1, 5, 10, 20)
RECALL_KS = (10, 20)
LISTS = ("fused", "fused_no_embedding", "mentions", "bm25", "graph", "embedding")


def to_class(eid: str, kind: dict) -> str:
    return eid.rsplit(".", 1)[0] if kind.get(eid) == "method" else eid


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def score(ranked: list[str], gold: set[str]) -> dict:
    if not gold:
        return {}
    first = next((i + 1 for i, x in enumerate(ranked) if x in gold), None)
    out = {f"hit@{k}": int(any(x in gold for x in ranked[:k])) for k in KS}
    out |= {f"recall@{k}": round(len(gold & set(ranked[:k])) / len(gold), 4) for k in RECALL_KS}
    out["mrr"] = round(1 / first, 4) if first else 0.0
    return out


def evaluate(iid: str, spec: dict) -> dict:
    index = json.loads((WORKSPACES / iid / "repository_index.json").read_text(encoding="utf-8"))
    ret = json.loads((WORKSPACES / iid / "retrieval.json").read_text(encoding="utf-8"))
    kind = {e["id"]: e["kind"] for e in index["entities"]}
    file_of = {e["id"]: e["file"] for e in index["entities"]}
    indexed = {f["id"] for f in index["files"]}
    g = gold_metrics(index, spec["gold_patch"])
    gold = {"files": {f for f, lines in patch_old_lines(spec["gold_patch"]).items()
                      if f.endswith(".py") and lines is not None and f in indexed},
            "entities": set(g["gold_entities"])}
    gold["classes"] = {to_class(e, kind) for e in gold["entities"]}

    lists = {"fused": [e["id"] for e in ret["entities"]]} | ret["sources"]
    file_lists = {"fused": [f["id"] for f in ret["files"]]}
    if "embedding" in ret["sources"]:  # the same run, fused without the embedding ranker
        base = {k: v for k, v in ret["sources"].items() if k != "embedding"}
        order, _, _, fscore = fuse(base, file_of)
        lists["fused_no_embedding"] = order
        file_lists["fused_no_embedding"] = sorted(fscore, key=lambda f: (-fscore[f], f))
    row = {"instance_id": iid, "repo": spec["repo"], "seconds": ret["seconds"],
           "gold_counts": {k: len(v) for k, v in gold.items()}, "metrics": {}}
    for name in LISTS:
        if name not in lists:
            continue
        ids = lists[name]
        files = file_lists[name] if name in file_lists else dedupe(file_of[i] for i in ids)
        row["metrics"][name] = {"files": score(files, gold["files"]),
                                "classes": score(dedupe(to_class(i, kind) for i in ids), gold["classes"]),
                                "entities": score(ids, gold["entities"])}
    return row


def summarise(rows: list[dict]) -> dict:
    out = {"instances": len(rows), "seconds_median": statistics.median(r["seconds"] for r in rows)}
    for name in LISTS:
        if not any(name in r["metrics"] for r in rows):
            continue
        out[name] = {}
        for level in ("files", "classes", "entities"):
            vals = [r["metrics"][name][level] for r in rows if r["metrics"].get(name, {}).get(level)]
            out[name][level] = {"n": len(vals)} | {m: round(statistics.mean(v[m] for v in vals), 4)
                                                   for m in (vals[0] if vals else {})}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--built", action="store_true", help="every instance with a retrieval.json")
    a = ap.parse_args()
    specs = {s["instance_id"]: s for s in json.loads(SPECS.read_text(encoding="utf-8"))}
    ids = a.ids or sorted(p.parent.name for p in WORKSPACES.glob("*/retrieval.json") if p.parent.name in specs)
    rows = []
    for iid in ids:
        r = evaluate(iid, specs[iid])
        rows.append(r)
        m = r["metrics"]["fused"]
        print(f"{iid:36} file hit@1/5/10 {m['files'].get('hit@1')}/{m['files'].get('hit@5')}/{m['files'].get('hit@10')}"
              f" | class hit@10 {m['classes'].get('hit@10')} | entity hit@10 {m['entities'].get('hit@10')}"
              f" | {r['seconds']}s", flush=True)
    summary = summarise(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "instances": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
