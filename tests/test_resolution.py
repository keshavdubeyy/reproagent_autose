"""Unit tests of the Bug Resolution tools (fault localizer, patch applier, patch validator). No SLM."""
from pathlib import Path

import pytest

from autose.agent.models import BugReport, BugSpec, Patch, ReproTest
from autose.agent.repair import FaultLocalizer, PatchApplier, definitions
from autose.agent.verify import PatchValidator, TestExecutor, run_python_check

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "benchmarks" / "shopcart" / "work"

REPRO_SETQ = """from shopcart import Cart, Product

def test_zero_removes():
    cart = Cart()
    cart.add(Product("P1", "Pen", 20), 2)
    cart.set_quantity("P1", 0)
    assert "P1" not in cart.items
"""

REPRO_DISCOUNT = """from shopcart import Cart, Product, checkout

def test_no_discount_below_1000():
    cart = Cart()
    cart.add(Product("P1", "Pen", 800))
    bill = checkout(cart)
    assert bill["discount"] == 0
"""


def _need(bug):
    if not (WORK / bug / "buggy").exists():
        pytest.skip("run benchmarks/shopcart/build.py first")
    return WORK / bug / "buggy"


def test_definitions_index_methods_and_constants():
    d = definitions("X = 1\n\nclass A:\n    def m(self):\n        return X\n\ndef f():\n    pass\n")
    assert d["X"][:2] == (1, 1) and d["A.m"] == (4, 5, 4) and d["f"][:2] == (7, 8)


def test_localizer_follows_asserted_call_into_callee_and_constant():
    repo = _need("shop_discount")
    spec = BugSpec("", "discount should be 0 below Rs 1,000", "")
    rep = BugReport("shop_discount", "Discount is incorrectly applied to orders below Rs 1,000", "")
    names = [c.name for c in FaultLocalizer(repo).localize(rep, spec, REPRO_DISCOUNT, "")]
    assert "checkout" in names and "bulk_discount" in names and "DISCOUNT_THRESHOLD" in names


def test_applier_replaces_method_inside_class_and_reports_diff():
    repo = _need("shop_set_quantity_zero")
    loc = FaultLocalizer(repo)
    cands = loc.localize(BugReport("x", "", ""), BugSpec("", "", ""), REPRO_SETQ, "")
    patch = Patch(1, """class Cart:
    def set_quantity(self, sku, quantity):
        if quantity < 0:
            raise ValueError("quantity cannot be negative")
        if quantity == 0:
            self.items.pop(sku, None)
            return
        self.items[sku][1] = quantity
""")
    assert PatchApplier(loc).apply(patch, cands) is None
    new = patch.files["shopcart/cart.py"]
    assert "    def set_quantity(self, sku, quantity):\n        if quantity < 0:" in new
    assert "def remove(self" in new          # other methods untouched
    assert "+        if quantity == 0:" in patch.diff


def test_applier_replaces_constant_and_rejects_unknown_or_identical_code():
    repo = _need("shop_discount")
    loc = FaultLocalizer(repo)
    ap = PatchApplier(loc)
    p = Patch(1, "DISCOUNT_THRESHOLD = 1000  # fixed\n")
    assert ap.apply(p, []) is None and "DISCOUNT_THRESHOLD = 1000" in p.files["shopcart/pricing.py"]
    assert "not found" in ap.apply(Patch(2, "def no_such_function():\n    pass\n"), [])
    same = loc.src("shopcart/pricing.py").splitlines()
    i = next(i for i, l in enumerate(same) if l.startswith("def gst"))
    assert "identical" in ap.apply(Patch(3, "\n".join(same[i:i + 3]) + "\n"), [])
    assert "not valid Python" in ap.apply(Patch(4, "def f(:\n"), [])


def test_validator_accepts_correct_patch_and_rejects_non_fix():
    repo = _need("shop_set_quantity_zero")
    loc = FaultLocalizer(repo)
    ap, val = PatchApplier(loc), PatchValidator(TestExecutor(timeout_s=60))
    files = val.select_tests(repo, ["shopcart.cart"])
    assert files == ["tests/test_cart.py"]
    good = Patch(1, "class Cart:\n    def set_quantity(self, sku, quantity):\n        if quantity < 0:\n"
                    "            raise ValueError('neg')\n        if quantity == 0:\n"
                    "            self.items.pop(sku, None)\n            return\n"
                    "        self.items[sku][1] = quantity\n")
    assert ap.apply(good, []) is None
    chk = val.check(repo, good, ReproTest(1, REPRO_SETQ), files, "unit")
    assert chk.accepted and chk.category == "plausible"
    bad = Patch(2, "class Cart:\n    def set_quantity(self, sku, quantity):\n"
                   "        self.items[sku][1] = quantity\n")
    assert ap.apply(bad, []) is None
    chk = val.check(repo, bad, ReproTest(1, REPRO_SETQ), files, "unit")
    assert not chk.accepted and chk.category == "repro_still_fails"
    ok, _ = run_python_check(repo, good.files, "from shopcart import Cart, Product\nc = Cart()\n"
                             "c.add(Product('A', 'a', 1))\nc.set_quantity('A', 0)\nassert not c.items")
    assert ok


