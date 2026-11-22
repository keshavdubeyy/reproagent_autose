"""Where the agent's files live and where its tests run.

The agent always reads and edits a private host copy of the project (a `Sandbox`). An `Executor`
decides where pytest runs on that copy:

* LocalExecutor  - pytest in a subprocess on this machine (small projects: ShopCart, BugsInPy).
* DockerExecutor - pytest inside the official SWE-bench image for the bug (repo at the buggy commit
                   with its exact Python/conda environment at /testbed). Only the files the agent wrote
                   are copied in before each run, and they are rolled back before the next one, so every
                   run starts from the buggy commit plus this sandbox's own changes.

Everything else in the agent (test generation, analysis, fault localisation, patching) works on the
host copy and does not know which executor is used.
"""
from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .models import ExecutionResult, ReproTest

TEST_FILE = "test_reproagent_repro.py"
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".tox", ".venv", "build", "dist",
                                ".autose_snapshot")
PYTEST_FLAGS = ["-p", "no:cacheprovider", "-p", "no:warnings"]   # old projects emit many warnings


class Sandbox:
    """Private copy of a project. The original tree is never modified.

    Write files through `write()` so the executor knows what to copy into a container.
    """

    def __init__(self, src: Path, label: str):
        self.src = Path(src)
        safe = re.sub(r"[^\w.-]", "_", label)
        self.root = Path(tempfile.mkdtemp(prefix=f"autose_{safe}_"))
        self.repo = self.root / "repo"
        shutil.copytree(self.src, self.repo, ignore=IGNORE)
        (self.root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        self.written: set[str] = set()

    def write(self, rel: str, text: str) -> None:
        rel = rel.replace("\\", "/")
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(text, encoding="utf-8")
        self.written.add(rel)

    def remove(self, rel: str) -> None:
        rel = rel.replace("\\", "/")
        (self.repo / rel).unlink(missing_ok=True)
        self.written.add(rel)          # an executor must delete it in the container too

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class Executor:
    """Runs pytest on a sandbox. Subclasses implement `_pytest`."""

    def __init__(self, timeout_s: int = 90):
        self.timeout_s = timeout_s

    def sandbox(self, src: Path, label: str) -> Sandbox:
        return Sandbox(src, label)

    def run(self, sb: Sandbox, test: ReproTest, stop_first: bool = True) -> ExecutionResult:
        """Write the reproduction test into the sandbox and run it."""
        from .verify import parse_pytest   # verify imports this module
        sb.write(TEST_FILE, test.code)
        args = [TEST_FILE, "-q", *(["-x"] if stop_first else []), "--tb=short"]
        code, out, dur = self._pytest(sb, args, self.timeout_s)
        if code is None:
            return ExecutionResult("timeout", -1, dur, f"Timed out after {self.timeout_s}s")
        return parse_pytest(out, code, dur)

    def run_files(self, sb: Sandbox, files: list[str], timeout_s: int) -> str | None:
        """Run existing test files (regression suite). Returns pytest output, or None on timeout."""
        code, out, _ = self._pytest(sb, [*files, "-q", "-rfE", "--tb=line"], timeout_s)
        return None if code is None else out

    def _pytest(self, sb: Sandbox, args: list[str], timeout_s: int) -> tuple[int | None, str, float]:
        raise NotImplementedError

    def close(self) -> None:
        pass


# Kept for code and tests written against the mid-term version.
class LocalExecutor(Executor):
    __test__ = False

    def __init__(self, timeout_s: int = 90, python: str = sys.executable,
                 extra_paths: list[str] | None = None):
        super().__init__(timeout_s)
        self.python = python
        self.extra_paths = extra_paths or []   # appended AFTER the project (e.g. compat shims)

    def _pytest(self, sb, args, timeout_s):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(sb.repo)] + self.extra_paths),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        cmd = [self.python, "-m", "pytest", *args, *PYTEST_FLAGS,
               "-c", str(sb.root / "pytest.ini"), "--rootdir", str(sb.repo)]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=sb.repo, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None, "", time.time() - t0
        return p.returncode, (p.stdout + "\n" + p.stderr).strip(), time.time() - t0


TestExecutor = LocalExecutor


