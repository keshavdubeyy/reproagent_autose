"""Validate the benchmark: every bug must FAIL on the buggy code and PASS with the real fix.

For each bug we run the hidden tests twice:
  no_patch -> buggy code as is: the FAIL_TO_PASS tests must fail (the tests detect the bug)
  gold     -> developers' real fix: everything must pass (the environment and tests work)

Status per bug:
  VALID           no_patch not resolved, gold resolved: usable for grading our agents
  GOLD_FAILS      the real fix doesn't pass here (environment or flaky tests); excluded
  ALREADY_PASSES  tests pass even without a fix (they can't detect the bug); excluded
  ERROR           docker or timeout problem; rerun with --redo-errors

Runs in batches and resumes: finished bugs are skipped. Usage:
  python -m autose.evaluation.validate --repo pallets/flask
  python -m autose.evaluation.validate --limit 25          # next 25 unfinished bugs
  python -m autose.evaluation.validate --all
  python -m autose.evaluation.validate --summary
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import docker

from autose.evaluation.evaluator import evaluate, load_harness

PRIVATE = Path("benchmarks/swe_bench_lite/private")
OUT = PRIVATE / "validation"                 # logs contain the gold patch: private
RESULTS = OUT / "results.jsonl"
SUMMARY = Path("results/benchmark_validation.json")  # ids + status only: safe to publish


def load_results() -> dict[str, dict]:
    if not RESULTS.exists():
        return {}
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {r["instance_id"]: r for r in rows}  # last line per bug wins


def status(no_patch: dict, gold: dict) -> str:
    def broken(run):  # infrastructure problem, not a test result
        return run["timed_out"] or (run["error"] or "").startswith("crash:")
    if broken(no_patch) or broken(gold):
        return "ERROR"
    if no_patch["resolved"]:
        return "ALREADY_PASSES"
    if not gold["resolved"]:
        return "GOLD_FAILS"
    return "VALID"


class Prefetcher:
    """Pull the next bug's image in the background while the current bug is tested."""

    def __init__(self, client):
        self.client, self.threads = client, {}

    def pull(self, image: str):
        if image not in self.threads:
            t = threading.Thread(target=self._pull, args=(image,), daemon=True)
            self.threads[image] = t
            t.start()

    def _pull(self, image: str):
        # Docker Hub rate-limits pulls (HTTP 429). Wait it out instead of failing the bug.
        for attempt in range(1, 13):
            if self.has(image):
                return
            try:
                repo, tag = image.rsplit(":", 1)
                self.client.images.pull(repo, tag=tag)
                return
            except Exception as exc:
                limited = "429" in str(exc) or "Too Many Requests" in str(exc)
                wait = 600 if limited else 30
                print(f"  pull failed ({attempt}/12) {image}: {'rate limited by Docker Hub' if limited else exc}; "
                      f"retrying in {wait // 60 or 1} min", flush=True)
                time.sleep(wait)

    def has(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def wait(self, image: str) -> bool:
        self.pull(image)
        self.threads[image].join()
        return self.has(image)


def remove_image(client, image: str):
    try:
        client.images.remove(image, force=True)
    except Exception:
        pass


def write_summary(results: dict[str, dict], total: int) -> Counter:
    counts = Counter(r["status"] for r in results.values())
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps({
        "benchmark": "SWE-bench Lite",
        "total_bugs": total,
        "checked": len(results),
        "counts": dict(sorted(counts.items())),
        "rule": "VALID = hidden tests fail on buggy code and pass with the real fix",
        "bugs": {iid: {"status": r["status"],
                       "no_patch_f2p": f"{r['no_patch']['f2p_passed']}/{r['no_patch']['f2p_total']}",
                       "gold_f2p": f"{r['gold']['f2p_passed']}/{r['gold']['f2p_total']}",
                       "gold_p2p": f"{r['gold']['p2p_passed']}/{r['gold']['p2p_total']}",
                       "note": r["gold"]["error"] or r["no_patch"]["error"]}
                 for iid, r in sorted(results.items())},
    }, indent=2), encoding="utf-8")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="only this repo, e.g. pallets/flask")
    ap.add_argument("--ids", nargs="*", help="only these instance ids")
    ap.add_argument("--limit", type=int, help="stop after this many bugs")
    ap.add_argument("--all", action="store_true", help="every unfinished bug")
    ap.add_argument("--redo-errors", action="store_true", help="rerun bugs whose status is ERROR")
    ap.add_argument("--redo", action="store_true", help="rerun selected bugs even if finished")
    ap.add_argument("--keep-images", action="store_true", help="don't delete images after use")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per test run")
    ap.add_argument("--summary", action="store_true", help="only print and write the summary")
    a = ap.parse_args()

    harness = load_harness()
    done = load_results()
    if a.summary:
        counts = write_summary(done, len(harness))
        print(f"checked {len(done)}/{len(harness)}: {dict(counts)}  -> {SUMMARY}")
        return 0
    if not (a.repo or a.ids or a.limit or a.all):
        ap.error("choose --repo, --ids, --limit or --all")

    # Sorted by repo + version so consecutive images share environment layers.
    todo = sorted(harness.values(), key=lambda r: (r["repo"], r["version"], r["instance_id"]))
    if a.repo:
        todo = [r for r in todo if r["repo"] == a.repo]
    if a.ids:
        todo = [r for r in todo if r["instance_id"] in set(a.ids)]
    if not a.redo:
        todo = [r for r in todo if r["instance_id"] not in done
                or (a.redo_errors and done[r["instance_id"]]["status"] == "ERROR")]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo)} bugs to validate ({len(done)} already done of {len(harness)})", flush=True)

    client = docker.from_env(timeout=1800)
    pre = Prefetcher(client)
    OUT.mkdir(parents=True, exist_ok=True)
    for i, rec in enumerate(todo):
        iid, image = rec["instance_id"], rec["image"]
        t0 = time.time()
        if not pre.wait(image):  # never record a download problem as a bug result
            print(f"stopping: could not download {image}; rerun later to continue", flush=True)
            break
        if i + 1 < len(todo):
            pre.pull(todo[i + 1]["image"])
        pulled = time.time() - t0
        no_patch = evaluate(rec, None, "no_patch", OUT / iid / "no_patch", client, a.timeout).to_dict()
        gold = evaluate(rec, rec["patch"], "gold", OUT / iid / "gold", client, a.timeout).to_dict()
        if not gold["resolved"] and not gold["timed_out"]:  # retry once to separate flaky from broken
            retry = evaluate(rec, rec["patch"], "gold", OUT / iid / "gold_retry", client, a.timeout).to_dict()
            if retry["resolved"]:
                gold = retry | {"error": "passed on retry (flaky)"}
        row = {"instance_id": iid, "repo": rec["repo"], "version": rec["version"],
               "status": status(no_patch, gold), "no_patch": no_patch, "gold": gold,
               "pull_s": round(pulled, 1), "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        done[iid] = row
        if not a.keep_images:
            remove_image(client, image)
        print(f"[{i + 1}/{len(todo)}] {iid}: {row['status']}  "
              f"no_patch F2P {no_patch['f2p_passed']}/{no_patch['f2p_total']}, "
              f"gold F2P {gold['f2p_passed']}/{gold['f2p_total']} P2P {gold['p2p_passed']}/{gold['p2p_total']}  "
              f"(pull {pulled:.0f}s, tests {no_patch['runtime_s'] + gold['runtime_s']:.0f}s)"
              + (f"  note: {gold['error'] or no_patch['error']}" if (gold['error'] or no_patch['error']) else ""),
              flush=True)

    counts = write_summary(done, len(harness))
    print(f"batch done. overall {len(done)}/{len(harness)}: {dict(counts)}", flush=True)
    return 0


if __name__ == "__main__":
    if not sys.flags.utf8_mode:  # test logs and patches are UTF-8; Windows defaults to cp1252
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", "-m", "autose.evaluation.validate", *sys.argv[1:]]))
    sys.exit(main())
