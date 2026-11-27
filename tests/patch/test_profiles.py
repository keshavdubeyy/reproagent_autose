"""Layer 4 profiles: the strategy follows the model size; slm8 builds on a partly-right patch."""
import json
import sys
from dataclasses import replace
from textwrap import dedent

import pytest

from autose.patch.loop import patch_instance
from autose.patch.profiles import PROFILES, for_model, model_size_b
from autose.repo_index import build_index
from autose.trace import runner


def test_profile_follows_model_size():
    assert model_size_b("qwen3:4b") == 4 and model_size_b("qwen2.5-coder:7b-instruct-q4_K_M") == 7
    assert model_size_b("ollama/llama3.1:70b") == 70 and model_size_b("mystery") is None
    picks = {m: for_model(m).name for m in ["qwen3:4b", "qwen2.5-coder:3b", "qwen3:8b", "qwen3:14b", "llama3.1:70b",
                                            "mystery"]}
    assert picks == {"qwen3:4b": "slm4", "qwen2.5-coder:3b": "slm4", "qwen3:8b": "slm8", "qwen3:14b": "slm14",
                     "llama3.1:70b": "slm14", "mystery": "slm4"}
    assert for_model("qwen3:4b", "slm14").name == "slm14"              # forced
    assert for_model("qwen3:4b", plan=True).plan and not PROFILES["slm4"].plan   # one field overridden
    # bigger models get more context and build on their partial fixes
    s4, s8, s14 = PROFILES["slm4"], PROFILES["slm8"], PROFILES["slm14"]
    assert s4.num_ctx < s8.num_ctx < s14.num_ctx and s4.code_chars < s8.code_chars < s14.code_chars
    assert not s4.build_on_best and s8.build_on_best and s14.build_on_best and s14.plan
    assert s4.edit_mode == "json" and s8.edit_mode == "tools" and s14.edit_mode == "tools"   # tools: 8B and up


FILES = {
    "pkg/__init__.py": "",
    "pkg/shape.py": dedent("""\
        class Shape:
            def __init__(self, w, h):
                self.w, self.h = w, h

            def area(self):
                return self.w + self.h

            def ratio(self):
                return self.h / self.w
        """),
}
REPORT = {"Description": "area is wrong and ratio crashes for a flat shape",
          "StepsToReproduce": ["Run `Shape(2, 3).area()`", "Run `Shape(0, 3).ratio()`"],
          "ExpectedBehavior": "area is 6 and ratio is 0", "ObservedBehavior": "area is 5 and ratio raises ZeroDivisionError"}
AGENT = {"status": "ok", "bug_report": {"RootCause": "1. area adds; 2. ratio divides by a zero width"},
         "problem_location": {"entities": ["pkg/shape.py:Shape.area", "pkg/shape.py:Shape.ratio"]}}
TRACE = {"code": ["from pkg.shape import Shape", "Shape(2, 3).area()", "Shape(0, 3).ratio()"]}


def _edit(function, search, code):
    return json.dumps({"problem": "p", "fix": "f", "edits": [{"function": function, "search": search, "code": code}]})


ANSWERS = [_edit("area", "return self.w + self.h", "return self.w * self.h"),          # half the fix
           _edit("ratio", "return self.h / self.w", "return self.h / self.w if self.w else 0")]   # the other half


class ScriptedLLM:
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []
        self.usage = {"calls": 0}

    def chat(self, messages, max_tokens=None, fmt=None, **_):
        self.seen.append(messages)
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


def test_slm8_keeps_a_partly_right_patch_and_builds_on_it(repo):
    llm = ScriptedLLM(ANSWERS)
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, llm, TRACE, log=lambda m: None,
                         profile=replace(PROFILES["slm8"], edit_mode="json"), max_attempts=2)
    a0, a1 = res["attempts"]
    assert a0["check"]["expected"] == 1 and a0["check"]["same_error"] == 1      # area right, ratio still crashes
    assert a1["built_on"] == 0 and res["verified"] and res["best"] == 1
    assert "self.w * self.h" in res["diff"] and "if self.w else 0" in res["diff"]   # both halves in one patch
    second_task = llm.seen[1][1]["content"]
    assert "return self.w * self.h" in second_task and "already contains your earlier" in second_task


def test_slm4_starts_every_attempt_from_the_original(repo):
    res = patch_instance("toy/toy", REPORT, AGENT, build_index(repo, "x"), repo, ScriptedLLM(ANSWERS), TRACE,
                         log=lambda m: None, profile=PROFILES["slm4"], max_attempts=2)
    a1 = res["attempts"][1]
    assert a1["built_on"] is None and "self.w * self.h" not in a1["diff"]      # the area fix is not carried over
    assert not res["verified"]
