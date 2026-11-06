"""Unit tests of the deterministic (Function/Tool) components. No SLM required."""
from pathlib import Path

from reproagent.issue_management import BugSpecifier, IssueRetrieval
from reproagent.models import BugReport, BugSpec, ExecutionResult, ReproTest
from reproagent.reproduction import RepoExplorer
from reproagent.verification import (ResultAnalyzer, Sandbox, TestExecutor, evidence_match,
                                     parse_pytest, raised_in_test)

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "benchmarks" / "shopcart" / "app"


def test_issue_retrieval_json(tmp_path):
    p = tmp_path / "bug.json"
    p.write_text('{"id": "b1", "title": "T", "body": "B"}', encoding="utf-8")
    r = IssueRetrieval().load(str(p))
    assert (r.id, r.title, r.body) == ("b1", "T", "B")


def test_heuristic_spec_extracts_symbols_and_kind():
    rep = BugReport("x", "str_to_int crashes", "str_to_int(523) raises TypeError in youtube_dl/utils.py")
    spec = BugSpecifier(None).specify(rep)
    assert "str_to_int" in spec.symbols
    assert "youtube_dl/utils.py" in spec.files
    assert spec.failure_kind == "exception"


def test_explorer_finds_relevant_function():
    spec = BugSpec(summary="", expected="", observed="", symbols=["gst"])
    ctx = RepoExplorer(SHOP).search(spec, BugReport("x", "GST off by one paisa", "gst rounding"))
    assert ctx.snippets[0].name == "gst"
    assert ctx.snippets[0].module == "shopcart.pricing"
    assert any("from shopcart" in l for l in ctx.usage_examples)


def test_symbol_hints_for_hallucinated_api():
    ex = RepoExplorer(SHOP)
    h = ex.symbol_hint("ModuleNotFoundError: No module named 'shopcart.product'",
                       "from shopcart.product import Product\n")
    assert "shopcart.cart.Product" in h
    h2 = ex.symbol_hint("AttributeError: 'Cart' object has no attribute 'products'", "")
    assert "items" in h2 and "set_quantity" in h2


def test_parse_pytest_assertion():
    out = ("test_reproagent_repro.py:5: in test_a\n    assert f(1) == 2\n"
           "E   AssertionError: assert 1 == 2\n1 failed in 0.01s")
    r = parse_pytest(out, 1, 0.1)
    assert r.status == "failed" and r.exception_type == "AssertionError"


def test_raised_in_test_vs_project():
    in_test = ("test_reproagent_repro.py:4: in test_a\n    f(1, 2, 3)\nE   TypeError: f() takes 1 positional argument")
    in_proj = ("test_reproagent_repro.py:4: in test_a\n    f('x')\npkg/mod.py:10: in f\n    return int(x)\n"
               "E   ValueError: invalid literal")
    assert raised_in_test(in_test)
    assert not raised_in_test(in_proj)


def test_analyzer_rules_without_llm():
    an = ResultAnalyzer(None)
    spec = BugSpec("", "", "")
    rep, t = BugReport("x", "", ""), ReproTest(1, "")
    assert an.analyze(rep, spec, t, ExecutionResult("passed", 0, 0, "")).category == "test_passed"
    assert an.analyze(rep, spec, t, ExecutionResult("collection_error", 2, 0, "E ImportError",
                                                    "ImportError")).category == "invalid_test"
    fail = ExecutionResult("failed", 1, 0, "E   AssertionError", "AssertionError", "assert 1 == 2")
    assert an.analyze(rep, spec, t, fail).is_reproduction


def test_evidence_match_accepts_observed_value_and_rejects_other_scenario():
    rep = BugReport("d", "Discount below 1000", "Add Rs 800. Actual: a 10% discount (Rs 80) is applied, giving Rs 720.")
    spec = BugSpec("", "discount should be 0", "a 10% discount (Rs 80) is applied, amount Rs 720")
    ok = ExecutionResult("failed", 1, 0, "E   AssertionError: assert Decimal('80.00') == 0", "AssertionError")
    bad = ExecutionResult("failed", 1, 0, "E   AssertionError: assert Decimal('200.00') == 0", "AssertionError")
    assert evidence_match(rep, spec, ok)[0] is True
    assert evidence_match(rep, spec, bad)[0] is False


def test_evidence_match_exception_named_in_report():
    rep = BugReport("r", "Removing twice crashes", "The second remove crashes with a KeyError.")
    res = ExecutionResult("failed", 1, 0, "pkg/cart.py:3: in remove\nE   KeyError: 'P1'", "KeyError")
    assert evidence_match(rep, BugSpec("", "", "", failure_kind="exception"), res)[0] is True


def test_executor_runs_in_sandbox_and_leaves_original_untouched():
    sb = Sandbox(SHOP, "unit")
    try:
        code = "from shopcart.pricing import gst\nfrom decimal import Decimal\n\n" \
               "def test_g():\n    assert gst(100) == Decimal('17.00')\n"
        res = TestExecutor(timeout_s=60).run(sb, ReproTest(1, code))
        assert res.status == "failed" and res.exception_type == "AssertionError"
        assert not (SHOP / "test_reproagent_repro.py").exists()
    finally:
        sb.cleanup()
