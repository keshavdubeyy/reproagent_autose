"""Build the SWE-bench Lite benchmark folder for Autose.

Result:
  benchmarks/swe_bench_lite/
    public/                     <- what the agents get
      bug_reports.json          one bug report per issue: Description = the real GitHub issue text;
                                Steps/Expected/Observed from MiniMax-2.5 ExploreSpec (no Title, Description or RootCause)
      code/<instance_id>/       repository source at the buggy commit, no .git history
    private/                    <- evaluator only, never given to agents
      test_specs.json           everything needed to check a fix (tests, test patch, gold patch, env)
      repos/<owner__repo>.git   bare clones used to export the code (contain the fix commits)

Steps:
  python scripts/build_benchmark.py reports --source external/brsr_data
  python scripts/build_benchmark.py code              # clones 12 repos, exports 300 snapshots (slow)
  python scripts/build_benchmark.py code --only pallets__flask-4045   # one instance
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

OUT = Path("benchmarks/swe_bench_lite")
REPORT_SOURCE = ("minimax2.5", "explorespec")  # 193/300 resolved (64.3%) with Mini-SWE-Agent V2
# Title, Description and RootCause are dropped: they were written by the refining agent, and RootCause
# hands our agents the answer. The real issue text becomes the Description instead.
REPORT_FIELDS = ["StepsToReproduce", "ExpectedBehavior", "ObservedBehavior"]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def as_list(v):
    return json.loads(v) if isinstance(v, str) and v.strip() else (v or [])


def build_reports(source: Path) -> int:
    data = source / "data"
    originals = {e["instance_id"]: e for f in sorted((data / "by_repo").glob("*.json")) for e in load(f)}
    model, variant = REPORT_SOURCE
    refined = {}
    for f in sorted((data / "output" / "merged" / model / variant).glob("*.json")):
        for e in load(f):
            if isinstance(e.get("problem_statement"), dict):
                refined[e["instance_id"]] = e["problem_statement"]

    public, private = [], []
    for iid, e in sorted(originals.items()):
        report = refined.get(iid)
        public.append({
            "instance_id": iid,
            "repo": e["repo"],
            "base_commit": e["base_commit"],
            "code_path": f"code/{iid}",
            "bug_report": {"Description": e["problem_statement"],
                           **{k: (report or {}).get(k) for k in REPORT_FIELDS}},
        })
        private.append({
            "instance_id": iid,
            "repo": e["repo"],
            "base_commit": e["base_commit"],
            "version": e.get("version"),
            "environment_setup_commit": e.get("environment_setup_commit"),
            "FAIL_TO_PASS": as_list(e.get("FAIL_TO_PASS")),
            "PASS_TO_PASS": as_list(e.get("PASS_TO_PASS")),
            "test_patch": e.get("test_patch", ""),
            "gold_patch": e.get("patch", ""),
            "hints_text": e.get("hints_text", ""),
        })

    (OUT / "public").mkdir(parents=True, exist_ok=True)
    (OUT / "private").mkdir(parents=True, exist_ok=True)
    (OUT / "public" / "bug_reports.json").write_text(json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "private" / "test_specs.json").write_text(json.dumps(private, indent=2, ensure_ascii=False), encoding="utf-8")
    missing = [p["instance_id"] for p in public if p["instance_id"] not in refined]
    print(f"bug_reports.json: {len(public)} issues, refined report missing for {len(missing)} {missing}")
    print(f"test_specs.json: {len(private)} issues")
    return 0


def git(*args: str, capture: bool = False):
    return subprocess.run(["git", *args], check=True, capture_output=capture)


def export_code(only: str | None) -> int:
    specs = load(OUT / "private" / "test_specs.json")
    if only:
        specs = [s for s in specs if s["instance_id"] == only]
    repos_dir = OUT / "private" / "repos"
    code_dir = OUT / "public" / "code"
    repos_dir.mkdir(parents=True, exist_ok=True)
    code_dir.mkdir(parents=True, exist_ok=True)

    problems = {}
    for i, s in enumerate(specs, 1):
        iid, repo = s["instance_id"], s["repo"]
        bare = repos_dir / (repo.replace("/", "__") + ".git")
        if not bare.exists():
            print(f"cloning {repo} ...", flush=True)
            git("clone", "--bare", "--quiet", f"https://github.com/{repo}.git", str(bare))
        dest = code_dir / iid
        done = dest / ".autose_snapshot"
        if done.exists():
            continue
        tar_bytes = git("--git-dir", str(bare), "archive", "--format=tar", s["base_commit"], capture=True).stdout
        dest.mkdir(parents=True, exist_ok=True)
        skipped = []
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            for m in tf.getmembers():
                try:
                    tf.extract(m, dest, filter="data")
                except Exception as exc:  # symlinks out of tree, Windows path limits
                    skipped.append(f"{m.name}: {type(exc).__name__}")
        done.write_text(json.dumps({"instance_id": iid, "repo": repo, "base_commit": s["base_commit"],
                                    "skipped_files": skipped}, indent=2), encoding="utf-8")
        if skipped:
            problems[iid] = skipped
        print(f"[{i}/{len(specs)}] {iid} ({len(skipped)} files skipped)", flush=True)

    print(f"done: {len(specs)} snapshots, {len(problems)} with skipped files")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reports")
    r.add_argument("--source", type=Path, required=True)
    c = sub.add_parser("code")
    c.add_argument("--only", help="export a single instance_id")
    a = ap.parse_args()
    return build_reports(a.source) if a.cmd == "reports" else export_code(a.only)


if __name__ == "__main__":
    sys.exit(main())
