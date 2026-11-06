"""Materialise BugsInPy bugs as buggy/fixed snapshots + per-project venvs, and validate ground truth.

usage: python benchmarks/setup_bugsinpy.py tqdm:1 thefuck:2 ...
Creates benchmarks/repos/<project>-<n>/{buggy,fixed}, benchmarks/envs/<project>/ and
benchmarks/repos/<project>-<n>/meta.json (incl. whether the ground-truth test is fail-to-pass).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
BIP = CACHE / "BugsInPy" / "projects"
REPOS, ENVS = HERE / "repos", HERE / "envs"
PYVER = "3.8"


def sh(cmd, cwd=None, check=True, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r


def info(path: Path) -> dict:
    return dict(re.findall(r'(\w+)="([^"]*)"', path.read_text()))


def snapshot(src: Path, commit: str, dest: Path):
    if dest.exists():
        return
    dest.mkdir(parents=True)
    sh(f"git archive {commit} | tar -x -C '{dest}'", cwd=src)


def ensure_env(project: str, n: str, fixed: Path) -> Path:
    env = ENVS / f"{project}-{n}"  # one env per bug: shared envs got polluted by conflicting pins
    py = env / "bin" / "python"
    marker = env / f".bug{n}"
    if marker.exists():
        return py
    if not py.exists():
        sh(["uv", "venv", "-q", "-p", PYVER, str(env)])
    req = BIP / project / "bugs" / n / "requirements.txt"
    pkgs = ["pytest<7", "mock", "nose", "setuptools<70", "importlib_metadata>=6"]
    sh(["uv", "pip", "install", "-q", "-p", str(py), *pkgs])
    if req.exists():  # best effort: pinned deps from BugsInPy, skip ones that fail
        for line in req.read_text().split():
            r = sh(["uv", "pip", "install", "-q", "-p", str(py), line], check=False, timeout=300)
            if r.returncode:  # old pin not installable on this platform: fall back to unpinned
                sh(["uv", "pip", "install", "-q", "-p", str(py), re.split(r"[=<>]", line)[0]], check=False, timeout=300)
    sh(["uv", "pip", "install", "-q", "-p", str(py), "--no-deps", "-e", str(fixed)], check=False)
    marker.touch()
    return py


def run_gt(py: Path, root: Path, test_cmd: str, test_file: str, test_src: Path) -> int:
    """Run the BugsInPy ground-truth test (taken from the fixed version) inside `root`."""
    tmp = CACHE / "gt" / root.name
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(root, tmp)
    (tmp / test_file).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(test_src, tmp / test_file)
    node = test_cmd.split()[-1]
    r = sh([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp), node], cwd=tmp, check=False, timeout=600)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode


def setup(project: str, n: str) -> dict:
    meta = info(BIP / project / "project.info")
    bug = info(BIP / project / "bugs" / n / "bug.info")
    src = CACHE / "src" / project
    if not src.exists():
        sh(["git", "clone", "-q", "--filter=blob:none", meta["github_url"], str(src)])
    out = REPOS / f"{project}-{n}"
    snapshot(src, bug["buggy_commit_id"], out / "buggy")
    snapshot(src, bug["fixed_commit_id"], out / "fixed")
    py = ensure_env(project, n, out / "fixed")
    test_cmd = (BIP / project / "bugs" / n / "run_test.sh").read_text().strip().splitlines()[-1]
    gt_src = out / "fixed" / bug["test_file"]
    rc_buggy = run_gt(py, out / "buggy", test_cmd, bug["test_file"], gt_src)
    rc_fixed = run_gt(py, out / "fixed", test_cmd, bug["test_file"], gt_src)
    m = {"id": f"{project}-{n}", "project": project, "github": meta["github_url"], **bug,
         "test_cmd": test_cmd, "python": str(py.relative_to(HERE)),
         "gt_fail_to_pass": rc_buggy != 0 and rc_fixed == 0, "rc_buggy": rc_buggy, "rc_fixed": rc_fixed}
    (out / "meta.json").write_text(json.dumps(m, indent=2))
    return m


if __name__ == "__main__":
    for spec in sys.argv[1:]:
        p, n = spec.split(":")
        try:
            m = setup(p, n)
            print(f"{m['id']:16s} gt_fail_to_pass={m['gt_fail_to_pass']} (buggy rc={m['rc_buggy']}, fixed rc={m['rc_fixed']})")
        except Exception as e:
            print(f"{p}-{n}: SETUP FAILED: {str(e)[:300]}")
