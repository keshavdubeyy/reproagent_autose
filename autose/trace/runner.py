"""Run a bug report's code against the bug's snapshot and record which repository functions ran.

The report statements run in a separate process (autose/trace/child.py) with the snapshot's own
interpreter, a timeout, a scratch working directory, a cleared environment and the network
blocked. Calls are mapped to Layer 1 index entities. A small baseline (trivial use of the
library) runs first in the same process, so code that runs for any input can be told apart.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

CHILD = Path(__file__).with_name("child.py")
ENVS = Path("envs")
DEADLINE = 60

REPOS = {
    # env: envs/<env> (python + the snapshot's dependencies); preamble: run first, untraced;
    # baseline: trivial use of the library (its code is "generic"); path: extra sys.path entries
    # inside the snapshot (src layouts); display: after an expression, call its _repr_png_ as a
    # notebook would (seaborn.objects draws only then); test_runner: "plain" (module-level test_*
    # functions, sympy's own pytest stand-in) or "pytest" (fixtures, test classes); env_vars:
    # paths under envs/, made absolute.
    "sympy/sympy": {
        "env": "trace-sympy",
        "preamble": ["from sympy import *",
                     "x, y, z, t, a, b, c, n, m, k = symbols('x y z t a b c n m k')",
                     "f, g, h = symbols('f g h', cls=Function)"],
        "baseline": ["(x + 1)*y - 2", "str(x**2 + y)", "repr(sin(x) + Rational(1, 2))"],
    },
    # Environments as SWE-bench's own (swebench/harness/constants/python.py at v3.0.17,
    # SPECS_SEABORN / SPECS_FLASK), so the sandbox behaves like the containers that grade:
    # seaborn 0.12-0.14: Python 3.9, numpy 1.25.2, pandas 2.0.0, matplotlib 3.7.2, scipy 1.11.2 (+ pins)
    "mwaskom/seaborn": {
        "env": "trace-seaborn",
        "preamble": ["import matplotlib", "matplotlib.use('Agg')", "import numpy as np", "import pandas as pd",
                     "import matplotlib.pyplot as plt", "import seaborn as sns", "import seaborn.objects as so"],
        "baseline": ["sns.set_theme()", "plt.figure()", "plt.plot([1, 2], [1, 2])", "plt.close('all')"],
        "display": True,
        "test_runner": "pytest",
        "env_vars": {"SEABORN_DATA": "seaborn-data", "MPLCONFIGDIR": "mplconfig"},
    },
    # flask 2.0: Python 3.9, Werkzeug 2.3.7, Jinja2 3.0.1, click 8.0.1, MarkupSafe 2.1.3;
    # flask 2.2-3.1: Python 3.11, Werkzeug 2.3.7, Jinja2 3.1.2, click 8.1.3, MarkupSafe 2.1.1 (+ blinker 1.6.2,
    # a dependency of the 2.3 snapshots); chosen by the snapshot's own __version__
    "pallets/flask": {
        "env": "trace-flask23",
        "envs": {"2.0": "trace-flask20", "2.1": "trace-flask23", "2.2": "trace-flask23", "2.3": "trace-flask23"},
        "version_file": "src/flask/__init__.py",
        "preamble": ["import os", "import tomllib", "import flask", "from flask import Flask, Blueprint, request",
                     "app = Flask('app')"],
        "baseline": ["Flask('baseline')"],
        "path": ["src"],
        "test_runner": "pytest",
    },
}


def job_options(repo: str) -> tuple[dict, dict]:
    """(child.py job keys, environment variables) for a repository's sandbox."""
    cfg = REPOS.get(repo, {})
    job = {"path": cfg.get("path", []), "display": cfg.get("display", False),
           "test_runner": cfg.get("test_runner", "plain")}
    env = {k: str((ENVS / v).resolve()) for k, v in cfg.get("env_vars", {}).items()}
    return job, env


def env_name(repo: str, snapshot: Path | None = None) -> str | None:
    """The sandbox for a repository, per version when it has several (read from the snapshot's
    own __version__, e.g. flask 2.0 vs 2.3)."""
    cfg = REPOS.get(repo)
    if not cfg:
        return None
    if cfg.get("envs") and snapshot is not None:
        try:
            text = (Path(snapshot) / cfg["version_file"]).read_text(encoding="utf-8")
            m = re.search(r"__version__\s*=\s*['\"](\d+)\.(\d+)", text)
            if m and f"{m.group(1)}.{m.group(2)}" in cfg["envs"]:
                return cfg["envs"][f"{m.group(1)}.{m.group(2)}"]
        except OSError:
            pass
    return cfg["env"]


def interpreter(repo: str, snapshot: Path | None = None) -> Path | None:
    env = os.environ.get("AUTOSE_TRACE_PYTHON")
    if env:
        return Path(env)
    name = env_name(repo, snapshot)
    if not name:
        return None
    exe = ENVS / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() else None


def run_statements(snapshot: Path, statements: list[str], preamble: list[str], baseline: list[str],
                   python: Path, deadline: int = DEADLINE, extra: dict | None = None,
                   repo: str | None = None) -> dict:
    """extra: further job keys for child.py (overrides: edited files; trace: False; tests: files);
    repo: adds that repository's sandbox options (job_options)."""
    opts, env_vars = job_options(repo) if repo else ({}, {})
    with tempfile.TemporaryDirectory(prefix="autose_trace_") as tmp:
        job, out = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job.write_text(json.dumps({"snapshot": str(Path(snapshot).resolve()), "preamble": preamble,
                                   "baseline": baseline, "statements": statements, "deadline": deadline,
                                   "out": str(out)} | opts | (extra or {})), encoding="utf-8")
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP") if k in os.environ}
        env |= {"MPLBACKEND": "Agg", "PYTHONHASHSEED": "0", "PYTHONWARNINGS": "ignore"} | env_vars
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
    python = interpreter(repo, snapshot)
    if not python:
        return {"status": "no_interpreter", "statements": len(statements)}
    if not statements:
        return {"status": "no_code"}
    cfg = REPOS.get(repo, {"preamble": [], "baseline": []})
    raw = run_statements(snapshot, statements, cfg["preamble"], cfg["baseline"], python, deadline, repo=repo)
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
