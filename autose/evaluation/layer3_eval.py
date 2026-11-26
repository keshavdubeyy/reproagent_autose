"""Evaluate Layer 3 localization (workspaces/<id>/agent_report.json) against the gold patches.
Evaluator-only: reads the gold patches, which agents must never see.

Port of context-engineering-v2@1c65307 src/evaluate_localization.py: the same gold
(parse_patch_locations: classes and methods the patch changes, from hunk context and changed
def/class lines), the same bare-name matching and the same metrics at class and method level:
  hit        any prediction matches a gold item
  coverage   every gold item is matched (all-or-nothing)
  precision  matched predictions / predictions
  recall     matched gold items / gold items
  f1
AUTOSE additions: the same metrics for Layer 2 alone (its top-k fused entities as the
prediction), so the agents' gain over retrieval is visible, and for the explorer's PRIMARY.

  python -m autose.evaluation.layer3_eval [--ids ...]     (every agent_report.json by default)
Writes results/layer3_eval.json (metrics only, no patch text or gold names).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

from autose.evaluation.layer1_eval import gold_metrics, patch_old_lines

SPECS = Path("benchmarks/swe_bench_lite/private/test_specs.json")
WORKSPACES = Path("workspaces")
OUT = Path("results/layer3_eval.json")
LEVELS = ("class", "method")
METRICS = ("hit", "coverage", "precision", "recall", "f1")
LAYER2_K = (1, 3, 5)


# ---------- copied from CE-v2 evaluate_localization.py ----------
def parse_patch_locations(patch_text: str) -> dict:
    classes, methods, seen_c, seen_m = [], [], set(), set()

    def add(lst, seen, name):
        if name not in seen:
            seen.add(name)
            lst.append(name)

    enclosing_class = enclosing_method = None
    for line in patch_text.splitlines():
        if line.startswith("diff --git"):
            enclosing_class = enclosing_method = None
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)", line)
        if hunk:
            ctx = hunk.group(1).strip()
            enclosing_method = enclosing_class = None
            if ctx:
                m = re.match(r"(?:def|async def)\s+(\w+)\s*\(", ctx)
                if m:
                    enclosing_method = m.group(1)
                m = re.match(r"class\s+(\w+)", ctx)
                if m:
                    enclosing_class = m.group(1)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        content = line[1:] if line[:1] in "+-" else line
        changed = line[:1] in "+-" and line != ""
        m = re.match(r"\s*class\s+(\w+)", content)
        if m:
            enclosing_class, enclosing_method = m.group(1), None
            if changed:
                add(classes, seen_c, m.group(1))
            continue
        m = re.match(r"\s*(?:def|async def)\s+(\w+)\s*\(", content)
        if m:
            enclosing_method = m.group(1)
            if changed:
                add(methods, seen_m, m.group(1))
            continue
        if not changed:
            continue
        if enclosing_method:
            add(methods, seen_m, enclosing_method)
        elif enclosing_class:
            add(classes, seen_c, enclosing_class)
    return {"classes": classes, "methods": methods}


def _bare(name: str) -> str:
    # AUTOSE: also drop markdown the model leaves around names (`x`, **x**, ___init___ -> __init__)
    name = re.sub(r"[`*()\s]", "", name).rstrip(".")
    bare = name.rsplit(".", 1)[-1].rsplit(":", 1)[-1].lower()
    return re.sub(r"^_{3,}(\w+?)_{3,}$", r"__\1__", bare)


def _dedupe_bare(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if _bare(x) not in seen:
            seen.add(_bare(x))
            out.append(x)
    return out


def metrics(pred: list[str], gold: list[str]) -> dict | None:
    if not gold:
        return None
    p, g = {_bare(x) for x in pred}, {_bare(x) for x in gold}
    tp = len(p & g)
    prec = tp / len(pred) if pred else 0.0
    rec = tp / len(g)
    return {"hit": int(tp > 0), "coverage": int(g <= p), "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0}
# ----------------------------------------------------------------


def entities_as_prediction(ids: list[str], kind: dict) -> dict:
    """Index entity ids -> CE-v2 style {classes, methods} (bare names)."""
    classes, methods = [], []
    for eid in ids:
        qual = eid.split(":", 1)[1]
        parts = qual.split(".")
        if kind.get(eid) == "class":
            classes.append(parts[-1])
        else:
            methods.append(parts[-1])
            if len(parts) > 1:
                classes.append(parts[-2])
    return {"classes": _dedupe_bare(classes), "methods": _dedupe_bare(methods)}


def strict(pred_ids: list[str], gold_ids: set[str], gold_files: set[str]) -> dict | None:
    """AUTOSE strict score: an entity counts only if it is a gold entity (innermost indexed
    entity around an edited line) or lies inside one (a method of / added to a gold class).
    CE-v2's bare-name match would count OctaveCodePrinter._print_sinc for CCodePrinter._print_sinc."""
    if not gold_ids and not gold_files:
        return None
    match = lambda p: any(p == g or p.startswith(g + ".") for g in gold_ids)
    hit_gold = {g for g in gold_ids if any(p == g or p.startswith(g + ".") for p in pred_ids)}
    files = {p.split(":", 1)[0] for p in pred_ids}
    return {"entity_hit": int(any(match(p) for p in pred_ids)) if gold_ids else None,
            "entity_recall": round(len(hit_gold) / len(gold_ids), 4) if gold_ids else None,
            "entity_precision": round(sum(match(p) for p in pred_ids) / len(pred_ids), 4) if pred_ids and gold_ids else 0.0,
            "file_hit": int(bool(files & gold_files)) if gold_files else None}


