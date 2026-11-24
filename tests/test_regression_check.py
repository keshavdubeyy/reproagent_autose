"""Regression check (Testing): Django's own runner, honest "unverified" results, test selection.

Night 2 showed every accepted Django patch passed a regression check that never ran: pytest could not
collect Django's tests ("settings are not configured") before or after the patch, and the empty
difference read as "no regressions"."""
from autose.agent.models import Patch
from autose.agent.verify import PatchValidator, passed_count
from autose.agent.workspace import LocalExecutor, django_label, unittest_to_pytest

DJANGO_31 = """\
test_a (model_fields.tests.BasicFieldTests) ... ok
test_field_ordering (model_fields.tests.BasicFieldTests) ... FAIL

======================================================================
FAIL: test_field_ordering (model_fields.tests.BasicFieldTests)
----------------------------------------------------------------------
AssertionError: False is not true

----------------------------------------------------------------------
Ran 12 tests in 0.1s

FAILED (failures=1, skipped=2)
"""

DJANGO_42 = """\
ERROR: test_x (admin_utils.tests.UtilsTests.test_x)
----------------------------------------------------------------------
TypeError: boom
ERROR: admin_utils.test_logentry (unittest.loader._FailedTest.admin_utils.test_logentry)
----------------------------------------------------------------------
ImportError: Failed to import test module: admin_utils.test_logentry
Ran 5 tests in 0.2s
"""


def test_django_output_becomes_pytest_result_lines():
    out = unittest_to_pytest(DJANGO_31)
    assert "FAILED model_fields.tests.BasicFieldTests.test_field_ordering" in out.splitlines()
    assert passed_count(out) == 9                              # 12 ran - 1 failed - 2 skipped
    out = unittest_to_pytest(DJANGO_42)                        # newer format: the name is already in ()
    lines = out.splitlines()
    assert "FAILED admin_utils.tests.UtilsTests.test_x" in lines
    assert "FAILED test_x" not in lines
    assert passed_count(out) == 3
    # The original lines are kept for feedback but indented, so they never parse as results.
    assert not any(l.startswith(("ERROR ", "FAIL ")) for l in lines)


def test_django_labels_are_test_apps():
    assert django_label("tests/model_fields/test_promises.py") == "model_fields"
    assert django_label("tests/runtests.py") is None
    assert django_label("sympy/core/tests/test_numbers.py") is None


def test_suite_that_never_passes_is_unverified_not_clean(tmp_path):
    """A test file that cannot even be collected must not make a patch look regression-free."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("import pkg.mod\nimport not_installed_anywhere\n\n"
                                                    "def test_f():\n    assert pkg.mod.f() == 1\n")
    v = PatchValidator(LocalExecutor(timeout_s=60))
    files = v.select_tests(tmp_path, ["pkg.mod"])
    assert files == ["tests/test_mod.py"]
    c = v.check(tmp_path, Patch(1, "", files={"pkg/mod.py": "def f():\n    return 2\n"}), None, files, "u")
    assert c.accepted and c.regression_checked is False
    assert "UNVERIFIED" in c.output


def test_suite_that_runs_is_checked(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("import pkg.mod\n\ndef test_f():\n    assert pkg.mod.f() == 1\n")
    v = PatchValidator(LocalExecutor(timeout_s=60))
    files = v.select_tests(tmp_path, ["pkg.mod"])
    ok = v.check(tmp_path, Patch(1, "", files={"pkg/mod.py": "def f():\n    return 1  # same\n"}), None, files, "a")
    assert ok.accepted and ok.regression_checked is True
    bad = v.check(tmp_path, Patch(2, "", files={"pkg/mod.py": "def f():\n    return 2\n"}), None, files, "b")
    assert bad.category == "regression" and not bad.accepted


def test_selection_prefers_tests_named_after_the_module(tmp_path):
    """django-13220: the fix is in django/core/exceptions.py; the smallest file importing it was an
    unrelated app's tests (staticfiles), the hidden test lives in tests/test_exceptions/."""
    t = tmp_path / "tests"
    (t / "staticfiles_tests").mkdir(parents=True)
    (t / "test_exceptions").mkdir()
    (t / "staticfiles_tests" / "test_utils.py").write_text("from django.core.exceptions import X\n")
    (t / "test_exceptions" / "test_validation_error.py").write_text(
        "from django.core.exceptions import ValidationError\n" + "# test\n" * 300)
    v = PatchValidator(executor=None, max_files=1)
    assert v.select_tests(tmp_path, ["django.core.exceptions"]) == ["tests/test_exceptions/test_validation_error.py"]
