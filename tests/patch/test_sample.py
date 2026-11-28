"""Layer 4 sampling: candidates filtered by tests and chosen by vote; line hints from the example."""
import json
import sys
from dataclasses import replace
from textwrap import dedent

import pytest

from autose.patch.loop import patch_instance
from autose.patch.profiles import PROFILES
from autose.patch.sample import patch_key
from autose.repo_index import build_index
from autose.trace import runner

FILES = {
    "pkg/__init__.py": "",
    "pkg/shape.py": dedent("""\
        class Shape:
            def __init__(self, w, h):
                self.w, self.h = w, h

            def area(self):
                if self.w < 0:
                    return 0
                return self.w + self.h


        def half(x):
            return x / 2
        """),
    "pkg/tests/__init__.py": "",
    "pkg/tests/test_shape.py": dedent("""\
        from pkg.shape import Shape


        def test_init():
            assert Shape(2, 3).w == 2


        def test_negative():
            assert Shape(-1, 3).area() == 0
        """),
}
REPORT = {"Description": "Shape(2, 3).area() returns 5", "StepsToReproduce": ["Run `Shape(2, 3).area()`"],
          "ExpectedBehavior": "6", "ObservedBehavior": "5"}
AGENT = {"status": "ok", "problem_location": {"entities": ["pkg/shape.py:Shape.area"]},
         "bug_report": {"RootCause": "1. Shape.area adds the sides"}}
TRACE = {"code": ["from pkg.shape import Shape", "Shape(2, 3).area()", "Shape(-1, 3).area()"]}


def _edit(search, code, function="area"):
    return json.dumps({"problem": "p", "fix": "f", "edits": [{"function": function, "search": search, "code": code}]})


class Scripted:
    def __init__(self, replies):
        self.replies, self.seen, self.temps = list(replies), [], []
        self.usage = {"calls": 0}
        self.temperature = 0.0

    def chat(self, messages, max_tokens=None, fmt=None, **_):
        self.seen.append(messages)
        self.temps.append(self.temperature)
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


def test_vote_picks_the_patch_most_candidates_agree_on_among_those_keeping_the_tests(repo):
    replies = [
        _edit("return self.w + self.h", "return self.w * self.h"),                 # right
        _edit("return self.w + self.h", "return self.w * self.h  # multiply"),     # the same, formatted differently
        _edit("return 0", "return -1"),                                            # breaks test_negative
        "not json at all", "still not json",                                       # refused twice
        _edit("return self.w + self.h", "return self.h * self.w + 0"),             # different, also right
    ]
    llm = Scripted(replies)
    prof = replace(PROFILES["slm4"], candidates=4, alt_locations=0)
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=prof)
    att = res["attempts"]
    assert len(att) == 4 and att[1]["same_as"] == 0                       # one check for both formattings
    assert att[2]["check"]["regressions"] == ["pkg/tests/test_shape.py::test_negative"]
    assert not att[3]["diff"]                                             # refused, with one retry
    assert res["best"] == 0 and att[0]["votes"] == 2 and "+        return self.w * self.h" in res["diff"]
    assert llm.temps[0] == 0.0 and all(t == prof.retry_temperature for t in llm.temps[1:])
    assert all(len(m) == 2 for m in llm.seen if m is not llm.seen[4])     # each candidate starts fresh


def test_line_hints_name_the_lines_that_ran_only_for_the_failing_example(repo):
    llm = Scripted([_edit("return self.w + self.h", "return self.w * self.h")])
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=PROFILES["slm4"], max_attempts=1)
    prompt = llm.seen[0][1]["content"]
    # Shape(2, 3) fails (shows 5, expects 6) and reaches the last line; Shape(-1, 3) works and returns early
    assert "Lines of it that ran when the report's example failed" in prompt
    assert "8: return self.w + self.h" in prompt and "7: return 0" not in prompt
    assert res["verified"]


def test_size_tie_break_prefers_the_candidate_changing_more_code(repo):
    # both keep the tests and give the same result on the report's example (one behaviour group):
    # "smallest" takes the one-line change, "size" the fuller one
    replies = [_edit("return self.w + self.h", "return self.w * self.h"),
               _edit("if self.w < 0:\n    return 0\nreturn self.w + self.h",
                     "if self.w < 0 or self.h < 0:\n    return 0\nreturn self.w * self.h")]
    runs = {}
    for tb in ("smallest", "size"):
        prof = replace(PROFILES["slm4"], candidates=2, alt_locations=0, tie_break=tb)
        runs[tb] = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, Scripted(list(replies)),
                                  TRACE, log=lambda m: None, profile=prof)
    assert runs["smallest"]["best"] == 0 and runs["size"]["best"] == 1
    assert runs["size"]["attempts"][1]["size"] > runs["size"]["attempts"][0]["size"] > 0


def test_code_size_ignores_docstrings_comments_and_formatting():
    from autose.patch.sample import code_size
    old = 'def f(x):\n    """Doc."""\n    return x\n'
    assert code_size(lambda f: old, {"m.py": "def f(x):\n    return x  # same\n"}) == 0
    assert code_size(lambda f: old, {"m.py": 'def f(x):\n    """Doc."""\n    return x + 1\n'}) == 1


def test_causes_from_one_thinking_call_go_to_the_later_candidates(repo):
    causes = json.dumps({"causes": [{"function": "area", "line": "return self.w + self.h", "change": "multiply"},
                                    {"function": "area", "line": "if self.w < 0:", "change": "check h too"}],
                         "sites": [{"function": "area", "line": "return self.w + self.h"}]})
    llm = Scripted([causes] + [_edit("return self.w + self.h", "return self.w * self.h")] * 4)
    llm.think, llm.think_tokens = False, 0
    thinking = []
    chat = llm.chat
    llm.chat = lambda messages, **k: (thinking.append(llm.think), chat(messages, **k))[1]
    prof = replace(PROFILES["slm4"], candidates=4, alt_locations=0, hypotheses=2, plain_candidates=2)
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=prof)
    assert thinking == [True, False, False, False, False] and llm.think is False   # only the first call thinks
    prompts = [m[1]["content"] for m in llm.seen[1:]]
    assert "A possible root cause" not in prompts[0] and "A possible root cause" not in prompts[1]
    assert "area: `return self.w + self.h`: multiply" in prompts[2] and "check h too" in prompts[3]
    assert "The same faulty logic may also appear here" in prompts[2]
    assert res["hypotheses"]["causes"][1].endswith("check h too") and res["attempts"][2]["cause"]


def test_patch_key_ignores_formatting_and_comments():
    a = {"m.py": "def f(x):\n    return x*2\n"}
    b = {"m.py": "def f(x):\n\n    return x * 2  # double\n"}
    c = {"m.py": "def f(x):\n    return x * 3\n"}
    assert patch_key(a) == patch_key(b) != patch_key(c)
