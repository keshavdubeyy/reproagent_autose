"""Run a bug report's code against the bug's snapshot and record which repository functions ran.

The report statements run in a separate process (autose/trace/child.py) with the snapshot's own
interpreter, a timeout, a scratch working directory, a cleared environment and the network
blocked. Calls are mapped to Layer 1 index entities. A small baseline (trivial use of the
library) runs first in the same process, so code that runs for any input can be told apart.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

CHILD = Path(__file__).with_name("child.py")
ENVS = Path("envs")
DEADLINE = 60

REPOS = {
    "sympy/sympy": {
        "env": "trace-sympy",
        "preamble": ["from sympy import *",
                     "x, y, z, t, a, b, c, n, m, k = symbols('x y z t a b c n m k')",
                     "f, g, h = symbols('f g h', cls=Function)"],
        "baseline": ["(x + 1)*y - 2", "str(x**2 + y)", "repr(sin(x) + Rational(1, 2))"],
    },
}


def interpreter(repo: str) -> Path | None:
    env = os.environ.get("AUTOSE_TRACE_PYTHON")
    if env:
        return Path(env)
    cfg = REPOS.get(repo)
    if not cfg:
        return None
    exe = ENVS / cfg["env"] / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() else None


def run_statements(snapshot: Path, statements: list[str], preamble: list[str], baseline: list[str],
                   python: Path, deadline: int = DEADLINE) -> dict:
    with tempfile.TemporaryDirectory(prefix="autose_trace_") as tmp:
        job, out = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job.write_text(json.dumps({"snapshot": str(Path(snapshot).resolve()), "preamble": preamble,
                                   "baseline": baseline, "statements": statements, "deadline": deadline,
                                   "out": str(out)}), encoding="utf-8")
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP") if k in os.environ}
        env |= {"MPLBACKEND": "Agg", "PYTHONHASHSEED": "0", "PYTHONWARNINGS": "ignore"}
        try:
            p = subprocess.run([str(python), "-I", "-X", "utf8", str(CHILD.resolve()), str(job)], cwd=tmp, env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=deadline + 30)
            stderr = p.stderr[-2000:]
        except subprocess.TimeoutExpired:
            stderr = "killed after timeout"
        if not out.exists():
            return {"status": "crashed", "stderr": stderr}
        return json.loads(out.read_text(encoding="utf-8")) | {"status": "ran", "stderr": stderr}


class EntityMap:
    """(file, code first line, code name) -> index entity id."""

    def __init__(self, index: dict):
        self.by_file = defaultdict(list)
        for e in index["entities"]:
            self.by_file[e["file"]].append(e)

    def innermost(self, file: str, line: int) -> str | None:
        inside = [e for e in self.by_file.get(file, []) if e["start_line"] <= line <= e["end_line"]]
        return min(inside, key=lambda e: e["end_line"] - e["start_line"])["id"] if inside else None

    def __call__(self, file: str, line: int, name: str) -> str | None:
        if name == "<module>":
            return None
        if not name.startswith("<"):
            # a decorated def's code starts at its first decorator, the index at the def line
            same = [e for e in self.by_file.get(file, []) if e["name"] == name and line <= e["start_line"] <= line + 30]
            if same:
                return min(same, key=lambda e: e["start_line"])["id"]
        return self.innermost(file, line)  # lambdas, comprehensions, unindexed nested code


def trace_instance(repo: str, bug_report, snapshot: Path, index: dict, statements: list[str],
                   deadline: int = DEADLINE) -> dict:
    python = interpreter(repo)
    if not python:
        return {"status": "no_interpreter", "statements": len(statements)}
    if not statements:
        return {"status": "no_code"}
    cfg = REPOS.get(repo, {"preamble": [], "baseline": []})
    raw = run_statements(snapshot, statements, cfg["preamble"], cfg["baseline"], python, deadline)
    if raw["status"] != "ran":
        return raw | {"statements": len(statements)}
    emap = EntityMap(index)

    def entities(rows):
        out = defaultdict(int)
        for f, line, name, count in rows:
            eid = emap(f, line, name)
            if eid:
                out[eid] += count
        return dict(out)

    executed, base = entities(raw["calls"]), entities(raw["baseline_calls"])
    frames = []
    for s in raw["statements"]:
        for f, line, _ in s["frames"]:
            frames.append(emap.innermost(f, line))  # the entity around the line that was running
    return {"status": "ran", "timed_out": raw["timed_out"], "statements": len(statements),
            "ok_statements": sum(s["ok"] for s in raw["statements"]),
            "errors": [{"i": s["i"], "error": s["error"]} for s in raw["statements"] if not s["ok"]][:20],
            "frames": [f for f in dict.fromkeys(frames) if f],
            "executed": executed, "baseline": sorted(base), "stdout": raw.get("stdout", "")[:1500],
            "stderr": raw.get("stderr", "")[-800:]}
