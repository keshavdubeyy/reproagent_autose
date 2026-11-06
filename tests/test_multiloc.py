"""Unit tests of the multi-location repair tools (no SLM)."""
from pathlib import Path

import pytest

from reproagent.models import Patch, ReproTest
from reproagent.resolution import (FaultLocalizer, PatchApplier, changed_definitions, cumulative_diff,
                                   find_siblings)
from reproagent.verification import PatchValidator, TestExecutor, parse_pytest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "benchmarks" / "multiloc" / "work"

REPRO_BOUNDS = """from shopcart.pricing import bulk_discount, shipping_fee

def test_discount_at_1000():
    assert bulk_discount(1000) == 100

def test_free_shipping_at_500():
    assert shipping_fee(500) == 0
"""

FIX_DISCOUNT = """def bulk_discount(subtotal) -> Decimal:
    \"\"\"10% discount for orders of Rs 1,000 or more.\"\"\"
    subtotal = Decimal(str(subtotal))
    if subtotal >= DISCOUNT_THRESHOLD:
        return money(subtotal * DISCOUNT_RATE)
    return money(0)
"""

FIX_SHIPPING = """def shipping_fee(amount) -> Decimal:
    \"\"\"Flat Rs 50 shipping, free for orders of Rs 500 or more.\"\"\"
    if Decimal(str(amount)) >= FREE_SHIPPING_THRESHOLD:
        return money(0)
    return money(SHIPPING_FEE)
"""


def _need(bug):
    if not (WORK / bug / "buggy").exists():
        pytest.skip("run benchmarks/multiloc/build.py first")
    return WORK / bug / "buggy"


def test_parse_pytest_counts_failing_functions():
    out = ("FF.\n=== FAILURES ===\nE   assert 0 == 100\n=== short test summary info ===\n"
           "FAILED test_reproagent_repro.py::test_a - assert 0 == 100\n"
           "FAILED test_reproagent_repro.py::test_b - assert 50 == 0\n2 failed, 1 passed in 0.1s")
    r = parse_pytest(out, 1, 0.1)
    assert r.n_failed == 2 and r.n_passed == 1 and r.failing == ["test_a", "test_b"]


def test_changed_definitions_finds_every_edited_function():
    old = "def a():\n    return 1\n\ndef b():\n    return 2\n\nclass C:\n    def m(self):\n        return 3\n"
    new = old.replace("return 1", "return 10").replace("return 3", "return 30")
    assert changed_definitions(old, new) == ["a", "C.m"]


def test_partial_fix_is_kept_and_full_fix_accepted():
    repo = _need("ml_boundaries")
    loc = FaultLocalizer(repo)
    ap, val = PatchApplier(loc), PatchValidator(TestExecutor(timeout_s=60))
    test = ReproTest(1, REPRO_BOUNDS)
    files = val.select_tests(repo, ["shopcart.pricing"])
    one = Patch(1, FIX_DISCOUNT)
    assert ap.apply(one, []) is None
    chk = val.check(repo, one, test, files, "ml", progress_from=2)
    assert chk.category == "partial" and not chk.accepted and chk.repro_failed == 1
    assert chk.repro_failing == ["test_free_shipping_at_500"]
    # without progress tracking (fix_full) the same patch is simply rejected
    assert val.check(repo, one, test, files, "ml").category == "repro_still_fails"
    both = Patch(2, FIX_DISCOUNT + "\n\n" + FIX_SHIPPING)      # one answer, two definitions
    assert ap.apply(both, []) is None
    assert set(changed_definitions(loc.src("shopcart/pricing.py"), both.files["shopcart/pricing.py"])) == {
        "bulk_discount", "shipping_fee"}
    assert val.check(repo, both, test, files, "ml", progress_from=2).accepted


def test_sibling_finder_spots_same_mistake_in_other_function():
    repo = _need("ml_boundaries")
    loc = FaultLocalizer(repo)
    p = Patch(1, FIX_DISCOUNT)
    assert PatchApplier(loc).apply(p, []) is None
    original = {"shopcart/pricing.py": loc.src("shopcart/pricing.py")}
    sibs = find_siblings(loc, original, p.files, ["shopcart/pricing.py", "shopcart/cart.py"])
    assert sibs and sibs[0].name == "shipping_fee"
    assert "bulk_discount" not in [s.name for s in sibs]


def test_cross_file_patch_and_cumulative_diff():
    repo = _need("ml_shipping")
    loc = FaultLocalizer(repo)
    cart = loc.src("shopcart/cart.py")
    i = cart.index("def checkout")
    fixed_checkout = cart[i:].replace("shipping_fee(subtotal)", "shipping_fee(taxable)")
    p = Patch(1, 'SHIPPING_FEE = Decimal("50")\n\n' + fixed_checkout)
    assert PatchApplier(loc).apply(p, []) is None
    assert set(p.files) == {"shopcart/cart.py", "shopcart/pricing.py"}
    d = cumulative_diff(repo, p.files)
    assert '+SHIPPING_FEE = Decimal("50")' in d and "+    ship = shipping_fee(taxable)" in d


def test_focus_test_keeps_only_failing_functions():
    from reproagent.agent import focus_test
    out = focus_test(REPRO_BOUNDS, ["test_free_shipping_at_500"])
    assert "def test_free_shipping_at_500" in out and "def test_discount_at_1000" not in out
    assert out.startswith("from shopcart.pricing import")
