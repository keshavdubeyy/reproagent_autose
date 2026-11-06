"""Fail-to-pass 'by meaning': error-message wording differences (no real SLM; a fake judge)."""
from pathlib import Path

import pytest

from reproagent.models import BugReport, ReproTest
from reproagent.verification import (FailToPassChecker, TestExecutor, message_is_the_bug,
                                     message_mismatches)

ROOT = Path(__file__).resolve().parents[1]
FIXED = ROOT / "benchmarks" / "multiloc" / "work" / "ml_quantity" / "fixed"

REPORT = BugReport("ml_quantity", "Cart accepts invalid quantities",
                   "cart.set_quantity(sku, -2) sets its quantity to -2. Expected: ValueError.")
MESSAGE_REPORT = BugReport("msg", "Misleading error message for negative quantities",
                           "set_quantity(sku, -2) raises an error that says 'invalid'. "
                           "It should say 'quantity must be positive'.")


def _test(match: str, body: str = "") -> ReproTest:
    return ReproTest(1, "import pytest\nfrom shopcart import Cart, Product\n\n"
                        "def test_negative():\n    cart = Cart()\n    cart.add(Product('P1', 'Pen', 20), 3)\n"
                        f"    with pytest.raises(ValueError, match={match!r}):\n"
                        "        cart.set_quantity('P1', -2)\n" + body)


class FakeJudge:
    def __init__(self, same: bool):
        self.same, self.calls = same, 0

    def same_meaning(self, expected, actual, context=""):
        self.calls += 1
        return self.same, "fake"


def _need():
    if not FIXED.exists():
        pytest.skip("run benchmarks/multiloc/build.py first")
    return FailToPassChecker(TestExecutor(timeout_s=60))


def test_parse_wording_mismatch_from_pytest_output():
    out = ("E   AssertionError: Regex pattern did not match.\n"
           "E     Expected regex: 'quantity must be positive'\n"
           "E     Actual message: 'quantity cannot be negative'\n")
    assert message_mismatches(out) == [("quantity must be positive", "quantity cannot be negative")]


def test_report_rule_detects_when_the_message_is_the_bug():
    assert not message_is_the_bug(REPORT, "quantity must be positive")
    assert message_is_the_bug(MESSAGE_REPORT, "quantity must be positive")


def test_same_meaning_wording_counts_as_valid():
    chk, judge = _need(), FakeJudge(True)
    strict, meaning, _, checks = chk.check_with_meaning(FIXED, _test("quantity must be positive"),
                                                        "t1", REPORT, judge)
    assert strict is False and meaning is True and judge.calls == 1
    assert checks[0]["actual"] == "quantity cannot be negative" and checks[0]["by"] == "slm"


def test_different_meaning_stays_invalid():
    strict, meaning, _, checks = _need().check_with_meaning(
        FIXED, _test("quantity must be positive"), "t2", REPORT, FakeJudge(False))
    assert (strict, meaning) == (False, False) and checks[0]["same"] is False


def test_message_bug_requires_exact_wording_and_skips_the_judge():
    judge = FakeJudge(True)
    strict, meaning, _, checks = _need().check_with_meaning(
        FIXED, _test("quantity must be positive"), "t3", MESSAGE_REPORT, judge)
    assert meaning is False and checks[0]["by"] == "report-rule" and judge.calls == 0


def test_contained_wording_needs_no_judge():
    judge = FakeJudge(False)
    strict, meaning, _, checks = _need().check_with_meaning(FIXED, _test("cannot be negative"), "t4",
                                                            REPORT, judge)
    assert strict is True and meaning is True and judge.calls == 0     # regex search already matches


def test_real_failure_is_not_rescued_by_wording_rule():
    judge = FakeJudge(True)
    t = _test("quantity must be positive", "\ndef test_other():\n    assert 1 == 2\n")
    strict, meaning, _, checks = _need().check_with_meaning(FIXED, t, "t5", REPORT, judge)
    assert (strict, meaning, checks) == (False, False, []) and judge.calls == 0
