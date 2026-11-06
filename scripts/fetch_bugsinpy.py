"""Materialise selected BugsInPy bugs as (buggy, fixed) source trees.

For every bug in benchmarks/bugsinpy/manifest.json we
  1. download the GitHub tarball of the *buggy* commit (no full git history -> small),
  2. extract it to  benchmarks/bugsinpy/work/<id>/buggy
  3. copy it to     benchmarks/bugsinpy/work/<id>/fixed  and apply BugsInPy's bug_patch.txt
The bug_patch.txt files ship in benchmarks/bugsinpy/patches/<project>/<n>.patch
(copied from https://github.com/soarsmu/BugsInPy).

Usage:  python scripts/fetch_bugsinpy.py [--only youtube-dl_3 ...]
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks" / "bugsinpy"
WORK = BENCH / "work"


def download_tree(github_url: str, commit: str, dest: Path) -> None:
    owner_repo = github_url.rstrip("/").split("github.com/")[1]
    url = f"https://codeload.github.com/{owner_repo}/tar.gz/{commit}"
    print(f"  downloading {url}")
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        members = tar.getmembers()
        prefix = members[0].name.split("/")[0] + "/"
        dest.mkdir(parents=True, exist_ok=True)
        for m in members:
            if not m.name.startswith(prefix) or m.issym() or m.islnk():
                continue
            rel = m.name[len(prefix):]
            if not rel or ".." in Path(rel).parts:
                continue
            target = dest / rel
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src:
                    target.write_bytes(src.read())


def apply_patch(tree: Path, patch: Path) -> None:
    res = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch)],
        cwd=tree, capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"patch failed in {tree}: {res.stderr}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    for bug in manifest["bugs"]:
        bid = bug["id"]
        if args.only and bid not in args.only:
            continue
        out = WORK / bid
        if out.exists() and not args.force:
            print(f"[skip] {bid} already materialised")
            continue
        if out.exists():
            shutil.rmtree(out)
        print(f"[fetch] {bid}")
        buggy, fixed = out / "buggy", out / "fixed"
        download_tree(bug["github_url"], bug["buggy_commit"], buggy)
        shutil.copytree(buggy, fixed)
        apply_patch(fixed, BENCH / "patches" / bug["patch"])
        print(f"  ok -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
