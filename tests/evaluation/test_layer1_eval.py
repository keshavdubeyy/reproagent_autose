"""Unit tests for the Layer 1 evaluator's patch and test-name parsing."""
from autose.evaluation.layer1_eval import gold_metrics, patch_old_lines, map_test_name

PATCH = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -6,3 +6,4 @@ class Base:
     def __init__(self):
-        self.x = helper()
+        self.x = helper() + 1
+        self.y = 0

diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+X = 1
"""


def test_patch_old_lines():
    lines = patch_old_lines(PATCH)
    assert lines["pkg/new.py"] is None
    assert lines["pkg/core.py"] == {7}  # removed line 7; insertions land after it (line 7 too)


def test_gold_metrics_finds_innermost_entity():
    index = {"files": [{"id": "pkg/core.py"}],
             "entities": [{"id": "pkg/core.py:Base", "file": "pkg/core.py", "start_line": 5, "end_line": 13},
                          {"id": "pkg/core.py:Base.__init__", "file": "pkg/core.py", "start_line": 6, "end_line": 7}]}
    g = gold_metrics(index, PATCH)
    assert g["gold_entities"] == ["pkg/core.py:Base.__init__"]
    assert (g["py_files"], g["new_files"], g["files_in_index"], g["files_existing"]) == (2, 1, 1, 1)


def test_test_name_mapping():
    assert map_test_name("tests/test_x.py::TestA::test_b[1-2]") == ("exact", ["tests/test_x.py:TestA.test_b"])
    mode, cands = map_test_name("test_ok (admin_views.tests.AdminViewTests)")
    assert mode == "exact" and "tests/admin_views/tests.py:AdminViewTests.test_ok" in cands
    assert map_test_name("test_bare_name") == ("name", ["test_bare_name"])
