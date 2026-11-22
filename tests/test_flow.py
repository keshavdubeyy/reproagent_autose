"""Control flow, data flow and slicing (autose/intel/flow.py)."""
import textwrap

from autose.intel.flow import flow_of

SRC = textwrap.dedent('''
    def price(items, threshold, rate):
        total = 0
        count = 0
        for it in items:
            total += it.cost
            count += 1
        log("priced", count)
        if total > threshold:
            discount = bulk_discount(total, rate)
        else:
            discount = 0
        return total - discount

    class Cart:
        def checkout(self, user):
            self.items = load(user)
            note = audit(user)
            try:
                value = price(self.items, LIMIT, self.rate)
            except ValueError as e:
                value = fallback(e)
            return value
''')


def lines(sl):
    src = SRC.splitlines()
    return [src[i - 1].strip() for i in sl.lines]


def test_slice_from_return_keeps_only_what_affects_the_result():
    f = flow_of(SRC, "price")
    sl = f.slice_from_returns()
    got = lines(sl)
    assert "return total - discount" in got
    assert "discount = bulk_discount(total, rate)" in got and "if total > threshold:" in got
    assert "total += it.cost" in got and "for it in items:" in got
    assert "count = 0" not in got and 'log("priced", count)' not in got     # irrelevant to the result
    assert "bulk_discount" in sl.calls and "log" not in sl.calls
    assert sl.params == {"items", "threshold", "rate"}


def test_slice_from_a_line_and_variable():
    f = flow_of(SRC, "price")
    line = SRC.splitlines().index('    log("priced", count)') + 1
    sl = f.slice_from_line(line)
    assert lines(sl) == ["count = 0", "for it in items:", "count += 1", 'log("priced", count)']
    assert sl.params == {"items"}


def test_method_with_attributes_and_try():
    f = flow_of(SRC, "Cart.checkout")
    sl = f.slice_from_returns()
    calls = sl.calls
    assert "price" in calls and "load" in calls and "fallback" in calls
    assert "audit" not in calls                                   # its result is never used
    assert f.stats()["cfg_edges"] > 0 and f.stats()["data_edges"] > 0


def test_loop_back_edge_carries_data():
    src = "def f(n):\n    x = 0\n    while n:\n        x = x + n\n        n -= 1\n    return x\n"
    sl = flow_of(src, "f").slice_from_returns()
    assert sl.lines == [2, 3, 4, 5, 6]


def test_unknown_or_broken_function():
    assert flow_of(SRC, "nope") is None
    assert flow_of("def broken(:\n", "broken") is None


def test_fault_localizer_prefers_callees_inside_the_slice(tmp_path):
    from autose.agent.models import BugReport, BugSpec
    from autose.agent.repair import FaultLocalizer
    pkg = tmp_path / "shop"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "pricing.py").write_text(textwrap.dedent('''
        def record_metrics(total):
            print("metric", total)

        def apply_tax(total):
            return total * 1.18

        def final_price(total):
            record_metrics(total)
            return apply_tax(total)
    '''))
    test = "from shop.pricing import final_price\n\ndef test_it():\n    assert final_price(100) == 118\n"
    spec, rep = BugSpec("", "", ""), BugReport("x", "wrong final price", "final price is wrong")

    def rank(use_slicing):
        cands = FaultLocalizer(tmp_path, use_slicing=use_slicing).localize(rep, spec, test, None)
        return {c.name: c.score for c in cands}

    with_slice, without = rank(True), rank(False)
    assert with_slice["apply_tax"] > with_slice["record_metrics"]          # only apply_tax reaches the result
    assert without["apply_tax"] == without["record_metrics"]               # old behaviour: no difference
