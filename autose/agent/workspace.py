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


# pytest plugin for Django projects (SWE-bench runs Django's own tests with `runtests.py --settings=test_sqlite`;
# a plain pytest run of a reproduction test has no settings at all). Only the reproduction test gets it, and
# only when the test does not configure Django itself.
DJANGO_PLUGIN = """
import os
import types

import pytest

REPRO = None                 # module name of the reproduction test, set only for reproduction runs
APP = "autose_repro_app"     # app that models defined inside the reproduction test belong to
_tables_done = False


def _app(apps, label):
    # Register an app for `label` (models written in the test, or a Meta.app_label that is not installed),
    # so Django can find the models, their tables and their reverse relations.
    from django.apps import AppConfig
    cfg = apps.app_configs.get(label)
    if cfg is None:
        module = types.ModuleType(label)
        module.__file__ = __file__
        cfg = AppConfig(label, module)
        cfg.apps, cfg.models = apps, apps.all_models[label]
        apps.app_configs[label] = cfg
        apps.clear_cache()
    return cfg


def _adopt_test_models():
    # A model class written in the test file has no app, so Django raises "doesn't declare an explicit
    # app_label and isn't in an application in INSTALLED_APPS". Give such models a small app of their own.
    from django.apps.registry import Apps
    original = Apps.get_containing_app_config

    def lookup(self, object_name):
        cfg = original(self, object_name)
        if cfg is None and REPRO and object_name.split(".")[0] == REPRO and self.ready:
            cfg = _app(self, APP)
        return cfg
    Apps.get_containing_app_config = lookup


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # Tables for the models the test defines ("no such table"): migrate ran before the test was imported.
    global _tables_done
    if _tables_done or not REPRO:
        return
    _tables_done = True
    try:
        from django.apps import apps
        from django.db import connections
        if not apps.ready:
            return
    except Exception:
        return
    for label in list(apps.all_models):        # Meta.app_label = "test_app" (not installed)
        if label not in apps.app_configs and any(m.__module__.split(".")[0] == REPRO
                                                 for m in apps.all_models[label].values()):
            _app(apps, label)
    models = [m for label in list(apps.all_models) for m in list(apps.all_models[label].values())
              if m.__module__.split(".")[0] == REPRO and m._meta.managed and not m._meta.proxy
              and not m._meta.swapped and not m._meta.auto_created]   # M2M tables come with their model
    for alias in connections:
        conn = connections[alias]
        for m in models:                         # one at a time: one bad model must not block the rest
            try:
                if m._meta.db_table not in conn.introspection.table_names():
                    with conn.schema_editor() as editor:
                        editor.create_model(m)
            except Exception:
                pass


def pytest_configure(config):
    global REPRO
    try:
        import django
        from django.conf import settings
    except ImportError:
        return
    paths = [str(a).split("::")[0] for a in config.args]
    repro = [p for p in paths if p.endswith("%s")]
    if not repro:
        return                                  # regression runs keep the project's own setup
    REPRO = os.path.splitext(os.path.basename(repro[0]))[0]
    _adopt_test_models()
    if settings.configured or os.environ.get("DJANGO_SETTINGS_MODULE"):
        return
    for p in paths:
        try:
            with open(p) as f:
                src = f.read()
        except OSError:
            continue
        if "settings.configure" in src or "DJANGO_SETTINGS_MODULE" in src:
            return                              # the test sets Django up itself
    settings.configure(
        DEBUG=False, SECRET_KEY="autose-repro", USE_TZ=False, SITE_ID=1,
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
                   "other": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "django.contrib.sessions",
                        "django.contrib.messages", "django.contrib.admin", "django.contrib.sites",
                        "django.contrib.staticfiles"],
        MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware",
                    "django.contrib.auth.middleware.AuthenticationMiddleware",
                    "django.contrib.messages.middleware.MessageMiddleware"],
        TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
                    "OPTIONS": {"context_processors": ["django.contrib.auth.context_processors.auth",
                                                       "django.contrib.messages.context_processors.messages",
                                                       "django.template.context_processors.request"]}}],
        STATIC_URL="/static/", ROOT_URLCONF=None,
    )
    django.setup()
    try:                                        # tables for the contrib models (in-memory database)
        from django.core.management import call_command
        call_command("migrate", run_syncdb=True, verbosity=0, interactive=False)
    except Exception:
        pass
""" % TEST_FILE
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


