"""Layer 4 tool-call mode and page faults, with scripted models on a toy repository."""
import json
import sys
from dataclasses import replace
from textwrap import dedent

import pytest

from autose.patch.loop import patch_instance
from autose.patch.profiles import PROFILES
from autose.repo_index import build_index
from autose.trace import runner

FILES = {
    "pkg/__init__.py": "",
    "pkg/shape.py": dedent("""\
        def _side(v):
            return v.size


        class Shape:
            def __init__(self, w, h):
                self.w, self.h = w, h

            def area(self):
                return _side(self.w) * _side(self.h)
        """),
}
REPORT = {"Description": "Shape(2, 3).area() raises AttributeError", "StepsToReproduce": ["Run `Shape(2, 3).area()`"],
          "ExpectedBehavior": "6", "ObservedBehavior": "AttributeError: 'int' object has no attribute 'size'"}
# Layer 3 located only the caller; the bug is in the helper it calls
AGENT = {"status": "ok", "bug_report": {"RootCause": "1. Shape.area fails"},
         "problem_location": {"entities": ["pkg/shape.py:Shape.area"]}}
TRACE = {"code": ["from pkg.shape import Shape", "Shape(2, 3).area()"]}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AUTOSE_TRACE_PYTHON", sys.executable)
    monkeypatch.setitem(runner.REPOS, "toy/toy", {"env": "none", "preamble": [], "baseline": []})
    return tmp_path


class Scripted:
    def __init__(self, replies):
        self.replies, self.seen, self.tools = list(replies), [], []
        self.usage = {"calls": 0}

    def chat(self, messages, max_tokens=None, fmt=None, tools=None, **_):
        self.seen.append([dict(m) for m in messages])
        self.tools.append(tools)
        self.usage["calls"] += 1
        r = self.replies.pop(0)
        return r if isinstance(r, dict) else {"content": r, "tool_calls": []}


def _json(function, search, code):
    return json.dumps({"problem": "p", "fix": "f", "edits": [{"function": function, "search": search, "code": code}]})


def test_page_fault_adds_the_helper_the_located_code_calls(repo):
    llm = Scripted([_json("area", "return _side(self.w) * _side(self.h)", "return _side(self.w) * _side(self.h) * 1"),
                    _json("_side", "return v.size", "return v")])
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=PROFILES["slm4"], max_attempts=3)
    a0, a1 = res["attempts"]
    assert a0["check"]["same_error"] == 1 and a0["paged_in"] == ["pkg/shape.py:_side"]
    assert res["paged_in"] == ["pkg/shape.py:_side"] and res["verified"] and res["best"] == 1
    assert "def _side(v):" in llm.seen[1][1]["content"]                 # its code is now in the prompt
    assert "could not see before" in llm.seen[1][-1]["content"] and "failing traceback runs through it" in llm.seen[1][-1]["content"]
    assert "-    return v.size" in res["diff"]


def _call(name, **args):
    return {"content": "", "tool_calls": [{"name": name, "arguments": args}]}


def test_tool_mode_reads_edits_checks_and_stops_when_fixed(repo):
    prof = replace(PROFILES["slm8"], num_gpu=None)
    llm = Scripted([
        _call("view_code", function="area"),
        _call("replace", function="area", old="return self.w * self.h", new="return 1"),   # not in the code: refused
        "I think the bug is in area.",                                                   # no tool call: nudged
        _call("replace", function="area", old="return _side(self.w) * _side(self.h)", new="return self.w * self.h"),
        _call("run_check"),
        "should not be asked",
    ])
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=prof)
    assert res["mode"] == "tools" and res["verified"] and len(res["attempts"]) == 1
    assert "+        return self.w * self.h" in res["diff"] and llm.usage["calls"] == 5
    tool_out = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert tool_out[0].startswith("pkg/shape.py:Shape.area (lines 9-10)")          # view_code
    assert tool_out[1].startswith("Error:") and "Nothing was changed" in tool_out[1]
    assert tool_out[2].startswith("Done: replaced 1 lines")
    assert any(m["role"] == "user" and m["content"].startswith("Use a tool") for m in llm.seen[-1])
    enum = llm.tools[0][1]["function"]["parameters"]["properties"]["function"]["enum"]
    assert "area" in enum and "(new method)" in enum
    assert (repo / "pkg/shape.py").read_text(encoding="utf-8") == FILES["pkg/shape.py"]   # disk untouched


def test_tool_mode_checks_unchecked_edits_at_the_end(repo):
    llm = Scripted([_call("replace", function="area", old="return _side(self.w) * _side(self.h)",
                          new="return self.w * self.h")] + ["no tools"] * 3)
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=PROFILES["slm8"])
    assert len(res["attempts"]) == 1 and res["verified"]                   # checked although it never asked
