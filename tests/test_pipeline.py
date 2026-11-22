"""Pipeline health summary (autose/evaluation/pipeline.py)."""
from autose.evaluation.pipeline import CHAIN, summarise, to_html


def bug(iid, **v):
    b = {"instance_id": iid, **{k: None for k in CHAIN}, "search_func": None}
    b.update(v)
    b["first_break"] = next((k for k in CHAIN if b.get(k) is False), None if b.get("resolved") else "unknown")
    return b


def test_stage_rates_handoffs_and_first_break():
    ok = dict(intake=True, search_file=True, repro=True, repro_valid=True, loc_file=True, loc_func=True,
              patch_applies=True, patch_gold=True, self_check=True)
    bugs = [bug("a", **ok, resolved=True),
            bug("b", **{**ok, "repro_valid": False}, resolved=False),
            bug("c", **{**ok, "loc_func": False, "patch_gold": False}, resolved=False)]
    s = summarise(bugs)
    st = {x["key"]: (x["pass"], x["n"]) for x in s["stages"]}
    assert st["repro_valid"] == (2, 3) and st["resolved"] == (1, 3)
    h = {x["label"]: (x["pass"], x["n"]) for x in s["handoffs"]}
    assert h["Own tests → Hidden tests (trust)"] == (1, 3)
    assert s["first_break"] == {"repro_valid": 1, "loc_func": 1}
    assert s["false_confidence"] == 2
    page = to_html({"run": "t", **s, "bugs": bugs})
    assert "Pipeline Health" in page and "first broken checkpoint" in page


def test_gold_symbols_attributes_new_method_to_its_class(tmp_path):
    from autose.evaluation.localization import gold_symbols
    (tmp_path / "m.py").write_text("import os\n\n\nclass E(Exception):\n    def __repr__(self):\n        return 'E'\n\n\n"
                                   "def f():\n    return 1\n")
    new_method = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -5,3 +5,6 @@\n     def __repr__(self):\n"
                  "         return 'E'\n+\n+    def __eq__(self, o):\n+        return True\n \n")
    assert gold_symbols(tmp_path, new_method)[1] == {("m.py", "E")}
    new_import = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,3 @@\n import os\n+import re\n \n"
    assert gold_symbols(tmp_path, new_import)[1] == {("m.py", "<module>")}
    edit = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -9,2 +9,2 @@\n def f():\n-    return 1\n"
            "+    return 2\n")
    assert gold_symbols(tmp_path, edit)[1] == {("m.py", "f")}
    in_body = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -5,2 +5,3 @@\n     def __repr__(self):\n"
               "+        x = 1\n         return 'E'\n")
    assert gold_symbols(tmp_path, in_body)[1] == {("m.py", "__repr__")}


def test_realign_moves_offset_hunk_to_its_context(tmp_path):
    from autose.evaluation.localization import gold_symbols, realign
    (tmp_path / "m.py").write_text("# a\n# b\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n")
    off = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -6,2 +6,2 @@\n def g():\n-    return 2\n+    return 3\n"
    assert "@@ -8,2 " in realign(tmp_path, off)
    assert gold_symbols(tmp_path, off)[1] == {("m.py", "g")}