def django_label(rel: str) -> str | None:
    """tests/model_fields/test_promises.py -> model_fields: the whole test app, for Django's runtests.py.
    The selected file is often a small sibling of the app's main tests.py; running the app covers both
    (on the 40-bug subset the hidden tests' app is covered for 32 bugs vs 25 with single files)."""
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "tests" or not rel.endswith(".py"):
        return None
    return parts[1]


_UT_HEAD = re.compile(r"^(FAIL|ERROR): ([\w.]+) \(([\w.]+)\)", re.M)


def unittest_to_pytest(out: str) -> str:
    """Rewrite unittest (Django runtests.py) output into the pytest result lines the validator parses:
    one `FAILED <id>` line per failing test and a `N passed, M failed` summary. Django < 4.1 prints
    `FAIL: test_x (pkg.mod.Class)`, newer versions `FAIL: test_x (pkg.mod.Class.test_x)`; an import
    failure of a whole module is `ERROR: pkg.mod (unittest.loader._FailedTest)` style and keeps its name."""
    ids = []
    for kind, name, where in _UT_HEAD.findall(out):
        tid = where if where.endswith("." + name) else f"{where}.{name}"
        if where.startswith("unittest.loader."):
            tid = name
        ids.append(tid)
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    lines = ["  " + l for l in out.splitlines()]          # indented: never mistaken for result lines
    lines += [f"FAILED {t}" for t in dict.fromkeys(ids)]
    if ran:
        n = int(ran.group(1))
        skipped = sum(int(x) for x in re.findall(r"skipped=(\d+)", out))
        lines.append(f"{max(n - len(set(ids)) - skipped, 0)} passed, {len(set(ids))} failed")
    return "\n".join(lines)


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
        # SWE-bench images clone the whole repository, so branches and tags still point at commits
        # that contain the fix. Drop every ref except the checked-out HEAD: `git log --all` then shows
        # nothing after the buggy commit (objects stay, but nothing reachable names them).
        self._exec("git checkout -q --detach && git for-each-ref --format='%(refname)' | "
                   "xargs -r -n1 git update-ref -d && git reflog expire --expire=now --all")
        # The repo's own test runner is often not pytest (e.g. Django); our tests need it.
        code, out = self._exec(f"{self.ACTIVATE} && (python -c 'import pytest' 2>/dev/null || "
                               "pip install -q pytest)")
        if code != 0:
            raise RuntimeError(f"could not install pytest in {image}: {out[-500:]}")
        code, _ = self._exec("test -f django/__init__.py")
        self.django = code == 0
        if self.django:
            self._put("/tmp/autose_plugins/autose_django.py", DJANGO_PLUGIN.encode("utf-8"))
        if base_patch:
            self._put("/tmp/base.diff", base_patch.encode("utf-8"))
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

    def run_files(self, sb: Sandbox, files: list[str], timeout_s: int) -> str | None:
        """Django's own tests need its runner (settings, test apps, test database), not pytest: under
        pytest they fail at collection ("settings are not configured") before and after a patch, which
        looks like "no regressions". Run them the way SWE-bench does and report pytest-style lines."""
        labels = list(dict.fromkeys(lb for lb in map(django_label, files) if lb))
        if not self.django or not labels or self._exec("test -f tests/runtests.py")[0] != 0:
            return super().run_files(sb, files, timeout_s)
        self._sync(sb)
        cmd = (f"{self.ACTIVATE} && cd tests && PYTHONDONTWRITEBYTECODE=1 python runtests.py "
               f"--settings=test_sqlite --parallel 1 --verbosity 2 " + " ".join(shlex.quote(x) for x in labels))
        code, out = self._exec(cmd + " 2>&1", timeout_s)
        if code == 124:
            return None
        return unittest_to_pytest(out)

    def _pytest(self, sb, args, timeout_s):
        self._sync(sb)
        plug = "PYTHONPATH=/tmp/autose_plugins${PYTHONPATH:+:$PYTHONPATH} " if self.django else ""
        cmd = (f"{self.ACTIVATE} && {plug}PYTHONDONTWRITEBYTECODE=1 python -m pytest "
               + " ".join(shlex.quote(a) for a in args) + " " + " ".join(PYTEST_FLAGS)
               + (" -p autose_django" if self.django else "")
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
