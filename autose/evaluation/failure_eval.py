"""Follow each bug of a scored Layer 4 run through every layer and name the first layer that lost it.
Evaluator-only: reads the gold patches, which the pipeline never sees.

Gold entities and files as in Layer 1 (innermost indexed entity around each edited line; the .py
files the patch edits). An entity matches gold if it is a gold entity or lies inside one.

Per bug:
  L1     gold files are in the index, and the gold lines lie inside an indexed entity
  L2     rank of the first gold entity in the fused retrieval (hit@8 = what Layer 3 is shown)
  trace  a gold entity ran when the report's code ran (and not in the baseline), or is in a traceback
  L3     the agent's final locations name a gold entity (strict), or at least a gold file;
         and whether gold was in front of it (its candidates or what it read)
  L4     the patcher's targets include gold; how many of its candidates edit gold; whether the chosen
         patch edits gold; the local check; the hidden tests (resolved, f2p, p2p)
  vs gold  the chosen patch's changed lines against the developer's: identical, same function,
           same file, or elsewhere

First loss (one per bug, in this order):
  resolved          the hidden tests pass
  L4 fix            the chosen patch edits a gold function but the hidden tests fail
                    (fix_none: no failing test fixed; fix_partial; fix_breaks: fixes them, breaks others)
  L1 index          the gold code is not in any indexed entity (a gold file is not indexed, or the
                    developer's fix only adds new code, so there is no existing function to find)
  L4 location       Layer 3 was right but the chosen patch edits elsewhere
  L3 choice         gold was in Layer 3's candidates, or it read it, or the trace ran it, but it chose wrong
  L2 ranking        gold in the retrieval top 50 but not shown to Layer 3, and the trace did not run it
  L2 recall         gold not in the retrieval top 50, and the trace did not run it

  python -m autose.evaluation.failure_eval [--label final]
Writes results/failure_eval.json (metrics only: no gold names or patch text).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from autose.evaluation.layer1_eval import gold_metrics, patch_old_lines

SPECS = Path("benchmarks/swe_bench_lite/private/test_specs.json")
WORKSPACES = Path("workspaces")
L4 = Path("results/layer4_eval.json")
OUT = Path("results/failure_eval.json")
GROUPS = {"unseen": Path("runs/l4_unseen19.txt"), "fresh": Path("runs/night_fresh20.txt")}
SHOWN = 8  # Layer 3 is shown the top N_CANDIDATES (autose/agents/pipeline.py) of Layer 2
DEEP = 50
LOSSES = ("resolved", "L4 fix", "L1 index", "L4 location", "L3 choice", "L2 ranking", "L2 recall")


def ids_in(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def on_gold(ids, gold: set[str]) -> bool:
    return any(e == g or e.startswith(g + ".") for e in ids for g in gold)


def rank(ids: list[str], gold: set[str]) -> int | None:
    for i, e in enumerate(ids, 1):
        if on_gold([e], gold):
            return i
    return None


def changed_lines(patch: str) -> Counter:
    """The patch's removed and added lines, whitespace-normalised, comments and blanks dropped."""
    out = Counter()
    for line in patch.splitlines():
        if line.startswith(("+++", "---")) or line[:1] not in "+-":
            continue
        body = re.sub(r"\s+", " ", line[1:].split("#", 1)[0]).strip()
        if body:
            out[(line[0], body)] += 1
    return out


def versus_gold(ours: str, gold: str, ours_ent: set[str], gold_ent: set[str], gold_files: set[str]) -> dict:
    a, b = changed_lines(ours), changed_lines(gold)
    inter = sum((a & b).values())
    union = sum((a | b).values())
    ours_files = {f for f, l in patch_old_lines(ours).items() if f.endswith(".py")}
    if a == b:
        kind = "identical"
    elif on_gold(ours_ent, gold_ent):
        kind = "same_function"
    elif ours_files & gold_files:
        kind = "same_file"
    else:
        kind = "elsewhere"
    return {"kind": kind, "line_overlap": round(inter / union, 3) if union else 0.0,
            "lines_ours": sum(a.values()), "lines_gold": sum(b.values())}


def frac(s: str | None) -> tuple[int, int]:
    if not s or "/" not in s:
        return 0, 0
    p, t = s.split("/")
    return int(p), int(t)


