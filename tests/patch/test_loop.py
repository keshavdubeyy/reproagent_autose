"""Layer 4 loop: a scripted model, a toy repository, real sandbox runs of the report code and tests."""
import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from autose.patch import check
from autose.patch.loop import patch_instance, select_targets
from autose.repo_index import build_index
from autose.trace import runner

FILES = {
    "pkg/__init__.py": "",
    "pkg/shape.py": dedent("""\
        class Shape:
            def __init__(self, w, h):
                self.w, self.h = w, h

            def area(self):
                return self.w + self.h


        def half(x):
            return x / 2
        """),
    "pkg/tests/__init__.py": "",
    "pkg/tests/test_shape.py": dedent("""\
        from pkg.shape import Shape, half


        def test_half():
            assert half(4) == 2


        def test_init():
            assert Shape(2, 3).w == 2
        """),
}
REPORT = {"Description": "Shape(2, 3).area() returns 5", "StepsToReproduce": ["Run `Shape(2, 3).area()`"],
          "ExpectedBehavior": "6", "ObservedBehavior": "5"}
AGENT = {"status": "ok", "problem_location": {"entities": ["pkg/shape.py:Shape.area"], "methods": [], "new_methods": []},
         "bug_report": {"RootCause": "1. Shape.area adds the sides instead of multiplying them"}}
TRACE = {"code": ["from pkg.shape import Shape", "Shape(2, 3).area()"]}

def _json(*edits):
    return json.dumps({"problem": "p", "fix": "f",
                       "edits": [{"function": f, "search": "", "code": c} for f, c in edits]})


ANSWERS = [
    # 0: wrong formula and a broken constructor: the observed 5 is gone, but a test regresses
    _json(("area", "def area(self):\n    return self.w - self.h\n"),
          ("__init__", "def __init__(self, w, h):\n    self.w, self.h = h, w\n")),
    # 1: refused: an existing function outside the located code
    _json(("(new function)", "def half(x):\n    return x // 2\n")),
    # 2: right, as cut-off JSON: the code block in the text is used
    'Multiply.\n```python\ndef area(self):\n    return self.w * self.h\n```\n{"problem": "cut of',
    "should not be asked",
]


class ScriptedLLM:
    def __init__(self, answers):
        self.answers, self.seen, self.formats = list(answers), [], []
        self.usage = {"calls": 0}

    def chat(self, messages, max_tokens=None, fmt=None, **_):
        self.seen.append(messages)
        self.formats.append(fmt)
        self.usage["calls"] += 1
        return {"content": self.answers.pop(0), "tool_calls": []}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AUTOSE_TRACE_PYTHON", sys.executable)
    monkeypatch.setitem(runner.REPOS, "toy/toy", {"env": "none", "preamble": [], "baseline": []})
    return tmp_path


def test_targets_cover_the_located_class(repo):
    index = build_index(repo, "deadbeef")
    targets, classes, members, new = select_targets(AGENT, index)
    assert [t["id"] for t in targets] == ["pkg/shape.py:Shape.area"]
    assert [c["id"] for c in classes] == ["pkg/shape.py:Shape"]
    assert {m["name"] for m in members["pkg/shape.py:Shape"]} == {"__init__", "area"}


def test_loop_retries_with_feedback_and_keeps_the_verified_patch(repo):
    index = build_index(repo, "deadbeef")
    plan = json.dumps({"steps": [{"function": "area", "change": "multiply the sides instead of adding them"}]})
    llm = ScriptedLLM([plan] + ANSWERS)
    res = patch_instance("toy/toy", REPORT, AGENT, index, repo, llm, TRACE, max_attempts=4, log=lambda m: None, plan=True)
    att = res["attempts"]
    assert len(att) == 3 and llm.usage["calls"] == 4            # a plan, then stopped once verified
    assert res["plan"] == ["area: multiply the sides instead of adding them"]
    assert "1. area: multiply the sides" in llm.seen[1][1]["content"]   # the plan reaches the edit prompt
    assert att[0]["check"]["regressions"] == ["pkg/tests/test_shape.py::test_init"]
    assert att[0]["check"]["observed_gone"] == 1 and att[0]["check"]["expected"] == 0
    assert "`half` is not one of the functions" in att[1]["errors"][0] and not att[1]["diff"]
    assert att[2]["check"]["expected"] == 1 and not att[2]["check"]["regressions"]
    assert res["best"] == 2 and res["verified"] and "+        return self.w * self.h" in res["diff"]
    # the model was told what its first patch did, then why the second was refused
    fb0, fb1 = llm.seen[2][-1]["content"], llm.seen[3][-1]["content"]
    assert "test_init" in fb0 and "The report expects: 6" in fb0
    assert "could not be applied" in fb1
    # every retry restarts from the task, not from a growing conversation
    assert all(len(m) in (2, 4) for m in llm.seen)
    # the answer format names only functions it may edit, plus the two ways to add code
    enum = llm.formats[1]["properties"]["edits"]["items"]["properties"]["function"]["enum"]
    assert enum == ["area", "__init__", "(new method)", "(new function)"]
    # nothing on disk changed
    assert (repo / "pkg/shape.py").read_text(encoding="utf-8") == FILES["pkg/shape.py"]


def test_compare_counts_fixed_errors_and_new_failures():
    before = {"statements": [{"i": 0, "ok": False, "error": "TypeError: bad", "value": None, "out": ""},
                             {"i": 1, "ok": True, "error": None, "value": "1", "out": ""}]}
    after = {"statements": [{"i": 0, "ok": True, "error": None, "value": "2", "out": ""},
                            {"i": 1, "ok": False, "error": "ValueError: x", "value": None, "out": ""}]}
    c = check.compare(before, after)
    assert c["fixed"] == 1 and c["broke"] == 1 and not check.good_enough(c)
    # a NameError raised inside the library (a typo there) is the bug; one in the report's own
    # example code is not
    lib = {"statements": [{"i": 0, "ok": False, "error": "NameError: name 'cotm' is not defined",
                           "frames": [["pkg/m.py", 3, "eval"]], "value": None, "out": ""},
                          {"i": 1, "ok": False, "error": "NameError: name 'theta' is not defined",
                           "frames": [], "value": None, "out": ""}]}
    ok = {"statements": [{"i": 0, "ok": True, "error": None, "value": "coth(x)", "out": ""},
                         {"i": 1, "ok": True, "error": None, "value": "1", "out": ""}]}
    assert check.compare(lib, ok)["fixed"] == 1


def test_patch_package_reads_no_private_data():
    banned = ("private", "external", "autose.evaluation", "FAIL_TO_PASS", "PASS_TO_PASS", "swebench")
    for p in Path("autose/patch").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert not [b for b in banned if b in text], p
