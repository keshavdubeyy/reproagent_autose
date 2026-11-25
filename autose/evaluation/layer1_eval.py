"""Evaluate Layer 1 (repository_index.json) on SWE-bench Lite snapshots. Evaluator-only:
it reads the gold patches and test lists, which agents must never see.

Metrics per instance:
  entities   independent ast walk vs the index: recall / precision of (file, name, start, end)
  gold       where the developers' fix is: files in the index, edited lines that fall inside
             an indexed function/method/class (else module-level code), gold entities
  tests      PASS_TO_PASS tests (exist at the buggy commit) that the index tags as tests
  graph      relationship counts, scoped vs global call edges, call out-degree
  cost       build seconds, file size

  python -m autose.evaluation.layer1_eval --ids <instance_id> ...     (indexes must exist)
  python -m autose.evaluation.layer1_eval --built                     (every workspace index)
Writes results/layer1_eval.json (metrics and ids only, no patch text).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

SPECS = Path("benchmarks/swe_bench_lite/private/test_specs.json")
CODE = Path("benchmarks/swe_bench_lite/public/code")
WORKSPACES = Path("workspaces")
OUT = Path("results/layer1_eval.json")


# ---------- entities: independent ground truth ----------
def ast_entities(repo: Path, files: list[str]) -> set[tuple]:
    """Every class/def with its dotted name and lines, by a plain recursive walk."""
    out = set()

    def walk(node, prefix, path):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                out.add((path, name, child.lineno, child.end_lineno))
                walk(child, name, path)
            else:
                walk(child, prefix, path)

    for path in files:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse((repo / path).read_text(encoding="utf-8"))
            walk(tree, "", path)
        except (SyntaxError, ValueError, UnicodeDecodeError):
            pass
    return out


def entity_metrics(index: dict, repo: Path) -> dict:
    truth = ast_entities(repo, [f["id"] for f in index["files"]])
    got = {(e["file"], e["id"].split(":", 1)[1], e["start_line"], e["end_line"]) for e in index["entities"]}
    # same-named definitions in one file share an id (kept from the original builder): truth has
    # every copy, the index only the last one; count them apart from real misses
    names = Counter((t[0], t[1]) for t in truth)
    shadowed = {t for t in truth - got if names[(t[0], t[1])] > 1}
    hit = len(truth & got)
    return {"truth": len(truth), "indexed": len(got), "recall": round(hit / max(len(truth), 1), 4),
            "precision": round(hit / max(len(got), 1), 4), "shadowed_same_name": len(shadowed),
            "recall_unique_names": round(hit / max(len(truth) - len(shadowed), 1), 4)}


# ---------- gold patch location ----------
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def patch_old_lines(patch: str) -> dict[str, set[int] | None]:
    """file -> buggy-side line numbers the patch touches (None for a new file)."""
    files: dict[str, set[int] | None] = {}
    cur, old_no, is_new = None, 0, False
    for line in patch.splitlines():
        if line.startswith("--- "):
            is_new = line.strip() == "--- /dev/null"
        elif line.startswith("+++ "):
            cur = line[6:].strip() if line.startswith("+++ b/") else None
            if cur:
                files[cur] = None if is_new else files.get(cur) or set()
        elif (m := HUNK.match(line)) and cur and files[cur] is not None:
            old_no = int(m.group(1))
        elif cur and files[cur] is not None and line[:1] in ("-", "+", " "):
            if line.startswith("-"):
                files[cur].add(old_no)
                old_no += 1
            elif line.startswith("+"):
                files[cur].add(max(old_no - 1, 1))  # insertion point: the line before
            else:
                old_no += 1
    return files


def gold_metrics(index: dict, patch: str) -> dict:
    by_file = defaultdict(list)
    for e in index["entities"]:
        by_file[e["file"]].append(e)
    indexed_files = {f["id"] for f in index["files"]}
    edited = patch_old_lines(patch)
    py_files = {f: lines for f, lines in edited.items() if f.endswith(".py")}
    lines_total = lines_in_entity = 0
    gold_entities = set()
    for f, lines in py_files.items():
        if not lines:
            continue
        for ln in lines:
            lines_total += 1
            inside = [e for e in by_file.get(f, []) if e["start_line"] <= ln <= e["end_line"]]
            if inside:
                lines_in_entity += 1
                gold_entities.add(min(inside, key=lambda e: e["end_line"] - e["start_line"])["id"])
    existing = [f for f, lines in py_files.items() if lines is not None]
    return {"py_files": len(py_files), "new_files": sum(l is None for l in py_files.values()),
            "files_in_index": sum(f in indexed_files for f in existing), "files_existing": len(existing),
            "lines": lines_total, "lines_in_entity": lines_in_entity,
            "gold_entities": sorted(gold_entities)}


# ---------- tests ----------
def map_test_name(name: str) -> tuple[str, list[str]]:
    """Map a SWE-bench test name to candidate index ids: ('exact', [...]) or ('name', [bare])."""
    name = re.sub(r"\[.*\]$", "", name.strip())
    if "::" in name:  # pytest: path::Class::test
        parts = name.split("::")
        return "exact", [f"{parts[0]}:{'.'.join(parts[1:])}"]
    m = re.match(r"^(\w+) \(([\w.]+)\)", name)  # unittest / Django: test (module.Class)
    if m:
        test, dotted = m.groups()
        mod, cls = dotted.rsplit(".", 1)
        base = mod.replace(".", "/")
        return "exact", [f"{root}{base}.py:{cls}.{test}" for root in ("", "tests/")] + \
                        [f"{root}{base}/__init__.py:{cls}.{test}" for root in ("", "tests/")]
    return "name", [name.split(".")[-1]]  # sympy and others give bare names


def test_metrics(index: dict, p2p: list[str]) -> dict:
    tagged = set(index["tests"]["functions"])
    tagged_names = {t.split(":")[-1].split(".")[-1] for t in tagged}
    all_ids = {e["id"] for e in index["entities"]}
    all_names = {e["name"] for e in index["entities"]}
    found = exists = 0
    misses = []
    for t in p2p:
        mode, cands = map_test_name(t)
        if mode == "exact":
            present = [c for c in cands if c in all_ids]
            if not present:
                continue  # parametrised / generated / outside the index: can't judge
            exists += 1
            found += any(c in tagged for c in present)
            if not any(c in tagged for c in present):
                misses.append(t)
        else:
            if cands[0] not in all_names:
                continue  # not defined at the buggy commit (added by the test patch): can't judge
            exists += 1
            found += cands[0] in tagged_names
            if cands[0] not in tagged_names:
                misses.append(t)
    return {"p2p_total": len(p2p), "p2p_located": exists, "tagged": found,
            "recall": round(found / max(exists, 1), 4), "miss_examples": misses[:3]}


# ---------- graph + cost ----------
def graph_metrics(index: dict) -> dict:
    out_deg = Counter(r["source"] for r in index["relationships"] if r["type"] == "invokes")
    degs = sorted(out_deg.values()) or [0]
    s = index["repository"]["stats"]
    return {"relationships": s["relationships"], "call_resolution": s["call_resolution"],
            "invokes_out_degree": {"median": statistics.median(degs), "p95": degs[int(0.95 * (len(degs) - 1))],
                                   "max": degs[-1]}}


def evaluate(iid: str, spec: dict) -> dict:
    path = WORKSPACES / iid / "repository_index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    s = index["repository"]["stats"]
    return {"instance_id": iid, "repo": spec["repo"],
            "cost": {"build_seconds": s["build_seconds"], "mb": round(path.stat().st_size / 1e6, 2),
                     "files": s["files"], "entities": s["classes"] + s["methods"] + s["functions"],
                     "parse_errors": s["parse_errors"]},
            "entities": entity_metrics(index, CODE / iid),
            "gold": gold_metrics(index, spec["gold_patch"]),
            "tests": test_metrics(index, spec["PASS_TO_PASS"]),
            "graph": graph_metrics(index)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--built", action="store_true", help="every instance with a workspace index")
    a = ap.parse_args()
    specs = {s["instance_id"]: s for s in json.loads(SPECS.read_text(encoding="utf-8"))}
    ids = a.ids or sorted(p.parent.name for p in WORKSPACES.glob("*/repository_index.json") if p.parent.name in specs)
    rows = []
    for iid in ids:
        r = evaluate(iid, specs[iid])
        rows.append(r)
        g, t, e = r["gold"], r["tests"], r["entities"]
        print(f"{iid:36} entities R={e['recall']:.3f} P={e['precision']:.3f} (shadowed {e['shadowed_same_name']}) | "
              f"gold files {g['files_in_index']}/{g['files_existing']} lines-in-entity {g['lines_in_entity']}/{g['lines']} | "
              f"tests {t['tagged']}/{t['p2p_located']} | {r['cost']['build_seconds']}s {r['cost']['mb']}MB", flush=True)

    def ratio(key, num, den):
        n, d = sum(r[key][num] for r in rows), sum(r[key][den] for r in rows)
        return {"num": n, "den": d, "rate": round(n / max(d, 1), 4)}

    summary = {
        "instances": len(rows),
        "entity_recall_unique_names": round(statistics.mean(r["entities"]["recall_unique_names"] for r in rows), 4),
        "entity_precision": round(statistics.mean(r["entities"]["precision"] for r in rows), 4),
        "gold_files_in_index": ratio("gold", "files_in_index", "files_existing"),
        "gold_lines_in_entity": ratio("gold", "lines_in_entity", "lines"),
        "instances_with_gold_entity": sum(bool(r["gold"]["gold_entities"]) for r in rows),
        "p2p_tests_tagged": ratio("tests", "tagged", "p2p_located"),
        "build_seconds": {"median": statistics.median(r["cost"]["build_seconds"] for r in rows),
                          "max": max(r["cost"]["build_seconds"] for r in rows)},
        "mb": {"median": statistics.median(r["cost"]["mb"] for r in rows), "max": max(r["cost"]["mb"] for r in rows)},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    public_rows = [{k: v for k, v in r.items() if k != "gold"} | {"gold": {k: v for k, v in r["gold"].items()
                                                                          if k != "gold_entities"}} for r in rows]
    OUT.write_text(json.dumps({"summary": summary, "instances": public_rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