def analyse(iid: str, spec: dict, scored: dict, patches: Path) -> dict:
    ws = WORKSPACES / iid
    index = json.loads((ws / "repository_index.json").read_text(encoding="utf-8"))
    gm = gold_metrics(index, spec["gold_patch"])
    gold = set(gm["gold_entities"])
    gold_files = {f for f, l in patch_old_lines(spec["gold_patch"]).items() if f.endswith(".py") and l is not None}
    rep_path = patches / iid / "patch_report.json"
    prep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
    diff = (patches / iid / "patch.diff").read_text(encoding="utf-8") if (patches / iid / "patch.diff").exists() else ""
    ent_of = lambda p: set(gold_metrics(index, p)["gold_entities"]) if p.strip() else set()
    ours_ent = ent_of(diff)
    cands = [a for a in prep.get("attempts", []) if a.get("diff") and "same_as" not in a]
    cand_on_gold = sum(on_gold(ent_of(a["diff"]), gold) for a in cands)
    del index

    row = {"instance_id": iid}
    # L1
    row["l1"] = {"files": gm["files_existing"] > 0 and gm["files_in_index"] == gm["files_existing"],
                 "entity": bool(gold), "lines_in_entity": f"{gm['lines_in_entity']}/{gm['lines']}"}
    # L2
    retr = json.loads((ws / "retrieval.json").read_text(encoding="utf-8"))
    r_ent = rank([e["id"] for e in retr["entities"]], gold)
    r_file = next((i for i, f in enumerate(retr["files"], 1) if f["id"] in gold_files), None)
    row["l2"] = {"entity_rank": r_ent, "file_rank": r_file,
                 f"hit@{SHOWN}": bool(r_ent and r_ent <= SHOWN), f"hit@{DEEP}": bool(r_ent and r_ent <= DEEP)}
    # trace
    tp = ws / "trace.json"
    tr = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {"status": "missing"}
    if tr.get("status") == "ran":
        new = set(tr["executed"]) - set(tr["baseline"])
        row["trace"] = {"status": "ran", "ran": on_gold(tr["executed"], gold), "new": on_gold(new, gold),
                        "frame": on_gold(tr["frames"], gold)}
    else:
        row["trace"] = {"status": tr.get("status"), "ran": False, "new": False, "frame": False}
    # L3
    ar = json.loads((ws / "agent_report.json").read_text(encoding="utf-8"))
    pl = ar.get("problem_location") or {}
    named = list(pl.get("entities", [])) + list(pl.get("new_methods", []))
    shown = list(ar.get("candidates", []))
    row["l3"] = {"status": ar.get("status"), "entity": on_gold(named, gold),
                 "file": bool({p.split(":", 1)[0] for p in named} & gold_files),
                 "primary": on_gold([ar["primary"]] if ar.get("primary") else [], gold),
                 "gold_shown": on_gold(shown, gold), "gold_read": on_gold(ar.get("read", []), gold)}
    # L4
    f2p, p2p = frac(scored.get("f2p")), frac(scored.get("p2p"))
    row["l4"] = {"targets": on_gold(prep.get("targets", []), gold), "candidates": len(cands),
                 "candidates_on_gold": cand_on_gold, "patch_on_gold": on_gold(ours_ent, gold),
                 "patch_on_gold_file": bool({f for f in patch_old_lines(diff)} & gold_files),
                 "verified": bool(prep.get("verified")), "applied": scored.get("applied"),
                 "resolved": bool(scored.get("resolved")), "f2p": scored.get("f2p"), "p2p": scored.get("p2p"),
                 "f2p_any": f2p[0] > 0, "f2p_all": f2p[1] > 0 and f2p[0] == f2p[1],
                 "p2p_all": p2p[0] == p2p[1]}
    row["vs_gold"] = versus_gold(diff, spec["gold_patch"], ours_ent, gold, gold_files) if diff.strip() else None

    # first loss
    l4, l3 = row["l4"], row["l3"]
    evidence = l3["gold_shown"] or l3["gold_read"] or row["trace"]["new"] or row["trace"]["frame"]
    if l4["resolved"]:
        loss, why = "resolved", None
    elif l4["patch_on_gold"]:
        loss = "L4 fix"
        why = "fix_none" if not l4["f2p_any"] else "fix_partial" if not l4["f2p_all"] else "fix_breaks"
    elif not (row["l1"]["files"] and row["l1"]["entity"]):
        loss = "L1 index"
        why = "gold only adds new code" if row["l1"]["files"] else "gold file not indexed"
    elif l3["entity"]:
        loss, why = "L4 location", ("a candidate edited gold" if cand_on_gold else "no candidate edited gold")
    elif evidence:
        loss, why = "L3 choice", ("shown" if l3["gold_shown"] else "read" if l3["gold_read"] else "trace only")
    elif row["l2"][f"hit@{DEEP}"]:
        loss, why = "L2 ranking", None
    else:
        loss, why = "L2 recall", None
    row["first_loss"], row["why"] = loss, why
    return row


