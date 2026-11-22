"""End-to-end tests of the agent loop with a scripted model: real sandbox, real pytest, real blackboard.

The model's answers are fixed, so these check the orchestration (feedback, loop controller, review,
freezing, multi-location repair), not the model.
"""
import json
import shutil
from pathlib import Path

import pytest

from autose.agent.loop import AgentConfig, ReproAgent, _locations, focus_test
from autose.agent.models import BugReport, BugSpec, Patch, ReproTest, Verdict

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "benchmarks" / "multiloc" / "work" / "ml_boundaries" / "buggy"

REPORT = BugReport("shop-1", "Discount and free shipping missing exactly at the threshold",
                   "bulk_discount(1000) returns 0.00 but the docs say orders of Rs 1,000 or more get 10% (expected 100.00). "
                   "shipping_fee(500) returns 50.00, expected 0.00 because orders of Rs 500 or more ship free.")
SPEC = BugSpec("threshold uses > instead of >=", "bulk_discount(1000) == 100 and shipping_fee(500) == 0",
               "bulk_discount(1000) == 0", symbols=["bulk_discount", "shipping_fee"], failure_kind="wrong_value")

T_PASSES = "from shopcart.pricing import bulk_discount\n\ndef test_ok():\n    assert bulk_discount(2000) == 200\n"
T_BROKEN = "from shopcart.pricing import no_such_function\n\ndef test_x():\n    assert no_such_function(1) == 1\n"
T_ONE = "from shopcart.pricing import bulk_discount\n\ndef test_discount_at_1000():\n    assert bulk_discount(1000) == 100\n"
T_BOTH = ("from shopcart.pricing import bulk_discount, shipping_fee\n\n"
          "def test_discount_at_1000():\n    assert bulk_discount(1000) == 100\n\n"
          "def test_free_shipping_at_500():\n    assert shipping_fee(500) == 0\n")

FIX_DISCOUNT = '''def bulk_discount(subtotal) -> Decimal:
    """10% discount for orders of Rs 1,000 or more."""
    subtotal = Decimal(str(subtotal))
    if subtotal >= DISCOUNT_THRESHOLD:
        return money(subtotal * DISCOUNT_RATE)
    return money(0)
'''
WRONG_DISCOUNT = FIX_DISCOUNT.replace(">= DISCOUNT_THRESHOLD", "> DISCOUNT_THRESHOLD + 1")
# a typo on a branch no test runs: tests pass, only the review can catch it (like flask-4045)
TYPO_DISCOUNT = FIX_DISCOUNT.replace("    if subtotal >=", "    if subtotal < 0:\n        raise ValueErorr('negative')\n    if subtotal >=")
FIX_SHIPPING = '''def shipping_fee(amount) -> Decimal:
    """Flat Rs 50 shipping, free for orders of Rs 500 or more."""
    if Decimal(str(amount)) >= FREE_SHIPPING_THRESHOLD:
        return money(0)
    return money(SHIPPING_FEE)
'''


class ScriptedLLM:
    """Stands in for the model; only the message judge would call it (it says 'same')."""
    def __init__(self):
        self.calls = 0

    def chat(self, system, user, json_mode=False, temperature=None):
        self.calls += 1
        return json.dumps({"same": True, "decision": "APPROVE", "problem": ""})


@pytest.fixture
def repo(tmp_path):
    if not SRC.exists():
        pytest.skip("multiloc benchmark not generated")
    dst = tmp_path / "buggy"
    shutil.copytree(SRC, dst)
    return dst


def make_agent(tmp_path, tests, patches, sweep=None, **cfg):
    cfg = {"use_judge": False, "timeout_s": 60, "board_dir": str(tmp_path / "board"), **cfg}
    agent = ReproAgent(ScriptedLLM(), AgentConfig(**cfg), log=lambda _m: None)
    agent.specifier.specify = lambda report: SPEC
    tests, patches = list(tests), list(patches)
    agent.generator.generate = lambda *a, **k: ReproTest(a[-1] if a else 1, tests.pop(0) if len(tests) > 1 else tests[0])
    agent.patcher.generate = lambda *a, **k: Patch(a[6], patches.pop(0) if len(patches) > 1 else patches[0])
    agent.patcher.sweep = lambda diff, sibs, attempt: sweep
    return agent


