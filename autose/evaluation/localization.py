"""Localisation accuracy of the retrieval modes, scored against the developers' fix (evaluator only).

Gold = the files the gold patch changes, and the innermost functions/classes around the changed lines
in the buggy version. For each bug and mode we record the rank of the first gold file and gold symbol.

  python -m autose.evaluation.localization                      # every bug with a code snapshot
  python -m autose.evaluation.localization --repo django/django --report raw
Writes results/localization.json (ids, ranks, metrics only: no patch content, safe to publish).
Progress is kept in <out>.partial.jsonl; rerunning the same command resumes. Delete it to start over.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import time
from pathlib import Path

from autose.agent.explore import RepoExplorer
from autose.agent.issue import BugSpecifier
from autose.intel.index import KnowledgeIndex, build
from autose.intel.retrieve import HybridRetriever
from autose.run import PUBLIC, load_reports, to_report
from .evaluator import load_harness

MODES = ["explorer", "bm25", "hybrid", "graph"]   # explorer = the mid-term keyword search (baseline)
OUT = Path("results/localization.json")


def changed_old_lines(patch: str) -> dict[str, set[int]]:
    """Per file, the buggy-version line numbers the gold patch touches (insertions count as the line before)."""
    out: dict[str, set[int]] = {}
    cur, old = None, 0
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            cur = re.search(r" a/(\S+)", line).group(1)
            out.setdefault(cur, set())
        elif line.startswith("@@"):
            old = int(re.search(r"-(\d+)", line).group(1))
        elif cur is None or line.startswith(("---", "+++")):
            continue
        elif line.startswith("-"):
            out[cur].add(old)
            old += 1
        elif line.startswith("+"):
            out[cur].add(max(old - 1, 1))
        else:
            old += 1
    return out


def gold_symbols(repo: Path, patch: str) -> tuple[set[str], set[tuple[str, str]]]:
    files, syms = set(), set()
    for rel, lines in changed_old_lines(patch).items():
        if not rel.endswith(".py"):
            continue
        files.add(rel)
        try:
            tree = ast.parse((repo / rel).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for ln in lines:
            best = None
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and \
                        n.lineno <= ln <= getattr(n, "end_lineno", n.lineno):
                    if best is None or n.lineno >= best.lineno:
                        best = n
            syms.add((rel, best.name if best else "<module>"))
    return files, syms


def rank(items: list, gold) -> int | None:
    for i, x in enumerate(items, 1):
        if x in gold:
            return i
    return None


def evaluate_bug(iid: str, row: dict, record: dict, variant: str) -> dict:
    repo = PUBLIC / "code" / iid
    report = to_report(row, variant)
    gfiles, gsyms = gold_symbols(repo, record["patch"])
    t0 = time.time()
    ix = KnowledgeIndex(build(repo, row["repo"], row["base_commit"]))
    res = {"instance_id": iid, "repo": row["repo"], "gold_files": sorted(gfiles), "n_gold_symbols": len(gsyms),
           "index_s": round(time.time() - t0, 1), "index": ix.manifest()}
    for mode in MODES:
        t1 = time.time()
        if mode == "explorer":
            spec = BugSpecifier(None).specify(report)        # heuristic spec, no model: same input for all
            ex = RepoExplorer(repo)
            snippets = ex.search(spec, report, k=20).snippets
            files = list(dict.fromkeys(s.file for s in snippets))
            syms = [(s.file, s.name) for s in snippets]
        else:
            r = HybridRetriever(ix, mode).search(report, k_symbols=20, k_files=10)
            files = [p for p, _ in r.files]
            syms = [(ix.symbol(c.sid)["path"], ix.symbol(c.sid)["name"]) for c in r.symbols]
        res[mode] = {"file_rank": rank(files, gfiles), "symbol_rank": rank(syms, gsyms),
                     "top_files": files[:5], "s": round(time.time() - t1, 2)}
    return res


def metrics(rows: list[dict], mode: str) -> dict:
    n = len(rows)
    fr = [r[mode]["file_rank"] for r in rows]
    sr = [r[mode]["symbol_rank"] for r in rows]
    at = lambda xs, k: round(sum(1 for x in xs if x and x <= k) / n, 3) if n else 0
    return {"n": n, "file@1": at(fr, 1), "file@3": at(fr, 3), "file@5": at(fr, 5),
            "file_MRR": round(sum(1 / x for x in fr if x) / n, 3) if n else 0,
            "symbol@5": at(sr, 5), "symbol@10": at(sr, 10), "symbol@20": at(sr, 20),
            "avg_s": round(sum(r[mode]["s"] for r in rows) / n, 2) if n else 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", nargs="*")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--report", default="full", choices=["full", "raw"])
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    reports, harness = load_reports(), load_harness()
    validation = json.loads(Path("results/benchmark_validation.json").read_text())["bugs"]
    ids = [i for i in sorted(reports) if (PUBLIC / "code" / i).exists()
           and validation.get(i, {}).get("status") == "VALID"]
    if a.repo:
        ids = [i for i in ids if reports[i]["repo"] in set(a.repo)]
    if a.ids:
        ids = [i for i in ids if i in set(a.ids)]
    # One line per finished bug, so a crash or Ctrl-C loses nothing: a rerun skips bugs already done.
    partial = a.out.with_suffix(".partial.jsonl")
    done = {}
    if partial.exists():
        for line in partial.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["instance_id"]] = r
    rows = []
    for n, iid in enumerate(ids, 1):
        if iid in done:
            rows.append(done[iid])
            continue
        r = evaluate_bug(iid, reports[iid], harness[iid], a.report)
        rows.append(r)
        with partial.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r) + "\n")
        print(f"[{n}/{len(ids)}] {iid}: " + "  ".join(f"{m}={r[m]['file_rank']}/{r[m]['symbol_rank']}" for m in MODES)
              + f"  (index {r['index_s']}s)", flush=True)
    summary = {"report": a.report, "modes": {m: metrics(rows, m) for m in MODES},
               "by_repo": {repo: {m: metrics([r for r in rows if r["repo"] == repo], m) for m in MODES}
                           for repo in sorted({r["repo"] for r in rows})},
               "bugs": rows}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("\nmode       " + "  ".join(f"{k:>9}" for k in metrics(rows, MODES[0])))
    for m in MODES:
        print(f"{m:<10} " + "  ".join(f"{v:>9}" for v in summary["modes"][m].values()))
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
