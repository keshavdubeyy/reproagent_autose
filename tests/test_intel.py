"""Knowledge package + hybrid retrieval + localisation scoring on a tiny repository (no model needed)."""
from pathlib import Path

import pytest

from autose.agent.models import BugReport
from autose.evaluation.localization import changed_old_lines, gold_symbols, rank
from autose.intel import index as ix_mod
from autose.intel.index import KnowledgeIndex, build, subtokens
from autose.intel.retrieve import HybridRetriever, identifiers

SRC = {
    "shop/pricing.py": '''"""Prices."""
DISCOUNT_THRESHOLD = 100


def bulk_discount(subtotal):
    """Discount for large orders."""
    if subtotal >= DISCOUNT_THRESHOLD:
        return subtotal * 0.1
    return 0
''',
    "shop/cart.py": '''from shop.pricing import bulk_discount


class Cart:
    def __init__(self):
        self.items = []

    def checkout(self):
        total = sum(self.items)
        return total - bulk_discount(total)
''',
    "shop/util.py": '''def format_money(x):
    if x < 0:
        raise ValueError("negative amount not allowed")
    return f"Rs {x}"
''',
    "tests/test_cart.py": "def test_x():\n    pass\n",
}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(ix_mod, "INDEX_DIR", tmp_path / "idx")
    root = tmp_path / "repo"
    for rel, src in SRC.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(src)
    return root


def test_build_is_cached_and_skips_tests(repo):
    p1 = build(repo, "o/shop", "abc123")
    m = KnowledgeIndex(p1).manifest()
    assert m["files"] == 3 and m["parse_failures"] == 0      # tests/ excluded
    assert build(repo, "o/shop", "abc123") == p1                # reused, not rebuilt


def test_subtokens_split_identifiers():
    assert subtokens("get_FIELD_display")[:1] == ["get_field_display"]
    assert {"get", "field", "display"} <= set(subtokens("get_FIELD_display"))
    assert {"bulk", "discount"} <= set(subtokens("BulkDiscount"))


def test_call_graph_links_checkout_to_bulk_discount(repo):
    ix = KnowledgeIndex(build(repo, "o/shop", "abc123"))
    checkout = ix.by_name("checkout")[0]
    bulk = ix.by_name("bulk_discount")[0]
    assert (bulk, "CALLS>") in ix.neighbours(checkout)
    assert (checkout, "<CALLS") in ix.neighbours(bulk)


@pytest.mark.parametrize("mode", ["bm25", "hybrid", "graph"])
def test_retrieval_finds_the_discount_bug(repo, mode):
    ix = KnowledgeIndex(build(repo, "o/shop", "abc123"))
    r = HybridRetriever(ix, mode).search(BugReport("b", "Discount applied below threshold",
                                                   "An order of 800 gets a bulk discount at checkout."))
    assert r.files[0][0] == "shop/pricing.py" or "shop/pricing.py" in [f for f, _ in r.files[:2]]


def test_error_message_and_traceback_point_to_the_function(repo):
    ix = KnowledgeIndex(build(repo, "o/shop", "abc123"))
    body = ('Traceback (most recent call last):\n  File "/site/shop/util.py", line 3, in format_money\n'
            'ValueError: negative amount not allowed')
    r = HybridRetriever(ix, "hybrid").search(BugReport("b", "Refund crashes", body))
    top = ix.symbol(r.symbols[0].sid)
    assert top["name"] == "format_money"
    assert any(w.startswith("traceback:") for w in r.symbols[0].why)


def test_identifiers_from_report():
    ids = identifiers("Calling `Cart.checkout()` uses bulk_discount and QuerySet.filter( x )")
    assert {"Cart", "checkout", "bulk_discount", "QuerySet", "filter"} <= set(ids)


def test_gold_from_patch(repo):
    patch = ("diff --git a/shop/pricing.py b/shop/pricing.py\n--- a/shop/pricing.py\n+++ b/shop/pricing.py\n"
             "@@ -6,3 +6,3 @@ def bulk_discount(subtotal):\n     \"\"\"Discount for large orders.\"\"\"\n"
             "-    if subtotal >= DISCOUNT_THRESHOLD:\n+    if subtotal >= 1000:\n         return subtotal * 0.1\n")
    assert changed_old_lines(patch) == {"shop/pricing.py": {7}}
    files, syms = gold_symbols(repo, patch)
    assert files == {"shop/pricing.py"} and syms == {("shop/pricing.py", "bulk_discount")}
    assert rank(["a.py", "shop/pricing.py"], files) == 2 and rank(["a.py"], files) is None


def test_src_and_lib_layouts_map_to_import_paths():
    from autose.agent.explore import _module_name
    from autose.intel.index import module_name
    for f in (_module_name, module_name):
        assert f("src/flask/blueprints.py") == "flask.blueprints"
        assert f("lib/matplotlib/axes/_base.py") == "matplotlib.axes._base"
        assert f("src/_pytest/__init__.py") == "_pytest"
        assert f("django/db/models/query.py") == "django.db.models.query"
        assert f("src.py") == "src"


def test_indexed_explorer_returns_code_snippets(repo):
    from autose.agent.explore import IndexedExplorer
    from autose.agent.models import BugSpec
    ex = IndexedExplorer(repo, "hybrid")
    ctx = ex.search(BugSpec("", "no discount below threshold", "discount applied"),
                    BugReport("b", "Bulk discount applied below threshold", "800 order gets bulk_discount"), k=3)
    assert ctx.snippets and ctx.snippets[0].name == "bulk_discount"
    assert ctx.snippets[0].module == "shop.pricing" and "def bulk_discount" in ctx.snippets[0].source
