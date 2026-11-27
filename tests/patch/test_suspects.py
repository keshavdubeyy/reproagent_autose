"""Layer 4 suspects for wrong output without an exception: same pattern (A), still-wrong lines (B),
run path (C) and value trail (D), on toy repositories with two-location bugs."""
import json
import sys
from dataclasses import replace
from textwrap import dedent

import pytest

from autose.patch import check
from autose.patch.loop import patch_instance
from autose.patch.profiles import PROFILES
from autose.repo_index import build_index
from autose.trace import runner


def make(tmp_path, monkeypatch, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AUTOSE_TRACE_PYTHON", sys.executable)
    monkeypatch.setitem(runner.REPOS, "toy/toy", {"env": "none", "preamble": [], "baseline": []})
    return tmp_path


class Scripted:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []
        self.usage = {"calls": 0}

    def chat(self, messages, max_tokens=None, fmt=None, tools=None, **_):
        self.seen.append([dict(m) for m in messages])
        self.usage["calls"] += 1
        return {"content": self.replies.pop(0), "tool_calls": []}


def _json(function, search, code):
    return json.dumps({"problem": "p", "fix": "f", "edits": [{"function": function, "search": search, "code": code}]})


# copied code: the same wrong line in two functions; Layer 3 found only the first
COPIES = {
    "pkg/__init__.py": "",
    "pkg/fmt.py": dedent('''\
        def fmt_title(x):
            return "%s!" % x


        def fmt_body(x):
            return "%s!" % x


        class Doc:
            def title(self):
                return fmt_title("T")

            def body(self):
                return fmt_body("B")
        '''),
}
COPIES_REPORT = {"Description": "Doc ends sentences with ! instead of .",
                 "StepsToReproduce": ["Run `Doc().title()`", "Run `Doc().body()`"],
                 "ExpectedBehavior": "'T.' and 'B.'", "ObservedBehavior": "'T!' and 'B!'"}
COPIES_AGENT = {"status": "ok", "bug_report": {"RootCause": "1. fmt_title appends !"},
                "problem_location": {"entities": ["pkg/fmt.py:fmt_title"]}}
COPIES_TRACE = {"code": ["from pkg.fmt import Doc", "Doc().title()", "Doc().body()"]}


def test_same_pattern_and_value_trail_find_the_copied_bug(tmp_path, monkeypatch):
    repo = make(tmp_path, monkeypatch, COPIES)
    both = json.dumps({"problem": "p", "fix": "f", "edits": [
        {"function": "fmt_title", "search": 'return "%s!" % x', "code": 'return "%s." % x'},
        {"function": "fmt_body", "search": 'return "%s!" % x', "code": 'return "%s." % x'}]})
    # slm4: every attempt starts from the original, so the second answer carries both edits
    llm = Scripted([_json("fmt_title", 'return "%s!" % x', 'return "%s." % x'), both])
    res = patch_instance("toy/toy", COPIES_REPORT, COPIES_AGENT, build_index(repo, "x"), repo, llm, COPIES_TRACE,
                         log=lambda m: None, profile=PROFILES["slm4"], max_attempts=3)
    a0 = res["attempts"][0]
    assert a0["check"]["expected"] == 1 and a0["check"]["wrong"] == [2]          # B: body is still wrong, no error
    assert a0["paged_in"][0] == "pkg/fmt.py:fmt_body"                            # A: the same line elsewhere
    fb = llm.seen[1][-1]["content"]
    assert "the same code you changed" in fb
    assert "fmt_body(x='B') returned 'B!'" in fb                                 # D: what it still returns
    assert "def fmt_body(x):" in llm.seen[1][1]["content"]                       # its code is shown
    assert res["verified"] and res["best"] == 1 and res["diff"].count('+    return "%s." % x') == 2


# two different wrong lines on two call paths; Layer 3 found only the first
PATHS = {
    "pkg/__init__.py": "",
    "pkg/calc.py": dedent('''\
        def unit(x):
            return x + 1


        def scale(x):
            return x * 3


        def first(x):
            return unit(x)


        def second(x):
            return scale(x)
        '''),
}
PATHS_REPORT = {"Description": "first(2) and second(2) are wrong",
                "StepsToReproduce": ["Run `first(2)`", "Run `second(2)`"],
                "ExpectedBehavior": "first gives 2, second gives 4", "ObservedBehavior": "first gives 3, second gives 6"}
PATHS_AGENT = {"status": "ok", "bug_report": {"RootCause": "1. unit adds one"},
               "problem_location": {"entities": ["pkg/calc.py:unit"]}}
PATHS_TRACE = {"code": ["from pkg.calc import first, second", "first(2)", "second(2)"]}


def test_run_path_finds_the_function_behind_the_still_wrong_line(tmp_path, monkeypatch):
    repo = make(tmp_path, monkeypatch, PATHS)
    llm = Scripted([_json("unit", "return x + 1", "return x"), _json("scale", "return x * 3", "return x * 2")])
    # slm8 in JSON mode builds on the partly-right patch, so the second answer adds only the new edit
    res = patch_instance("toy/toy", PATHS_REPORT, PATHS_AGENT, build_index(repo, "x"), repo, llm, PATHS_TRACE,
                         log=lambda m: None, profile=replace(PROFILES["slm8"], edit_mode="json"), max_attempts=3)
    a0 = res["attempts"][0]
    assert a0["check"]["wrong"] == [2]
    # C: what ran only for the wrong line; `first` ran for the line that is now right, so it is not blamed
    assert set(a0["paged_in"]) == {"pkg/calc.py:second", "pkg/calc.py:scale"}
    assert "runs for `second(2)`, which is still wrong" in llm.seen[1][-1]["content"]
    assert res["attempts"][1]["built_on"] == 0
    assert res["verified"] and "+    return x * 2" in res["diff"] and "+    return x\n" in res["diff"]


def test_probe_records_calls_and_return_values_per_line(tmp_path, monkeypatch):
    repo = make(tmp_path, monkeypatch, PATHS)
    p = check.probe_run("toy/toy", repo, PATHS_TRACE["code"], watch=[["pkg/calc.py", 5, "scale"]])
    names = lambda i: {n for _, _, n, _ in p["per"][str(i)]}
    assert names(1) == {"first", "unit"} and names(2) == {"second", "scale"}
    assert p["trail"]["2"] == [["pkg/calc.py", 5, "scale", "x=2", "6"]] and "1" not in p["trail"]