def swebench_image(instance_id: str) -> str:
    """Official prebuilt SWE-bench image name (same naming as the swebench package)."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_').lower()}:latest"


class DockerExecutor(Executor):
    """Runs pytest inside one long-lived SWE-bench container per bug.

    `base_patch` (evaluation only) is applied once at start, e.g. the developers' fix to check that a
    reproduction test passes on the fixed version. The agent never gets an executor with a base patch.
    """
    WORKDIR = "/testbed"
    ACTIVATE = "source /opt/miniconda3/bin/activate && conda activate testbed"
    INI = "/tmp/autose_pytest.ini"
    PLATFORM = "linux/amd64"   # SWE-bench images are x86 only (emulated on Apple Silicon)

    def __init__(self, image: str, timeout_s: int = 180, base_patch: str | None = None,
                 name: str | None = None, client=None):
        super().__init__(timeout_s)
        import docker   # only needed for SWE-bench runs
        self.client = client or docker.from_env(timeout=1800)
        self.image = image
        self.name = name or f"autose-agent-{int(time.time() * 1000) % 10**9}"
        self.pushed: set[str] = set()      # paths currently differing from the base state
        try:
            self.client.images.get(image)
        except docker.errors.ImageNotFound:
            repo, tag = image.rsplit(":", 1)
            self.client.images.pull(repo, tag=tag, platform=self.PLATFORM)
        self.container = self.client.containers.create(image, command="tail -f /dev/null", name=self.name,
                                                       platform=self.PLATFORM,
                                                       working_dir=self.WORKDIR, user="root", detach=True)
        self.container.start()
        self._exec(f"printf '[pytest]\\n' > {self.INI}")
        # The repo's own test runner is often not pytest (e.g. Django); our tests need it.
        code, out = self._exec(f"{self.ACTIVATE} && (python -c 'import pytest' 2>/dev/null || "
                               "pip install -q pytest)")
        if code != 0:
            raise RuntimeError(f"could not install pytest in {image}: {out[-500:]}")
        if base_patch:
            self._put(f"/tmp/base.diff", base_patch.encode("utf-8"))
            code, out = self._exec("git apply /tmp/base.diff || patch --batch --forward -p1 -i /tmp/base.diff")
            if code != 0:
                raise RuntimeError(f"base patch did not apply: {out[-500:]}")
            self._exec("git add -A && git -c user.email=a@b -c user.name=autose commit -qm base")

    def _exec(self, cmd: str, timeout_s: int | None = None) -> tuple[int, str]:
        full = f"cd {self.WORKDIR} && {cmd}"
        if timeout_s:
            full = f"timeout {timeout_s} bash -c {shlex.quote(full)}"
        res = self.container.exec_run(["bash", "-c", full], workdir=self.WORKDIR)
        return res.exit_code, res.output.decode("utf-8", "replace")

    def _put(self, path: str, data: bytes) -> None:
        """Copy bytes into the container unchanged (no newline conversion; matters on Windows)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(Path(path).name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
        parent = str(Path(path).parent.as_posix())
        self.container.exec_run(["mkdir", "-p", parent])
        self.container.put_archive(parent, buf.getvalue())

    def _sync(self, sb: Sandbox) -> None:
        """Container = base state + exactly this sandbox's written files."""
        stale = sorted(self.pushed - sb.written)
        if stale:
            quoted = " ".join(shlex.quote(p) for p in stale)
            # tracked files go back to the base commit, files we created are deleted
            self._exec(f"for f in {quoted}; do git checkout -q HEAD -- \"$f\" 2>/dev/null || rm -f \"$f\"; done")
        for rel in sorted(sb.written):
            host = sb.repo / rel
            if host.exists():
                self._put(f"{self.WORKDIR}/{rel}", host.read_bytes())
            else:
                self._exec(f"rm -f {shlex.quote(rel)}")
        self.pushed = set(sb.written)

    def _pytest(self, sb, args, timeout_s):
        self._sync(sb)
        cmd = (f"{self.ACTIVATE} && PYTHONDONTWRITEBYTECODE=1 python -m pytest "
               + " ".join(shlex.quote(a) for a in args) + " " + " ".join(PYTEST_FLAGS)
               + f" -c {self.INI} --rootdir {self.WORKDIR}")
        t0 = time.time()
        code, out = self._exec(cmd, timeout_s)
        dur = time.time() - t0
        if code == 124:      # `timeout` exit code
            return None, out, dur
        return code, out.strip(), dur

    def close(self) -> None:
        try:
            self.container.remove(force=True)
        except Exception:
            pass