def board(tmp_path, bug="shop-1"):
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "board" / bug).glob("*.json"))]


def test_reproduce_then_fix_freezes_only_the_accepted_patch(tmp_path, repo):
    agent = make_agent(tmp_path, [T_BROKEN, T_PASSES, T_ONE], [WRONG_DISCOUNT, FIX_DISCOUNT],
                       fix=True, review="rules", loop_control=True)
    out = agent.run(REPORT, repo)
    assert out.verdict == Verdict.REPRODUCED and len(out.attempts) == 3
    assert [a.analysis.category for a in out.attempts][:2] != ["reproduced", "reproduced"]
    assert out.fix.status == "fixed" and ">= DISCOUNT_THRESHOLD" in out.fix.final_diff
    assert [a.check.category for a in out.fix.attempts] == ["repro_still_fails", "plausible"]
    assert out.fix.locations == ["shopcart/pricing.py:bulk_discount"]
    kinds = [x["kind"] for x in board(tmp_path)]
    assert kinds == ["BUG_SPEC", "CHANGE_PLAN", "TEST_REPORT", "CHANGE_PLAN", "TEST_REPORT", "REVIEW_REPORT",
                     "FROZEN_PATCH"]
    frozen = board(tmp_path)[-1]
    assert frozen["patch_sha"] == board(tmp_path)[3]["patch_sha"]
    assert (tmp_path / "board" / "shop-1" / "events.jsonl").read_text().count("\n") == 7
    assert out.retrieved and out.retrieval == "explorer"
    assert len(out.control_log) == 3          # 2 failed reproductions + 1 failed patch, no repeats


def test_review_rejects_typo_and_repair_loop_recovers(tmp_path, repo):
    agent = make_agent(tmp_path, [T_ONE], [TYPO_DISCOUNT, FIX_DISCOUNT], fix=True, review="rules")
    out = agent.run(REPORT, repo)
    assert [a.check.category for a in out.fix.attempts] == ["review_rejected", "plausible"]
    reviews = [x for x in board(tmp_path) if x["kind"] == "REVIEW_REPORT"]
    assert [r["decision"] for r in reviews] == ["REJECT", "APPROVE"]
    assert "ValueErorr" in out.fix.attempts[0].check.feedback
    assert "ValueErorr" not in out.fix.final_diff


def test_review_rejection_on_last_attempt_means_not_fixed(tmp_path, repo):
    agent = make_agent(tmp_path, [T_ONE], [TYPO_DISCOUNT], fix=True, fix_mode="fix_single", review="rules")
    out = agent.run(REPORT, repo)
    assert out.fix.status == "not_fixed" and out.fix.final_diff is None
    assert not [x for x in board(tmp_path) if x["kind"] == "FROZEN_PATCH"]


def test_loop_controller_stops_repeated_reproduction_failure(tmp_path, repo):
    agent = make_agent(tmp_path, [T_PASSES], [], loop_control=True, budget=6)
    out = agent.run(REPORT, repo)
    assert out.verdict == Verdict.NOT_REPRODUCED
    assert len(out.attempts) == 3                         # stopped at the third identical failure
    assert [e["action"] for e in out.control_log] == ["continue", "switch", "stop"]
    assert "clearly different approach" in out.attempts[1].analysis.feedback


def test_loop_controller_switches_candidate_then_stops_repair(tmp_path, repo):
    agent = make_agent(tmp_path, [T_ONE], [WRONG_DISCOUNT], fix=True, loop_control=True)
    out = agent.run(REPORT, repo)
    assert out.fix.status == "not_fixed" and len(out.fix.attempts) == 3
    assert [e["action"] for e in out.control_log if e["stage"] == "repair"] == ["continue", "switch", "stop"]


def test_without_loop_controller_budget_is_used_up(tmp_path, repo):
    agent = make_agent(tmp_path, [T_PASSES], [], budget=4)
    out = agent.run(REPORT, repo)
    assert len(out.attempts) == 4 and out.control_log == []


def test_not_actionable_report_needs_info(tmp_path, repo):
    agent = make_agent(tmp_path, [T_ONE], [])
    agent.specifier.specify = lambda report: BugSpec("", "", "", is_actionable=False)
    out = agent.run(REPORT, repo)
    assert out.verdict == Verdict.NEEDS_INFO and out.attempts == []


