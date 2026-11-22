"""Glue between the ported agent and SWE-bench: patch format, sandbox bookkeeping, report building."""
import shutil
import subprocess

import pytest

from autose.agent.repair import cumulative_diff
from autose.agent.workspace import TEST_FILE, Sandbox, swebench_image
from autose.run import to_report


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_multi_file_diff_applies_with_git(tmp_path):
    """The SWE-bench evaluator applies our patch with `git apply`; a blank line between files or a
    missing final newline would make it fail."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("X = 1")                 # no final newline
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    diff = cumulative_diff(tmp_path, {"pkg/a.py": "def f():\n    return 2\n", "pkg/b.py": "X = 2\n"})
    assert "\n\ndiff --git" not in diff
    (tmp_path / "p.diff").write_text(diff)
    r = subprocess.run(["git", "apply", "--check", "p.diff"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_sandbox_tracks_written_and_removed_files(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    sb = Sandbox(tmp_path, "unit/../weird name")
    try:
        sb.write("m.py", "x = 2\n")
        sb.write(TEST_FILE, "def test_a():\n    pass\n")
        sb.remove(TEST_FILE)
        assert sb.written == {"m.py", TEST_FILE}       # removal must reach the container too
        assert (tmp_path / "m.py").read_text() == "x = 1\n"   # original untouched
    finally:
        sb.cleanup()


def test_swebench_image_name():
    assert swebench_image("django__django-11099") == "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


def test_report_variants_keep_issue_text_and_add_sections():
    row = {"instance_id": "x__y-1", "bug_report": {
        "Description": "Crash on empty input\r\nCalling f('') raises.",
        "StepsToReproduce": ["call f('')"], "ExpectedBehavior": "returns ''", "ObservedBehavior": "IndexError"}}
    raw, full = to_report(row, "raw"), to_report(row, "full")
    assert raw.title == full.title == "Crash on empty input"
    assert "Expected behavior" not in raw.body
    assert "## Expected behavior\nreturns ''" in full.body and "- call f('')" in full.body


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_patch_applier_diff_applies_with_git(tmp_path):
    """The main fix path (PatchApplier) must produce the same git-applicable format."""
    from autose.agent.models import FaultCandidate, Patch
    from autose.agent.repair import FaultLocalizer, PatchApplier
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "m.py").write_text("LIMIT = 100\n\n\ndef ok(x):\n    return x > LIMIT\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    loc = FaultLocalizer(tmp_path)
    cands = [FaultCandidate("pkg/m.py", "pkg.m", "ok", 4, 5, "def ok(x):\n    return x > LIMIT\n", 1.0)]
    patch = Patch(1, "def ok(x):\n    return x >= LIMIT\n")
    assert PatchApplier(loc).apply(patch, cands) is None
    assert patch.diff.startswith("diff --git a/pkg/m.py b/pkg/m.py\n")
    (tmp_path / "p.diff").write_text(patch.diff)
    r = subprocess.run(["git", "apply", "--check", "p.diff"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
