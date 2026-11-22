"""Blackboard: schema checks and the patch-hash rules (autose/agent/blackboard.py)."""
import json

import pytest

from autose.agent.blackboard import Blackboard, BlackboardError, patch_sha
from autose.agent.loop import AgentConfig, ReproAgent
from autose.agent.models import FaultCandidate, Patch, PatchCheck

DIFF = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def plan(board, diff=DIFF, attempt=1):
    return board.post("CHANGE_PLAN", "code_modification", {
        "attempt": attempt, "patch_kind": "patch", "candidates": [], "changed": ["m.py:x"], "files": ["m.py"],
        "patch_sha": patch_sha(diff), "diff": diff, "applies": True})


def report(board, accepted, diff=DIFF):
    return board.post("TEST_REPORT", "testing", {
        "attempt": 1, "patch_sha": patch_sha(diff), "category": "plausible" if accepted else "regression",
        "accepted": accepted, "repro_status": "passed", "regressions": [], "feedback": ""})


def test_schema_is_checked():
    b = Blackboard(None, "bug")
    with pytest.raises(BlackboardError, match="missing field"):
        b.post("BUG_SPEC", "x", {"summary": ""})
    with pytest.raises(BlackboardError, match="expected bool"):
        b.post("CHANGE_PLAN", "x", {"attempt": 1, "patch_kind": "patch", "candidates": [], "changed": [], "files": [],
                                     "patch_sha": "a", "diff": "", "applies": "yes"})


def test_test_report_needs_a_published_patch():
    b = Blackboard(None, "bug")
    with pytest.raises(BlackboardError, match="no CHANGE_PLAN"):
        report(b, True)


def test_freeze_only_the_exact_patch_that_passed():
    b = Blackboard(None, "bug")
    plan(b)
    report(b, False)
    with pytest.raises(BlackboardError, match="no accepted TEST_REPORT"):
        b.freeze(DIFF)
    other = DIFF.replace("x = 2", "x = 3")           # a different patch passed: still not this one
    plan(b, other, 2)
    report(b, True, other)
    with pytest.raises(BlackboardError):
        b.freeze(DIFF)
    seq = b.freeze(other)
    assert b.items[seq - 1]["patch_sha"] == patch_sha(other)


def test_review_rejection_blocks_freezing():
    b = Blackboard(None, "bug")
    plan(b)
    report(b, True)
    b.post("REVIEW_REPORT", "review", {"patch_sha": patch_sha(DIFF), "decision": "REJECT",
                                       "reasons": ["changes the wrong function"], "by": "slm"})
    with pytest.raises(BlackboardError, match="rejected"):
        b.freeze(DIFF)


def test_files_and_event_log(tmp_path):
    b = Blackboard(tmp_path, "django__django-1")
    plan(b)
    report(b, True)
    b.freeze(DIFF)
    d = tmp_path / "django__django-1"
    assert sorted(p.name for p in d.glob("*.json")) == ["01_CHANGE_PLAN.json", "02_TEST_REPORT.json",
                                                        "03_FROZEN_PATCH.json"]
    events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["CHANGE_PLAN", "TEST_REPORT", "FROZEN_PATCH"]


def test_agent_publishes_every_patch_attempt():
    class NoLLM:
        calls = 0
    agent = ReproAgent(NoLLM(), AgentConfig(), log=lambda _m: None)
    cand = FaultCandidate("m.py", "m", "x", 1, 1, "x = 1", 5.0, "named in the report")
    hist = []
    p = Patch(1, "x = 2", diff=DIFF, files={"m.py": "x = 2\n"}, changed=["m.py:x"])
    agent._record(hist, p, PatchCheck("plausible", True, "", repro_status="passed"), [cand])
    assert [i["kind"] for i in agent.board.items] == ["CHANGE_PLAN", "TEST_REPORT"]
    assert agent.board.items[1]["patch_sha"] == patch_sha(DIFF) and len(hist) == 1
    agent.board.freeze(DIFF)
