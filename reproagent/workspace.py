"""Sandboxed workspace: an isolated copy of the target repo plus path-confined tools.

Isolation (see docs/adr/0003-sandbox.md): the agent never touches the user's checkout.
All paths are resolved inside the copy, commands run with a timeout and a scrubbed env.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", "*.pyc", ".pytest_cache")
REPRO_TEST = "test_repro_agent.py"


class Workspace:
    def __init__(self, src: str | Path, python: str | None = None):
        self.src = Path(src).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="reproagent_")).resolve() / self.src.name
        shutil.copytree(self.src, self.root, ignore=IGNORE)
        self.python = os.path.abspath(python) if python else sys.executable
        self.original: dict[str, str] = {}  # rel path -> original text, for diffing

    # ---- path safety -------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError(f"path escapes workspace: {rel}")
        return p

    # ---- read-only tools ---------------------------------------------
    def list_files(self, subdir: str = ".") -> str:
        base = self._resolve(subdir)
        files = sorted(str(p.relative_to(self.root)) for p in base.rglob("*.py"))
        return "\n".join(files[:200]) or "(no python files)"

    def search_code(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for p in sorted(self.root.rglob("*.py")):
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p.relative_to(self.root)}:{i}: {line.strip()}")
                    if len(hits) >= 50:
                        return "\n".join(hits) + "\n(truncated at 50 hits)"
        return "\n".join(hits) or "(no matches)"

    def read_file(self, path: str, start: int = 1, end: int = 400) -> str:
        lines = self._resolve(path).read_text(errors="ignore").splitlines()
        start, end = max(1, int(start)), min(len(lines), int(end))
        return "\n".join(f"{i:4d} {lines[i-1]}" for i in range(start, end + 1))

    # ---- write tools ---------------------------------------------------
    def write_test(self, content: str) -> str:
        p = self._resolve(f"tests/{REPRO_TEST}" if (self.root / "tests").is_dir() else REPRO_TEST)
        p.write_text(content)
        return f"wrote {p.relative_to(self.root)}"

    def test_path(self) -> str:
        return f"tests/{REPRO_TEST}" if (self.root / "tests").is_dir() else REPRO_TEST

    @staticmethod
    def is_test_file(path: str) -> bool:
        name = Path(path).name
        return name.startswith("test_") or name.endswith("_test.py") or "tests" in Path(path).parts or name == "conftest.py"

    def edit_file(self, path: str, old: str, new: str, line: str = "") -> str:
        """Replace `old` with `new`. If `old` occurs several times, `line` picks the occurrence starting on that line."""
        if self.is_test_file(path):
            return "ERROR: test files are read-only while fixing; change the project's source code instead"
        p = self._resolve(path)
        text = p.read_text()
        starts, i = [], text.find(old)
        while old and i != -1:
            starts.append(i)
            i = text.find(old, i + 1)
        if not starts:
            return f"ERROR: `old` text not found in {path}. read_file it and copy the exact text, including indentation."
        if len(starts) > 1:
            lines_at = [text.count("\n", 0, k) + 1 for k in starts]
            if not str(line).strip():
                return (f"ERROR: `old` occurs {len(starts)} times in {path}, starting on lines {lines_at}. "
                        "Call edit_file again with `line` set to the line number of the occurrence to change.")
            try:
                pick = starts[lines_at.index(int(line))]
            except (ValueError, TypeError):
                return f"ERROR: no occurrence of `old` starts on line {line}; occurrences start on lines {lines_at}"
        else:
            pick = starts[0]
            ln0 = text.count("\n", 0, pick) + 1
            if str(line).strip() and str(ln0) != str(line).strip():
                return f"ERROR: `old` starts on line {ln0}, not line {line}. Check indentation and read_file again."
        self.original.setdefault(path, text)
        p.write_text(text[:pick] + new + text[pick + len(old):])
        ln = text.count("\n", 0, pick) + 1
        return f"edited {path}; now:\n" + self.read_file(path, max(1, ln - 3), ln + new.count("\n") + 3)

    def replace_lines(self, path: str, start: str, end: str, new: str) -> str:
        """Replace lines start..end (1-based, inclusive) with `new`. Returns the edited region."""
        if self.is_test_file(path):
            return "ERROR: test files are read-only while fixing; change the project's source code instead"
        p = self._resolve(path)
        text = p.read_text()
        lines = text.splitlines(True)
        s, e = int(start), int(end)
        if not (1 <= s <= e <= len(lines)):
            return f"ERROR: invalid range {s}-{e} (file has {len(lines)} lines)"
        self.original.setdefault(path, text)
        repl = new if new.endswith("\n") else new + "\n"
        lines[s - 1:e] = [repl]
        p.write_text("".join(lines))
        return f"edited {path}; now:\n" + self.read_file(path, max(1, s - 3), s + repl.count("\n") + 2)

    def revert(self):
        """Undo all source edits (used between failed fix attempts)."""
        for rel, before in self.original.items():
            self._resolve(rel).write_text(before)
        self.original.clear()

    def diff(self) -> str:
        import difflib
        out = []
        for rel, before in self.original.items():
            after = self._resolve(rel).read_text()
            out += difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{rel}", f"b/{rel}")
        return "".join(out)

    # ---- execution -------------------------------------------------------
    def run_pytest(self, target: str | None = None, timeout: int = 60) -> tuple[int, str]:
        args = [self.python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--rootdir", str(self.root)]
        if target:
            args.append(target)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "PYTHONPATH")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            r = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=timeout, env=env)
            out = (r.stdout + r.stderr)[-4000:]
            return r.returncode, out
        except subprocess.TimeoutExpired:
            return -1, f"TIMEOUT after {timeout}s"

    def failing_tests(self, timeout: int = 600) -> set[str] | None:
        """Run the whole existing suite (minus the repro test) and return the ids of failing tests."""
        args = [self.python, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider", "--rootdir", str(self.root),
                "--ignore", self.test_path()]
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(self.root)
        try:
            r = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return None
        return set(re.findall(r"^(?:FAILED|ERROR) (\S+)", r.stdout, re.M))

    def preflight(self) -> tuple[bool, str]:
        """Can pytest run a trivial test in this environment? Separates infra failures from agent failures."""
        probe = self.root / "test_reproagent_preflight.py"
        probe.write_text("def test_preflight():\n    assert True\n")
        code, out = self.run_pytest(probe.name)
        probe.unlink()
        return code == 0, out[-1500:]

    def cleanup(self):
        shutil.rmtree(self.root.parent, ignore_errors=True)