def test_applier_handles_copied_header_comment_above_indented_method():
    repo = _need("shop_set_quantity_zero")
    loc = FaultLocalizer(repo)
    p = Patch(1, "# shopcart/cart.py (module shopcart.cart) - method called\n"
                 "    def remove(self, sku):\n        return self.items.pop(sku, None) is not None\n")
    assert PatchApplier(loc).apply(p, []) is None
    assert "    def remove(self, sku):\n        return self.items.pop" in p.files["shopcart/cart.py"]


def test_report_symbol_finds_method():
    repo = _need("shop_set_quantity_zero")
    spec = BugSpec("", "", "", symbols=["Cart.set_quantity"])
    names = [c.name for c in FaultLocalizer(repo).localize(BugReport("x", "", ""), spec, REPRO_SETQ_REMOVE, "")]
    assert names[0] == "Cart.set_quantity"


REPRO_SETQ_REMOVE = """from shopcart import Cart, Product

def test_zero():
    cart = Cart()
    p = Product("P1", "Product 1", 100)
    cart.add(p, 2)
    cart.set_quantity(p.sku, 0)
    assert cart.remove(p.sku) == False
"""


def test_new_import_goes_after_multiline_import():
    lines = ["import os", "from x import (", "    a,", "    b,", ")", "", "def f():", "    pass"]
    out = PatchApplier._add_imports(lines, ["import re"])
    assert out[5] == "import re" and out[1:5] == lines[1:5]


# Night 1: sympy-14774's right answer (a table local to a method) was rejected as "not found in the project".
PRINTER = '''def latex(expr):
    return Printer().doprint(expr)


class Printer:
    def _print_Function(self, func):
        inv_trig_table = ["asin", "acos"]
        return func in inv_trig_table

    def _print_Other(self, x):
        return x
'''


def _printer_repo(tmp_path, src=PRINTER):
    (tmp_path / "printing.py").write_text(src)
    loc = FaultLocalizer(tmp_path)
    c = loc._candidate("printing.py", "latex", 1.0, "test")   # the localiser named the file, not the method
    return loc, [c]


def test_applier_replaces_a_table_local_to_a_method_in_a_candidate_file(tmp_path):
    loc, cands = _printer_repo(tmp_path)
    p = Patch(1, 'inv_trig_table = ["asin", "acos", "acsc"]\n')
    assert PatchApplier(loc).apply(p, cands) is None
    assert '        inv_trig_table = ["asin", "acos", "acsc"]' in p.files["printing.py"]
    body = [l for l in p.diff.splitlines() if l[:1] in "+-" and not l.startswith(("---", "+++"))]
    assert body == ['-        inv_trig_table = ["asin", "acos"]', '+        inv_trig_table = ["asin", "acos", "acsc"]']


def test_applier_rejects_a_local_name_assigned_in_two_places(tmp_path):
    src = PRINTER.replace("        return x\n", "        inv_trig_table = []\n        return x\n")
    loc, cands = _printer_repo(tmp_path, src)
    p = Patch(1, 'inv_trig_table = ["asin"]\n')
    assert "not found in the project" in PatchApplier(loc).apply(p, cands)


def test_patch_generator_reasks_when_the_answer_is_cut_off():
    from autose.agent.repair import CUT_OFF_HINT, PatchGenerator

    class LLM:
        prompts = []

        def chat(self, system, prompt, **k):
            self.prompts.append(prompt)
            return "```python\ndef f():\n    return [1," if len(self.prompts) == 1 else "```python\ndef f():\n    return 2\n```"

    llm = LLM()
    p = PatchGenerator(llm).generate(BugReport("x", "t", "b"), BugSpec("s", "e", "o"), [], "def test(): pass",
                                     "E   assert 1 == 2", [], 1)
    assert len(llm.prompts) == 2 and llm.prompts[1].endswith(CUT_OFF_HINT) and "return 2" in p.code


def test_applier_adds_a_new_method_to_an_existing_class(tmp_path):
    # Night 1, django-13220: the fix adds ValidationError.__eq__, which did not exist yet.
    loc, cands = _printer_repo(tmp_path)
    cands = [loc._candidate("printing.py", "Printer._print_Function", 1.0, "test")]
    p = Patch(1, "class Printer:\n    def __eq__(self, other):\n        return True\n")
    assert PatchApplier(loc).apply(p, cands) is None
    new = p.files["printing.py"]
    assert "    def __eq__(self, other):\n        return True\n" in new
    assert new.index("_print_Other") < new.index("__eq__")       # appended after the last method
    assert "def _print_Function" in new and "def latex" in new   # nothing else replaced