def evaluate(iid: str, spec: dict) -> dict | None:
    rep_path = WORKSPACES / iid / "agent_report.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    if rep.get("status") != "ok":
        return {"instance_id": iid, "repo": spec["repo"], "status": rep.get("status"), "error": rep.get("error")}
    index = json.loads((WORKSPACES / iid / "repository_index.json").read_text(encoding="utf-8"))
    kind = {e["id"]: e["kind"] for e in index["entities"]}
    gold_ids = set(gold_metrics(index, spec["gold_patch"])["gold_entities"])
    gold_files = {f for f, lines in patch_old_lines(spec["gold_patch"]).items() if f.endswith(".py") and lines is not None}
    del index
    retrieval = json.loads((WORKSPACES / iid / "retrieval.json").read_text(encoding="utf-8"))
    gold = parse_patch_locations(spec["gold_patch"])
    gold = {"classes": _dedupe_bare(gold["classes"]), "methods": _dedupe_bare(gold["methods"])}

    pl = rep.get("problem_location") or {}
    preds = {"agent": {"classes": _dedupe_bare(pl.get("classes", [])), "methods": _dedupe_bare(pl.get("methods", []))},
             "agent_primary": entities_as_prediction([rep["primary"]] if rep.get("primary") else [], kind)}
    fused = [e["id"] for e in retrieval["entities"]]
    for k in LAYER2_K:
        preds[f"layer2_top{k}"] = entities_as_prediction(fused[:k], kind)
    pred_ids = {"agent": list(pl.get("entities", [])) + list(pl.get("new_methods", [])),
                "agent_primary": [rep["primary"]] if rep.get("primary") else []}
    pred_ids |= {f"layer2_top{k}": fused[:k] for k in LAYER2_K}
    u = rep.get("usage", {})
    return {"instance_id": iid, "repo": spec["repo"], "status": "ok", "seconds": rep.get("seconds"),
            "primary_source": rep.get("primary_source"), "tool_calls": len(rep.get("tool_calls", [])),
            "tokens_in": u.get("prompt_tokens"), "tokens_out": u.get("output_tokens"),
            "gold_counts": {k: len(v) for k, v in gold.items()},
            "pred_counts": {name: {k: len(v) for k, v in p.items()} for name, p in preds.items()},
            "metrics": {name: {lvl: metrics(p[lvl + ("es" if lvl == "class" else "s")], gold[lvl + ("es" if lvl == "class" else "s")])
                               for lvl in LEVELS} for name, p in preds.items()},
            "strict": {name: strict(ids, gold_ids, gold_files) for name, ids in pred_ids.items()}}


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    out = {"instances": len(rows), "ok": len(ok), "errors": len(rows) - len(ok)}
    if not ok:
        return out
    out["seconds_median"] = statistics.median(r["seconds"] for r in ok)
    out["primary_from_layer2_fallback"] = sum(r["primary_source"] == "layer2_fallback" for r in ok)
    for name in ok[0]["metrics"]:
        out[name] = {}
        for lvl in LEVELS:
            vals = [r["metrics"][name][lvl] for r in ok if r["metrics"][name][lvl]]
            out[name][lvl] = {"n": len(vals)} | {m: round(statistics.mean(v[m] for v in vals), 4) for m in METRICS
                                                 if vals}
        s = [r["strict"][name] for r in ok if r.get("strict", {}).get(name)]
        out[name]["strict"] = {m: round(statistics.mean(v[m] for v in s if v[m] is not None), 4)
                               for m in ("entity_hit", "entity_recall", "entity_precision", "file_hit")
                               if any(v[m] is not None for v in s)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    specs = {s["instance_id"]: s for s in json.loads(SPECS.read_text(encoding="utf-8"))}
    ids = a.ids or sorted(p.parent.name for p in WORKSPACES.glob("*/agent_report.json") if p.parent.name in specs)
    rows = [evaluate(i, specs[i]) for i in ids]
    for r in rows:
        if r["status"] != "ok":
            print(f"{r['instance_id']:36} {r['status']}: {r.get('error')}")
            continue
        m = r["metrics"]
        fmt = lambda x: "-" if x is None else f"{x['hit']}"
        st = lambda n: "-" if not r["strict"].get(n) else f"{r['strict'][n]['entity_hit']}/{r['strict'][n]['file_hit']}"
        print(f"{r['instance_id']:36} CE-v2 method hit: agent {fmt(m['agent']['method'])} layer2@1 {fmt(m['layer2_top1']['method'])}"
              f" @3 {fmt(m['layer2_top3']['method'])} | strict entity/file: agent {st('agent')} layer2@3 {st('layer2_top3')}"
              f" | {r['seconds']}s")
    summary = summarise(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "instances": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
