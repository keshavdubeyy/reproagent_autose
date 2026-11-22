"""Evaluator-only experiment: does handing the search results to the Fault Localiser help?

The developers' own new tests (hidden test_patch) stand in for the agent's reproduction test: their code
is given to the localiser (no traceback). For each bug the localiser runs with and without the search's
ranked definitions, and we check whether the function the developers fixed is among its candidates.

    python -m autose.evaluation.localizer_handoff                # 40-bug subset
    python -m autose.evaluation.localizer_handoff --all          # every valid downloaded bug
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from autose.agent.explore import IndexedExplorer, RepoExplorer
from autose.agent.issue import BugSpecifier
from autose.agent.repair import FaultLocalizer
from autose.run import PUBLIC, load_reports, to_report
from .evaluator import load_harness
from .localization import gold_symbols

SUBSET = Path("benchmarks/swe_bench_lite/subset.json")


def oracle_test(repo: Path, test_patch: str) -> str | None:
    """Imports + the test functions the developers' test patch adds or changes (after applying it)."""
    tmp = Path(tempfile.mkdtemp(prefix="autose_oracle_"))
    try:
        rels = re.findall(r"^diff --git a/(\S+) b/", test_patch, re.M)
        for rel in rels:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            if (repo / rel).exists():
                shutil.copy(repo / rel, tmp / rel)
        if subprocess.run(["git", "apply", "-"], input=test_patch, text=True, cwd=tmp,
                          capture_output=True).returncode != 0:
            return None
        added = {}
        cur = None
        for line in test_patch.splitlines():
            if line.startswith("+++ b/"):
                cur = line[6:]
                added.setdefault(cur, set())
            elif line.startswith("@@"):
                new = int(re.search(r"\+(\d+)", line).group(1))
            elif cur and line.startswith("+"):
                added[cur].add(new)
                new += 1
            elif cur and not line.startswith("-"):
                new += 1
        parts = []
        for rel, lines in added.items():
            if not rel.endswith(".py") or not (tmp / rel).exists():
                continue
            src = (tmp / rel).read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            seg = src.splitlines()
            parts += [ast.get_source_segment(src, n) or "" for n in tree.body
                      if isinstance(n, (ast.Import, ast.ImportFrom))]
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test") \
                        and any(n.lineno <= x <= n.end_lineno for x in lines):
                    parts.append("\n".join(l[n.col_offset:] for l in seg[n.lineno - 1:n.end_lineno]))
        return "\n\n".join(parts) or None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("results/localizer_handoff.json"))
    a = ap.parse_args()
    reports, harness = load_reports(), load_harness()
    if a.all:
        val = json.loads(Path("results/benchmark_validation.json").read_text())["bugs"]
        ids = sorted(i for i in reports if (PUBLIC / "code" / i).exists() and val.get(i, {}).get("status") == "VALID")
    else:
        ids = json.loads(SUBSET.read_text())
    rows = []
    for n, iid in enumerate(ids, 1):
        row, repo = reports[iid], PUBLIC / "code" / iid
        report = to_report(row)
        spec = BugSpecifier(None).specify(report)
        _, gold = gold_symbols(repo, harness[iid]["patch"])
        gleaf = {(f, q.rsplit(".", 1)[-1]) for f, q in gold}
        test = oracle_test(repo, harness[iid]["test_patch"])
        ex = IndexedExplorer(repo, "graph", row["repo"], row["base_commit"])
        ret = ex.search(spec, report).ranked                 # exactly what the agent hands over
        hit = lambda cands: next((i for i, c in enumerate(cands, 1) if (c.file, c.name.rsplit(".", 1)[-1]) in gleaf), None)
        loc = FaultLocalizer(repo, RepoExplorer(repo))
        r = {"instance_id": iid, "has_test": test is not None,
             "search": next((i for i, (f, _, nm) in enumerate(ret, 1) if (f, nm.rsplit(".", 1)[-1]) in gleaf), None),
             "loc_test_only": hit(loc.localize(report, spec, test, None)),
             "loc_test_plus_search": hit(loc.localize(report, spec, test, None, retrieved=ret)),
             "loc_report_only": hit(loc.localize(report, spec, None, None)),
             "loc_report_plus_search": hit(loc.localize(report, spec, None, None, retrieved=ret))}
        rows.append(r)
        print(f"[{n}/{len(ids)}] {iid} " + " ".join(f"{k}={v}" for k, v in r.items() if k != "instance_id"), flush=True)
    keys = ["search", "loc_test_only", "loc_test_plus_search", "loc_report_only", "loc_report_plus_search"]
    summary = {k: sum(1 for r in rows if r[k]) for k in keys}
    summary["n"] = len(rows)
    a.out.write_text(json.dumps({"summary": summary, "bugs": rows}, indent=1))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