def test_multi_location_repair_keeps_partial_fix_and_builds_on_it(tmp_path, repo):
    agent = make_agent(tmp_path, [T_BOTH], [FIX_DISCOUNT, FIX_SHIPPING], fix=True, fix_mode="fix_multi",
                       review="rules", loop_control=True)
    out = agent.run(REPORT, repo)
    assert out.verdict == Verdict.REPRODUCED
    assert [a.check.category for a in out.fix.attempts] == ["partial", "plausible"]
    assert out.fix.status == "fixed"
    assert sorted(out.fix.locations) == ["shopcart/pricing.py:bulk_discount", "shopcart/pricing.py:shipping_fee"]
    assert "FROZEN_PATCH" in [x["kind"] for x in board(tmp_path)]


def test_multi_location_sweep_patch_is_validated(tmp_path, repo):
    sweep = Patch(0, FIX_SHIPPING, kind="sweep")
    agent = make_agent(tmp_path, [T_BOTH], [FIX_DISCOUNT + "\n\n" + FIX_SHIPPING], sweep=sweep, fix=True,
                       fix_mode="fix_multi")
    out = agent.run(REPORT, repo)
    assert out.fix.status == "fixed" and out.fix.attempts[0].check.accepted


def test_fail_to_pass_oracle_on_fixed_version(tmp_path, repo):
    fixed = tmp_path / "fixed"
    shutil.copytree(repo, fixed)
    p = fixed / "shopcart" / "pricing.py"
    p.write_text(p.read_text().replace("subtotal > DISCOUNT_THRESHOLD", "subtotal >= DISCOUNT_THRESHOLD"))
    out = make_agent(tmp_path, [T_ONE], []).run(REPORT, repo, fixed_repo=fixed)
    assert out.fail_to_pass is True
    out = make_agent(tmp_path, [T_PASSES], [], budget=1).run(REPORT, repo, fixed_repo=fixed)
    assert out.fail_to_pass is False                       # no accepted test -> not a valid reproduction


def test_fix_without_reproduction_uses_report_only(tmp_path, repo):
    agent = make_agent(tmp_path, [T_ONE], [FIX_DISCOUNT])
    fx = agent.fix(REPORT, SPEC, repo, None, mode="fix_no_repro")
    assert fx.mode == "fix_no_repro" and len(fx.attempts) == 1


def test_focus_test_and_locations(repo):
    assert "test_free_shipping_at_500" in focus_test(T_BOTH, ["test_free_shipping_at_500"])
    assert "test_discount_at_1000" not in focus_test(T_BOTH, ["test_free_shipping_at_500"])
    assert focus_test("def broken(:\n", ["x"]) == "def broken(:\n"
    assert focus_test(T_BOTH, []) == T_BOTH
    src = (repo / "shopcart" / "pricing.py").read_text()
    assert _locations(repo, {"shopcart/pricing.py": src.replace("> DISCOUNT", ">= DISCOUNT")}) == [
        "shopcart/pricing.py:bulk_discount"]


def test_search_results_reach_the_fault_localiser(repo):
    from autose.agent.repair import FaultLocalizer
    loc = FaultLocalizer(repo)
    src = (repo / "shopcart" / "pricing.py").read_text().splitlines()
    line = next(i for i, l in enumerate(src, 1) if l.startswith("def shipping_fee"))
    plain = loc.localize(REPORT, SPEC, T_ONE, None)
    handed = loc.localize(REPORT, SPEC, T_ONE, None, retrieved=[("shopcart/pricing.py", line, "shipping_fee")])
    top = {c.name: c for c in handed}
    assert "ranked #1 by the search" in top["shipping_fee"].why or top["shipping_fee"].score > \
        next((c.score for c in plain if c.name == "shipping_fee"), 0)
    assert handed[0].name == "bulk_discount"           # the test's asserted call still comes first


def test_agent_hands_ranked_search_results_only_for_knowledge_package_configs(tmp_path, repo):
    from autose.agent.models import CodeContext
    agent = make_agent(tmp_path, [T_ONE], [])
    agent._ctx = CodeContext(ranked=[("shopcart/pricing.py", 20, "shipping_fee")])
    assert agent._retrieved() == []                     # config A (keyword search) keeps the mid-term localiser
    agent.cfg.retrieval = "graph"
    assert agent._retrieved() == [("shopcart/pricing.py", 20, "shipping_fee")]
