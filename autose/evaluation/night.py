"""Night runner: every configuration on every bug, one SWE-bench image at a time.

    python -m autose.evaluation.night --configs A C --tag night1              # 40-bug subset
    python -m autose.evaluation.night --configs D E --tag night2
    python -m autose.evaluation.night --configs A C D E --ids pallets__flask-4045 --tag smoke

For each bug, all requested configurations run back to back (the image is pulled once), the patch is
graded with the hidden tests, and the image is deleted afterwards (images that were already on the
machine are kept). Results: runs/<tag>_<config>/summary.jsonl, one line per bug; rerunning the same
command resumes where it stopped. Ctrl-C / SIGTERM removes the run's containers before exiting. The run
stops by itself when free disk falls below --min-free-gb.

Configurations (the paper's ablation; B = vector RAG is not built):
  A  mid-term agent: keyword search, test-guided repair                 (baseline)
  C  + knowledge package, hybrid search (BM25 + names + traceback)
  D  + code graph re-ranking; search results handed to the fault localiser
  E  D + independent review + loop controller                          (full design)
"""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import time
from pathlib import Path

from autose.agent.llm import DEFAULT_MODEL, LLM
from autose.run import PUBLIC, load_reports, run_one
from .evaluator import load_harness

SUBSET = Path("benchmarks/swe_bench_lite/subset.json")
BASE = dict(mode="full", budget=4, fix=True, fix_mode="fix_full", report="full", timeout=180,
            check_f2p=True, grade=True, review="off", loop_control=False)
CONFIGS = {
    "A": dict(retrieval="explorer"),
    "C": dict(retrieval="hybrid"),
    "D": dict(retrieval="graph"),
    "E": dict(retrieval="graph", review="rules", loop_control=True),
}
CONTAINER_PREFIXES = ("autose-agent-", "autose-eval-")


def cleanup_containers(client) -> int:
    n = 0
    for c in client.containers.list(all=True):
        if c.name.startswith(CONTAINER_PREFIXES):
            try:
                c.remove(force=True)
                n += 1
            except Exception:
                pass
    return n


def image_of(harness: dict, iid: str) -> str:
    from autose.agent.workspace import swebench_image
    return harness[iid].get("image") or swebench_image(iid)


def free_gb() -> float:
    return shutil.disk_usage("/").free / 1e9


def done_ids(summary: Path) -> set[str]:
    if not summary.exists():
        return set()
    return {json.loads(l)["instance_id"] for l in summary.read_text().splitlines() if l.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", nargs="+", default=["A", "C"], choices=sorted(CONFIGS))
    ap.add_argument("--tag", required=True, help="run name prefix, e.g. night1")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--subset", type=Path, default=SUBSET)
    ap.add_argument("--limit", type=int, help="only the first N bugs")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--min-free-gb", type=float, default=6.0)
    a = ap.parse_args()

    import docker
    client = docker.from_env(timeout=1800)
    ids = a.ids or json.loads(a.subset.read_text())
    ids = ids[:a.limit] if a.limit else ids
    reports, harness = load_reports(), load_harness()
    llm = LLM(a.model, seed=a.seed)
    preexisting = {t for img in client.images.list() for t in img.tags}
    log = Path("runs") / f"{a.tag}.log"
    log.parent.mkdir(exist_ok=True)

    def say(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def stop(signum, _frame):
        say(f"signal {signum}: removed {cleanup_containers(client)} container(s); exiting")
        sys.exit(130)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    n_old = cleanup_containers(client)
    say(f"start: configs {a.configs} on {len(ids)} bugs, seed {a.seed}, model {a.model}; "
        f"removed {n_old} leftover container(s); {free_gb():.0f} GB free")
    dirs = {c: Path("runs") / f"{a.tag}_{c}" for c in a.configs}
    for c, d in dirs.items():
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps({"config": c, **BASE, **CONFIGS[c], "seed": a.seed,
                                                   "model": a.model}, indent=2), encoding="utf-8")
    t_start = time.time()
    for i, iid in enumerate(ids, 1):
        if not (PUBLIC / "code" / iid).exists():
            say(f"[{i}/{len(ids)}] {iid}: no code snapshot, skipped")
            continue
        todo = [c for c in a.configs if iid not in done_ids(dirs[c] / "summary.jsonl")]
        if not todo:
            continue
        if free_gb() < a.min_free_gb:
            say(f"only {free_gb():.1f} GB free (< {a.min_free_gb}); stopping. Free space and rerun to resume.")
            break
        for c in todo:
            args = argparse.Namespace(**{**BASE, **CONFIGS[c], "seed": a.seed})
            row = run_one(args, iid, dirs[c], llm, reports, harness)
            row["config"] = c
            with (dirs[c] / "summary.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            say(f"[{i}/{len(ids)}] {iid} {c}: verdict={row.get('verdict')} f2p_valid={row.get('fail_to_pass')} "
                f"fix={row.get('fix_status')} resolved={row.get('resolved')} "
                f"{'ERROR ' + row['error'][:120] if row.get('error') else ''}({row['wall_s']:.0f}s)")
            cleanup_containers(client)
        img = image_of(harness, iid)
        if not a.keep_images and img not in preexisting:
            try:
                client.images.remove(img, force=True)
                say(f"    removed image {img}; {free_gb():.0f} GB free")
            except Exception as exc:
                say(f"    could not remove {img}: {exc}")
    hours = (time.time() - t_start) / 3600
    for c, d in dirs.items():
        rows = [json.loads(l) for l in (d / "summary.jsonl").read_text().splitlines() if l.strip()] \
            if (d / "summary.jsonl").exists() else []
        say(f"{c}: {len(rows)} bugs · reproduced {sum(r.get('verdict') == 'reproduced' for r in rows)} · "
            f"valid {sum(bool(r.get('fail_to_pass')) for r in rows)} · fixed {sum(r.get('fix_status') == 'fixed' for r in rows)} · "
            f"resolved {sum(bool(r.get('resolved')) for r in rows)} · errors {sum('error' in r for r in rows)}")
    say(f"done in {hours:.1f} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
