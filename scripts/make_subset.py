"""Freeze the evaluation subset of SWE-bench Lite (run once; the output is committed).

The laptop runs every configuration on the same bugs, so the subset is drawn from few Docker
environments (repo, version): images of one environment share their large base layers. Within an
environment bugs are sampled at random with a fixed seed, so nobody picks easy bugs by hand.

  python scripts/make_subset.py        -> benchmarks/swe_bench_lite/subset.json
"""
import json
import random
from pathlib import Path

SEED = 2026
PLAN = [("django/django", "3.1", 13), ("django/django", "3.2", 13), ("sympy/sympy", "1.1", 12),
        ("pallets/flask", None, 2)]        # flask: every valid bug (different versions), smoke-tested repo



def select(harness: list[dict], valid: dict) -> tuple[list[str], list[dict]]:
    """Deterministic: same harness + validation + SEED -> same subset (checked by the quality scorecard)."""
    rows = [h for h in harness if valid.get(h["instance_id"], {}).get("status") == "VALID"]
    rng = random.Random(SEED)
    ids, groups = [], []
    for repo, version, n in PLAN:
        pool = sorted(h["instance_id"] for h in rows if h["repo"] == repo and (version is None or h["version"] == version))
        pick = sorted(rng.sample(pool, min(n, len(pool))))
        ids += pick
        groups.append({"repo": repo, "version": version, "pool": len(pool), "picked": pick})
    return ids, groups


if __name__ == "__main__":
    harness = json.loads(Path("benchmarks/swe_bench_lite/private/swebench_harness.json").read_text())
    valid = json.loads(Path("results/benchmark_validation.json").read_text())["bugs"]
    ids, groups = select(harness, valid)
    out = Path("benchmarks/swe_bench_lite/subset.json")
    out.write_text(json.dumps(ids, indent=1) + "\n")
    Path("benchmarks/swe_bench_lite/subset_meta.json").write_text(
        json.dumps({"seed": SEED, "n": len(ids), "groups": groups}, indent=1) + "\n")
    print(len(ids), "bugs ->", out)