def funnel(rows: list[dict]) -> dict:
    c = lambda f: sum(1 for r in rows if f(r))
    return {"bugs": len(rows),
            "l1_gold_indexed": c(lambda r: r["l1"]["files"] and r["l1"]["entity"]),
            f"l2_gold@{SHOWN}": c(lambda r: r["l2"][f"hit@{SHOWN}"]),
            f"l2_gold@{DEEP}": c(lambda r: r["l2"][f"hit@{DEEP}"]),
            "l2_file@1": c(lambda r: r["l2"]["file_rank"] == 1),
            "trace_ran": c(lambda r: r["trace"]["status"] == "ran"),
            "trace_gold_new": c(lambda r: r["trace"]["new"]),
            "trace_gold_frame": c(lambda r: r["trace"]["frame"]),
            "l3_gold_shown_or_read": c(lambda r: r["l3"]["gold_shown"] or r["l3"]["gold_read"]),
            "l3_entity": c(lambda r: r["l3"]["entity"]),
            "l3_file": c(lambda r: r["l3"]["file"]),
            "l4_targets_gold": c(lambda r: r["l4"]["targets"]),
            "l4_any_candidate_on_gold": c(lambda r: r["l4"]["candidates_on_gold"] > 0),
            "l4_patch_on_gold": c(lambda r: r["l4"]["patch_on_gold"]),
            "l4_patch_on_gold_file": c(lambda r: r["l4"]["patch_on_gold_file"]),
            "l4_any_failing_test_fixed": c(lambda r: r["l4"]["f2p_any"]),
            "l4_all_failing_tests_fixed": c(lambda r: r["l4"]["f2p_all"]),
            "l4_no_regression": c(lambda r: r["l4"]["p2p_all"]),
            "l4_verified_locally": c(lambda r: r["l4"]["verified"]),
            "resolved": c(lambda r: r["l4"]["resolved"]),
            "first_loss": {k: c(lambda r, k=k: r["first_loss"] == k) for k in LOSSES},
            "why": dict(Counter(f"{r['first_loss']}: {r['why']}" for r in rows if r["why"])),
            "vs_gold_all": dict(Counter(r["vs_gold"]["kind"] for r in rows if r["vs_gold"])),
            "vs_gold_resolved": dict(Counter(r["vs_gold"]["kind"] for r in rows if r["vs_gold"] and r["l4"]["resolved"]))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="final", help="a run in results/layer4_eval.json")
    a = ap.parse_args()
    run = json.loads(L4.read_text(encoding="utf-8"))["runs"][a.label]
    specs = {s["instance_id"]: s for s in json.loads(SPECS.read_text(encoding="utf-8"))}
    group_of = {i: g for g, p in GROUPS.items() for i in ids_in(p)}
    rows = []
    for iid in sorted(run["instances"]):
        r = analyse(iid, specs[iid], run["instances"][iid], Path(run["patches"]))
        r["group"] = group_of.get(iid, "seen")
        rows.append(r)
        v = r["vs_gold"] or {}
        print(f"{iid:28} {r['group']:6} L2@{SHOWN}={int(r['l2'][f'hit@{SHOWN}'])} trace={int(r['trace']['new'])} "
              f"L3={int(r['l3']['entity'])} L4gold={int(r['l4']['patch_on_gold'])} cand={r['l4']['candidates_on_gold']}/"
              f"{r['l4']['candidates']} f2p={r['l4']['f2p']} p2p={r['l4']['p2p']} -> {r['first_loss']}"
              f"{' (' + r['why'] + ')' if r['why'] else ''} | vs gold {v.get('kind')} {v.get('line_overlap')}", flush=True)
    summary = {"label": a.label, "all": funnel(rows)}
    for g in ("unseen", "fresh", "seen"):
        summary[g] = funnel([r for r in rows if r["group"] == g])
    OUT.write_text(json.dumps({"summary": summary, "instances": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
