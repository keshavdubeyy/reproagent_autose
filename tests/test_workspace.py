from pathlib import Path

import pytest

from reproagent.pipeline import classify_failure
from reproagent.workspace import Workspace

TOY = Path(__file__).resolve().parents[1] / "examples" / "toy_calc"


@pytest.fixture
def ws():
    w = Workspace(TOY)
    yield w
    w.cleanup()


def test_copy_is_isolated(ws):
    assert ws.root != TOY and (ws.root / "calc" / "stats.py").exists()


def test_path_escape_blocked(ws):
    with pytest.raises(ValueError):
        ws.read_file("../../etc/passwd")


def test_search(ws):
    assert "stats.py" in ws.search_code(r"def median")


def test_edit_requires_unique_match(ws):
    assert ws.edit_file("calc/stats.py", "return s[mid]", "x").startswith("ERROR")
    assert ws.edit_file("calc/stats.py", "not there", "x").startswith("ERROR")


def test_cannot_edit_repro_test(ws):
    ws.write_test("def test_x(): assert False\n")
    assert "read-only" in ws.edit_file(ws.test_path(), "False", "True")


def test_run_and_classify(ws):
    ws.write_test("from calc.stats import median\n\ndef test_m():\n    assert median([1,2,3,4]) == 2.5\n")
    code, out = ws.run_pytest(ws.test_path())
    assert classify_failure(code, out)[0]


def test_import_error_is_not_a_reproduction(ws):
    ws.write_test("import nope\n\ndef test_m():\n    assert True\n")
    code, out = ws.run_pytest(ws.test_path())
    assert not classify_failure(code, out)[0]


def test_usage_error_is_not_a_reproduction():
    assert not classify_failure(4, "error: unrecognized arguments")[0]


def test_preflight_ok(ws):
    assert ws.preflight()[0]


def test_edit_disambiguates_by_line(ws):
    msg = ws.edit_file("calc/stats.py", "    return s[mid]", "    return 0")
    assert "lines [12, 13]" in msg
    ws.edit_file("calc/stats.py", "    return s[mid]", "    return (s[mid - 1] + s[mid]) / 2", line="13")
    code, out = ws.run_pytest("tests/test_stats.py")
    assert code == 0, out


def test_existing_tests_are_read_only(ws):
    assert "read-only" in ws.edit_file("tests/test_stats.py", "median", "mean")


def test_line_hint_must_match_single_occurrence(ws):
    ws.edit_file("calc/stats.py", "    return s[mid]", "    return (s[mid - 1] + s[mid]) / 2", line="13")
    # the remaining match is inside the more-indented line 12, so line=13 must be refused
    assert ws.edit_file("calc/stats.py", "    return s[mid]", "x", line="13").startswith("ERROR")
