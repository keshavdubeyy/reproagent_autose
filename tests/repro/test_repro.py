"""Reproduction stage: model-written tests kept only when they fail on the buggy code, and Layer 4
choosing the candidate that passes them over a more popular symptom-only patch."""
import json
import sys
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest

from autose.patch.loop import patch_instance
from autose.patch.profiles import PROFILES
from autose.repo_index import build_index
from autose.repro import reproduce
from autose.trace import runner

FILES = {
    "pkg/__init__.py": "",
    "pkg/shape.py": dedent("""\
        class Shape:
            def __init__(self, w, h):
                self.w, self.h = w, h

            def area(self):
                return self.w + self.h
        """),
}
REPORT = {"Description": "Shape(2, 3).area() returns 5", "StepsToReproduce": ["Run `Shape(2, 3).area()`"],
          "ExpectedBehavior": "6", "ObservedBehavior": "5"}
AGENT = {"status": "ok", "problem_location": {"entities": ["pkg/shape.py:Shape.area"]},
         "bug_report": {"RootCause": "1. Shape.area adds the sides"}}
TRACE = {"code": ["from pkg.shape import Shape", "Shape(2, 3).area()"]}

TESTS = {"tests": [
    {"name": "report_case", "kind": "report", "code": "from pkg.shape import Shape\nassert Shape(2, 3).area() == 6"},
    {"name": "passes_already", "kind": "report", "code": "from pkg.shape import Shape\nassert Shape(2, 3).w == 2"},
    {"name": "broken", "kind": "variant", "code": "assert Shap(2, 3).area() == 6"},
    {"name": "other_sides", "kind": "variant", "code": "from pkg.shape import Shape\nassert Shape(4, 5).area() == 20"},
]}


class Scripted:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []
        self.usage = {"calls": 0}
        self.temperature = 0.0

    def chat(self, messages, max_tokens=None, fmt=None, **_):
        self.seen.append(messages)
        self.usage["calls"] += 1
        return {"content": self.replies.pop(0), "tool_calls": []}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AUTOSE_TRACE_PYTHON", sys.executable)
    monkeypatch.setitem(runner.REPOS, "toy/toy", {"env": "none", "preamble": [], "baseline": []})
    return tmp_path


def test_gate_keeps_only_tests_that_fail_on_the_buggy_code(repo):
    res = reproduce("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, Scripted([json.dumps(TESTS)]), TRACE,
                    log=lambda m: None)
    assert [t["name"] for t in res["kept"]] == ["report_case", "other_sides"]
    why = {t["name"]: t["why"] for t in res["rejected"]}
    assert "passes on the buggy code" in why["passes_already"] and "own code" in why["broken"]
    assert res["kept"][0]["before"].startswith("AssertionError") and res["rounds"] == 1


def test_a_second_round_when_nothing_reproduces(repo):
    useless = json.dumps({"tests": [TESTS["tests"][1]]})
    llm = Scripted([useless, json.dumps({"tests": [TESTS["tests"][0]]})])
    res = reproduce("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None)
    assert res["rounds"] == 2 and [t["name"] for t in res["kept"]] == ["report_case"]
    assert "passes on the buggy code" in llm.seen[1][-1]["content"]          # told why


def _edit(code):
    return json.dumps({"problem": "p", "fix": "f", "edits": [
        {"function": "area", "search": "return self.w + self.h", "code": code}]})


def test_reproduction_tests_beat_a_popular_symptom_only_patch(repo):
    repro = reproduce("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, Scripted([json.dumps(TESTS)]), TRACE,
                      log=lambda m: None)
    special = "return 6 if (self.w, self.h) == (2, 3) else self.w + self.h"       # fixes the example only
    answers = [_edit(special), _edit(special + "  # same"), _edit("return self.w * self.h")]
    prof = replace(PROFILES["slm4"], candidates=3, alt_locations=0)
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, Scripted(answers), TRACE,
                         log=lambda m: None, profile=prof, repro=repro)
    att = res["attempts"]
    assert att[0]["votes"] == 2 and att[2]["votes"] == 1                    # the symptom patch is more popular
    assert res["best"] == 2 and "+        return self.w * self.h" in res["diff"]   # but the real fix passes more tests
    # without the reproduction tests the popular patch would win
    res2 = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, Scripted(answers), TRACE,
                          log=lambda m: None, profile=replace(prof, repro=False), repro=repro)
    assert res2["best"] == 0


def test_repro_package_reads_no_private_data():
    banned = ("private", "external", "autose.evaluation", "FAIL_TO_PASS", "PASS_TO_PASS", "swebench")
    for p in Path("autose/repro").glob("*.py"):
        assert not [b for b in banned if b in p.read_text(encoding="utf-8")], p
