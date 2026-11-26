"""Execution-trace tests: statement extraction from reports, and the sandboxed traced run."""
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from autose.repo_index import build_index
from autose.trace import statements, trace_instance

REPORT = {
    "Description": dedent("""\
        Mod is wrong. The eval logic:

        ```
        if (p == q or
                p.is_integer):
            return S.Zero
        ```
        so
        ```
        >>> Mod(x**2, x)
        0
        ```
        """),
    "StepsToReproduce": "Execute `from sympy import Mod, Symbol`\nCreate a symbol: `x = Symbol('x')`\n"
                        "Run print(Mod(x**2, x)) and observe it is 0\nObserve the `Mod` result",
}


def test_statements_from_fences_sessions_and_steps():
    got = statements(REPORT)
    assert "Mod(x**2, x)" in got                          # doctest prompt stripped, output dropped
    assert "from sympy import Mod, Symbol" in got and "x = Symbol('x')" in got   # inline step code
    assert "print(Mod(x**2, x))" in got                    # unquoted step code
    assert "Mod" not in got                                 # a bare name is not a statement
    assert all("return S.Zero" not in s or s.startswith("if") for s in got)  # quoted source kept whole or dropped
    steps = {"Description": "```\n>>>convert_to(joule*second,joule)\n    joule**(7/9)\n```",
             "StepsToReproduce": ["Import units: from sympy.physics.units import *", "Run convert_to(joule, second)",
                                  "Observe the returned value is joule**(7/9)"]}
    assert statements(steps) == ["from sympy.physics.units import *", "convert_to(joule*second,joule)",
                                 "convert_to(joule, second)"]   # list steps; '>>>' with no space; output dropped;
    #                                                             imports first


FILES = {
    "pkg/__init__.py": "",
    "pkg/m.py": dedent("""\
        import functools


        def deco(fn):
            @functools.wraps(fn)
            def inner(*a):
                return fn(*a)
            return inner


        def f(x):
            return g(x) + 1


        def g(x):
            return 1 / x


        class C:
            @deco
            def meth(self):
                return [g(i) for i in (1, 2)]
        """),
}


@pytest.fixture()
def repo(tmp_path):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_traced_run_maps_calls_frames_and_blocks_network(repo, monkeypatch):
    monkeypatch.setenv("AUTOSE_TRACE_PYTHON", sys.executable)
    index = build_index(repo, "deadbeef")
    code = ["from pkg.m import f, C", "f(2)", "C().meth()", "f(0)", "undefined_name + 1",
            "import socket\nsocket.create_connection(('example.com', 80))"]
    res = trace_instance("toy/toy", {}, repo, index, code, deadline=30)
    assert res["status"] == "ran" and res["ok_statements"] == 3
    ex = res["executed"]
    assert ex["pkg/m.py:f"] == 2 and ex["pkg/m.py:g"] == 4 and "pkg/m.py:C.meth" in ex  # decorated method found
    assert res["frames"][-1] == "pkg/m.py:g"               # the raising function, innermost last
    errors = {e["i"]: e["error"] for e in res["errors"]}
    assert errors[3].startswith("ZeroDivisionError") and errors[4].startswith("NameError")
    assert "network disabled" in errors[5]


def test_deadline_keeps_partial_trace(repo, monkeypatch):
    monkeypatch.setenv("AUTOSE_TRACE_PYTHON", sys.executable)
    index = build_index(repo, "deadbeef")
    res = trace_instance("toy/toy", {}, repo, index, ["from pkg.m import f", "f(1)", "while True: pass"], deadline=3)
    assert res["status"] == "ran" and res["timed_out"] and res["executed"].get("pkg/m.py:f") == 1


def test_layer2_restricted_to_code_that_ran(repo):
    from autose.retrieval import Retriever
    from autose.trace.rank import ranking
    index = build_index(repo, "deadbeef")
    ran = {"pkg/m.py:f", "pkg/m.py:g"}
    out = Retriever(index, repo).retrieve("C.meth and g divide by zero", restrict=ran)
    assert {e["id"] for e in out["entities"]} <= ran and out["entities"]
    trace = {"status": "ran", "executed": {"pkg/m.py:f": 1, "pkg/m.py:g": 1, "pkg/m.py:deco": 1},
             "baseline": ["pkg/m.py:deco"], "frames": ["pkg/m.py:f", "pkg/m.py:g"]}
    assert ranking(trace, "divide by zero", index, repo) == ["pkg/m.py:g", "pkg/m.py:f"]  # innermost frame first


def test_trace_code_never_reads_evaluation_data():
    src = "\n".join(p.read_text(encoding="utf-8") for p in Path("autose/trace").glob("*.py"))
    for banned in ("private", "test_specs", "gold_patch", "autose.evaluation", "external/", "FAIL_TO_PASS"):
        assert banned not in src
