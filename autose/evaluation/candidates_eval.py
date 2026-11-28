"""Score every candidate patch of a saved Layer 4 run with the hidden tests, not only the chosen one.
Evaluator-only: uses the private SWE-bench harness records, which the patcher never sees.

Answers whether the model ever wrote a passing fix (generation) and whether the vote picked it
(selection). Candidates come from <run>/<id>/patch_report.json ("attempts" with a diff); duplicates
(same diff text, or marked same_as) are scored once. The chosen patch's existing score in
results/layer4_eval.json is reused when its diff is one of the candidates.

Per bug:
  candidates        distinct candidate diffs
  resolved          candidates that resolve the bug (pass@k numerator)
  chosen_resolved   the vote's pick resolves it
  best_f2p          most failing tests any candidate fixes
  resolved_where    where the resolving candidates were placed: located / alternative
  resolved_votes    votes the resolving candidates got, against the winner's
Summary: any_resolved (oracle), chosen_resolved, lost_by_selection, never_generated.

  python -m autose.evaluation.candidates_eval [--label final] [--ids ...]
Needs Docker Desktop running. Resumes: scored candidates are skipped; a Docker crash is not recorded
(exit 3 after 3 in a row, so a wrapper can restart Docker and run it again).
Writes runs/candidates/<label>/<id>/<n>/ (logs) and results/candidates_eval.json (metrics only).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import docker

from autose.evaluation.evaluator import evaluate, load_harness

L4 = Path("results/layer4_eval.json")
OUT = Path("results/candidates_eval.json")
RUNS = Path("runs/candidates")
GROUPS = {"unseen": Path("runs/l4_unseen19.txt"), "fresh": Path("runs/night_fresh20.txt"),
          "new": Path("runs/night_rest32.txt")}  # new: never run past Layer 2 before the v3 run
MAX_CRASHES = 3


def frac(s: str | None) -> tuple[int, int]:
    if not s or "/" not in s:
        return 0, 0
    p, t = s.split("/")
    return int(p), int(t)


def candidates(rep: dict) -> list[dict]:
    """Distinct candidate diffs with the attempt that first wrote each, and its votes."""
    seen, out = {}, []
    for a in rep.get("attempts", []):
        d = a.get("diff") or ""
        if not d.strip() or "same_as" in a:
            continue
        if d in seen:
            continue
        seen[d] = True
        out.append({"n": int(a["n"]), "location": "located" if a.get("location") == "located" else "alternative",
                    "votes": int(a.get("votes") or 0), "verified_like": bool((a.get("check") or {}).get("fixed")),
                    "diff": d})
    return out


def summarise(rows: dict, group_of: dict) -> dict:
    def block(ids):
        rs = [rows[i] for i in ids if rows[i]["complete"]]
        return {"bugs": len(rs),
                "candidates": sum(r["candidates"] for r in rs),
                "candidates_resolved": sum(r["resolved"] for r in rs),
                "any_resolved": sum(r["resolved"] > 0 for r in rs),
                "chosen_resolved": sum(r["chosen_resolved"] for r in rs),
                "lost_by_selection": sum(r["resolved"] > 0 and not r["chosen_resolved"] for r in rs),
                "never_generated": sum(r["resolved"] == 0 for r in rs),
                "any_f2p_fixed": sum(r["best_f2p"] > 0 for r in rs),
                "incomplete": sum(not rows[i]["complete"] for i in ids)}
    out = {"all": block(list(rows))}
    for g in ("unseen", "fresh", "new", "seen"):
        out[g] = block([i for i in rows if group_of.get(i, "seen") == g])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="final", help="a run in results/layer4_eval.json")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    run = json.loads(L4.read_text(encoding="utf-8"))["runs"][a.label]
    src = Path(run["patches"])
    harness = load_harness()
    group_of = {i: g for g, p in GROUPS.items() for i in (p.read_text(encoding="utf-8").split() if p.exists() else [])}
    store = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    scores = store.get("runs", {}).get(a.label, {}).get("scores", {})  # iid -> {n: result}
    ids = a.ids or sorted(run["instances"])
    client = docker.from_env(timeout=a.timeout)
    crashes = 0

    def save(rows=None):
        entry = {"patches": str(src), "scores": scores}
        if rows is not None:
            entry |= {"summary": summarise(rows, group_of), "instances": rows}
        store.setdefault("runs", {})[a.label] = entry
        OUT.write_text(json.dumps(store, indent=1), encoding="utf-8")

    for iid in ids:
        rep = json.loads((src / iid / "patch_report.json").read_text(encoding="utf-8"))
        chosen = (src / iid / "patch.diff").read_text(encoding="utf-8") if (src / iid / "patch.diff").exists() else ""
        done = scores.setdefault(iid, {})
        for c in candidates(rep):
            key = str(c["n"])
            if key in done:
                continue
            prev = run["instances"].get(iid, {})
            if c["diff"] == chosen and prev.get("f2p"):
                done[key] = {"resolved": prev["resolved"], "f2p": prev["f2p"], "p2p": prev["p2p"],
                             "applied": prev.get("applied"), "reused": True}
            else:
                r = evaluate(harness[iid], c["diff"], f"cand{key}", RUNS / a.label / iid / key, client,
                             a.timeout).to_dict()
                if (r["error"] or "").startswith("crash"):
                    crashes += 1
                    print(f"{iid} #{key}: NOT SCORED ({r['error'][:140]})", flush=True)
                    if crashes >= MAX_CRASHES:
                        print("stopping: Docker failed 3 times in a row", flush=True)
                        save()
                        return 3
                    continue
                crashes = 0
                done[key] = {"resolved": r["resolved"], "f2p": f"{r['f2p_passed']}/{r['f2p_total']}",
                             "p2p": f"{r['p2p_passed']}/{r['p2p_total']}", "applied": r["patch_applied"],
                             "error": r["error"]}
            print(f"{iid} #{key} ({c['location']}, {c['votes']} votes): resolved={done[key]['resolved']} "
                  f"f2p={done[key]['f2p']} p2p={done[key]['p2p']}", flush=True)
            save()

    rows = {}
    for iid in ids:
        rep = json.loads((src / iid / "patch_report.json").read_text(encoding="utf-8"))
        cs = candidates(rep)
        sc = scores.get(iid, {})
        res = [c for c in cs if sc.get(str(c["n"]), {}).get("resolved")]
        top = max((c["votes"] for c in cs), default=0)
        rows[iid] = {"group": group_of.get(iid, "seen"), "candidates": len(cs),
                     "complete": all(str(c["n"]) in sc for c in cs),
                     "resolved": len(res), "chosen_resolved": bool(run["instances"].get(iid, {}).get("resolved")),
                     "best_f2p": max((frac(sc.get(str(c["n"]), {}).get("f2p"))[0] for c in cs), default=0),
                     "resolved_where": [c["location"] for c in res], "resolved_votes": [c["votes"] for c in res],
                     "top_votes": top}
    save(rows)
    s = summarise(rows, group_of)
    print(json.dumps(s, indent=1))
    print("score done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
