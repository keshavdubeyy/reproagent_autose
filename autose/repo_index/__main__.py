"""Build repository_index.json.

  python -m autose.repo_index <repo_path> [--commit SHA] [-o repository_index.json]
  python -m autose.repo_index --instance pallets__flask-4045 [django__django-11099 ...]
  python -m autose.repo_index --repo sympy/sympy pallets/flask --skip-existing
      -> reads benchmarks/swe_bench_lite/public/code/<id>, writes workspaces/<id>/repository_index.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autose.repo_index.build import build_index, write_index

CODE = Path("benchmarks/swe_bench_lite/public/code")
WORKSPACES = Path("workspaces")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo_path", nargs="?")
    ap.add_argument("--commit")
    ap.add_argument("-o", "--output", default="repository_index.json")
    ap.add_argument("--instance", nargs="*", help="SWE-bench Lite instance ids (uses the public snapshots)")
    ap.add_argument("--repo", nargs="*", help="every instance of these repos, e.g. sympy/sympy")
    ap.add_argument("--skip-existing", action="store_true", help="don't rebuild indexes that exist")
    a = ap.parse_args()
    ids = list(a.instance or [])
    if a.repo:
        reports = json.loads((CODE.parent / "bug_reports.json").read_text(encoding="utf-8"))
        ids += sorted(b["instance_id"] for b in reports if b["repo"] in a.repo)
    if not a.repo_path and not ids:
        ap.error("give a repo_path, --instance or --repo")

    jobs = [(Path(a.repo_path), a.commit, Path(a.output))] if a.repo_path else \
           [(CODE / iid, None, WORKSPACES / iid / "repository_index.json") for iid in ids]
    for repo, commit, out in jobs:
        if a.skip_existing and out.exists():
            continue
        index = build_index(repo, commit)
        write_index(index, out)
        s = index["repository"]["stats"]
        print(f"{out}: {s['files']} files, {s['classes']} classes, {s['methods']} methods, "
              f"{s['functions']} functions, {sum(s['relationships'].values())} relationships, "
              f"{s['test_functions']} tests, {s['parse_errors']} parse errors "
              f"({s['build_seconds']}s, {out.stat().st_size / 1e6:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
