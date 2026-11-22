"""Guards against invalid reproductions seen on SWE-bench (flask-4045, django-11848)."""
from autose.agent.models import BugReport, BugSpec, ExecutionResult, ReproTest
from autose.agent.verify import ResultAnalyzer, relax_invented_messages

FLASK = BugReport("pallets__flask-4045", "Raise error when blueprint name contains a dot",
                  "This is required since every dot is now significant since blueprints can be nested.")


def test_invented_error_wording_is_dropped():
    code = ("import pytest\nfrom flask import Blueprint\n\ndef test_it():\n"
            "    with pytest.raises(ValueError, match='Blueprint names should not contain dots'):\n"
            "        Blueprint('myapp.frontend', __name__)\n")
    new, removed = relax_invented_messages(code, FLASK)
    assert removed == ["Blueprint names should not contain dots"]
    assert "pytest.raises(ValueError)" in new and "match=" not in new


def test_wording_kept_when_the_report_quotes_it():
    rep = BugReport("x", "Wrong error", "Calling it with -1 should raise 'value must be positive'.")
    code = "import pytest\ndef test_it():\n    with pytest.raises(ValueError, match='value must be positive'):\n        f(-1)\n"
    new, removed = relax_invented_messages(code, rep)
    assert removed == [] and new == code


def test_unittest_assert_raises_regex_becomes_assert_raises():
    code = ("import unittest\nclass T(unittest.TestCase):\n    def test_it(self):\n"
            "        with self.assertRaisesRegex(ValueError, 'no dots allowed'):\n            f('a.b')\n")
    new, removed = relax_invented_messages(code, FLASK)
    assert removed == ["no dots allowed"] and "assertRaises(ValueError)" in new


DJANGO = BugReport("django__django-11848", "django.utils.http.parse_http_date two digit year check is incorrect",
                   "RFC 850 does not mention this, but in RFC 7231 ... two digit years ... current year.")
SPEC = BugSpec("two digit years", "Years more than 50 years in the future map to the past",
               "years 0-69 map to 2000-2069 regardless of the current year", failure_kind="wrong_value")


def run(out, exc):
    return ExecutionResult("failed", 1, 0.1, out, exc, out.splitlines()[-1].lstrip("E "), 1, 0)


def test_wrong_value_bug_failing_with_unrelated_exception_is_rejected():
    out = ("test_reproagent_repro.py:11: in test_it\n    parse_http_date(s)\n"
           "django/utils/http.py:175: in parse_http_date\n    raise ValueError(...)\n"
           "E   ValueError: '01 Jan 1980 00:00:00 GMT' is not in a valid HTTP date format")
    a = ResultAnalyzer(None).analyze(DJANGO, SPEC, ReproTest(1, ""), run(out, "ValueError"))
    assert a.category == "wrong_failure" and not a.is_reproduction
    assert "not valid for this function" in a.feedback


def test_exception_named_in_the_report_still_counts():
    rep = BugReport("x", "parse crashes", "parse('a') raises ValueError instead of returning None")
    out = "src/p.py:3: in parse\nE   ValueError: bad"
    spec = BugSpec("", "returns None", "raises ValueError", failure_kind="wrong_value")
    a = ResultAnalyzer(None).analyze(rep, spec, ReproTest(1, ""), run(out, "ValueError"))
    assert a.category == "reproduced"


def test_regression_tests_follow_the_changed_module(tmp_path):
    from autose.agent.verify import PatchValidator
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_small.py").write_text("import flask\n")
    (tmp_path / "tests" / "test_blueprints.py").write_text("import flask\n" + "# many tests\n" * 200)
    v = PatchValidator(executor=None, max_files=1)
    assert v.select_tests(tmp_path, ["flask.blueprints"]) == ["tests/test_blueprints.py"]


def test_real_django_report_does_not_name_value_error():
    from autose.run import load_reports, to_report
    rep = to_report(load_reports()["django__django-11848"])
    out = "django/utils/http.py:175: in parse_http_date\nE   ValueError: '01 Jan 1980 00:00:00 GMT' is not in a valid HTTP date format"
    a = ResultAnalyzer(None).analyze(rep, SPEC, ReproTest(1, ""), run(out, "ValueError"))
    assert a.category == "wrong_failure"


def test_exception_phrase_in_prose_counts():
    rep = BugReport("x", "crash", "calling parse() gives a value error for empty input")
    spec = BugSpec("", "returns None", "crashes", failure_kind="wrong_value")
    a = ResultAnalyzer(None).analyze(rep, spec, ReproTest(1, ""), run("src/p.py:3: in parse\nE   ValueError: x", "ValueError"))
    assert a.category == "reproduced"
