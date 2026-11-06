"""Materialise the multi-location bugs as work/<id>/{buggy,fixed} from ../shopcart/app + bugs.json,
then check that each bug really needs ALL its locations: the hidden oracle must fail on the buggy tree,
fail when any single mutation is reverted alone (only one place fixed), and pass on the fixed tree."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "shopcart" / "app"


def make(dst: Path, mutations: list) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(APP, dst, ignore=shutil.ignore_patterns("__pycache__"))
    for m in mutations:
        f = dst / m["file"]
        src = f.read_text(encoding="utf-8")
        assert src.count(m["old"]) == 1, f"mutation anchor not unique/found: {m['old']!r}"
        f.write_text(src.replace(m["old"], m["new"], 1), encoding="utf-8")


def oracle_ok(tree: Path, code: str) -> bool:
    env = dict(os.environ, PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-c", code], cwd=tree, env=env, capture_output=True).returncode == 0


def main() -> None:
    bugs = json.loads((HERE / "bugs.json").read_text(encoding="utf-8"))["bugs"]
    ns: dict = {}
    exec((HERE / "oracle_checks.py").read_text(encoding="utf-8"), ns)
    tmp = HERE / "work" / "_check"
    for b in bugs:
        out = HERE / "work" / b["id"]
        make(out / "fixed", [])
        make(out / "buggy", b["mutations"])
        code = ns["ORACLES"][b["id"]]
        assert not oracle_ok(out / "buggy", code), f"{b['id']}: oracle passes on buggy"
        assert oracle_ok(out / "fixed", code), f"{b['id']}: oracle fails on fixed"
        for i in range(len(b["mutations"])):     # fix only ONE location -> must still fail
            make(tmp, [m for j, m in enumerate(b["mutations"]) if j != i])
            assert not oracle_ok(tmp, code), f"{b['id']}: fixing location {i} alone passes the oracle"
        print("built + checked", b["id"])
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
