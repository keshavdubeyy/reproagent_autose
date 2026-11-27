"""Run report statements under a call profiler. Executed as a separate process by the trace runner,
inside the snapshot's own interpreter (Python 3.9 for SWE-bench sympy), so: standard library only,
no autose imports, Python 3.9 syntax.

  python -I -X utf8 child.py job.json
job: {snapshot, preamble: [code], baseline: [code], statements: [code], deadline: seconds, out: path,
      overrides: {relative path: source} (optional: edited files, loaded instead of the snapshot's),
      trace: bool (default true), tests: [relative test file paths] (optional: run those instead),
      per_statement: bool (calls recorded per statement), watch: [[file, def line, name]] (record the
      arguments and return values of these functions, per statement), lines: [[file, first, last]]
      (record which of these lines ran, per statement)}
out: {calls: [[file, firstline, name, count]], baseline_calls: [...],
      statements: [{i, ok, error, frames, value, out}], timed_out, stdout,
      per_statement_calls: {i: [[file, firstline, name, count]]}, trail: {i: [[file, firstline, name, args, returned]]}}
  or, with tests: {tests: {file::name: "pass" | "skip" | "xfail" | "fail: <error>"}, timed_out}
Every statement runs on its own (one failing line does not stop the rest); imports are not traced;
the network is blocked; the working directory is a scratch folder.
"""
import ast
import io
import json
import os
import socket
import sys
import threading
import traceback

job = json.load(open(sys.argv[1], encoding="utf-8"))
SNAP = os.path.realpath(job["snapshot"])
PREFIX = os.path.normcase(SNAP + os.sep)
sys.path.insert(0, SNAP)
for _sub in reversed(job.get("path") or []):   # src layouts (flask): the package lives below the root
    sys.path.insert(0, os.path.join(SNAP, *_sub.split("/")))
DISPLAY = bool(job.get("display"))
OVERRIDES = {os.path.normcase(os.path.join(SNAP, *k.split("/"))): v for k, v in (job.get("overrides") or {}).items()}

if OVERRIDES:
    import importlib.abc
    import importlib.machinery
    import importlib.util

    class _Loader(importlib.abc.SourceLoader):
        """Loads an edited file's source from the job instead of the disk; keeps the real path so
        tracebacks and the profiler see the snapshot's file and line numbers. No bytecode cache."""
        def __init__(self, path):
            self.path = path

        def get_filename(self, fullname):
            return self.path

        def get_data(self, path):
            key = os.path.normcase(path)
            if key in OVERRIDES:
                return OVERRIDES[key].encode("utf-8")
            with open(path, "rb") as fh:
                return fh.read()

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or not spec.origin or os.path.normcase(spec.origin) not in OVERRIDES:
                return spec
            return importlib.util.spec_from_file_location(fullname, spec.origin, loader=_Loader(spec.origin),
                                                          submodule_search_locations=spec.submodule_search_locations)

    sys.meta_path.insert(0, _Finder())


def _no_network(*a, **k):
    raise OSError("network disabled in the AUTOSE trace sandbox")


socket.socket.connect = _no_network
socket.create_connection = _no_network
socket.getaddrinfo = _no_network

rel_cache = {}
calls = {}
baseline = {}
current = [None]  # the dict being filled, or None for untraced


def rel(path):
    r = rel_cache.get(path)
    if r is None:
        p = os.path.normcase(os.path.realpath(path))
        r = p[len(PREFIX):].replace(os.sep, "/") if p.startswith(PREFIX) else ""
        rel_cache[path] = r
    return r


WATCH = {}   # (file, name) -> [def lines] of functions whose calls and return values are recorded
for _f, _line, _name in job.get("watch") or []:
    WATCH.setdefault((_f, _name), []).append(_line)
PER = bool(job.get("per_statement"))
LINES = {}   # file -> [(first, last)] line ranges whose executed lines are recorded, per statement
for _f, _a, _b in job.get("lines") or []:
    LINES.setdefault(_f, []).append((_a, _b))
line_hits = {}   # statement -> {file: set(lines)}


def _local_tracer(frame, event, arg):
    if event == "line":
        i = cur_i[0]
        if i is not None:
            r = rel(frame.f_code.co_filename)
            n = frame.f_lineno
            if any(a <= n <= b for a, b in LINES.get(r, ())):
                line_hits.setdefault(i, {}).setdefault(r, set()).add(n)
    return _local_tracer


def _global_tracer(frame, event, arg):
    # only frames of the watched files are line-traced; everything else runs untraced
    if event == "call" and rel(frame.f_code.co_filename) in LINES:
        return _local_tracer
    return None
per_calls = {}      # statement -> {(file, firstline, name): count}
trail = {}          # statement -> [[file, firstline, name, args, returned]] for watched functions
open_calls = {}     # id(frame) -> its trail entry, until it returns
cur_i = [None]      # the statement running now
busy = [False]      # True while taking a repr (its own calls are not recorded)
MAX_TRAIL, MAX_PER_FUNCTION = 60, 3


def _short(v):
    busy[0] = True
    try:
        s = repr(v)
    except BaseException as e:  # noqa: B902 - a broken __repr__ must not break the run
        s = "<repr failed: %s>" % type(e).__name__
    finally:
        busy[0] = False
    return s if len(s) <= 150 else s[:147] + "..."


def _args(frame):
    co = frame.f_code
    names = co.co_varnames[:co.co_argcount + co.co_kwonlyargcount]
    out = []
    for n in names[:4]:
        v = frame.f_locals.get(n)
        out.append("%s=<%s>" % (n, type(v).__name__) if n in ("self", "cls") else "%s=%s" % (n, _short(v)))
    return ", ".join(out)


def _watched(r, co):
    lines = WATCH.get((r, co.co_name))
    return bool(lines) and any(0 <= l - co.co_firstlineno <= 30 for l in lines)


def prof(frame, event, arg):
    if busy[0]:
        return
    if event == "call":
        co = frame.f_code
        r = rel(co.co_filename)
        if not r:
            return
        d = current[0]
        if d is not None:
            k = (r, co.co_firstlineno, co.co_name)
            d[k] = d.get(k, 0) + 1
        i = cur_i[0]
        if WATCH and i is not None and _watched(r, co):
            lst = trail.setdefault(i, [])
            if len(lst) < MAX_TRAIL and sum(1 for e in lst if e[0] == r and e[2] == co.co_name) < MAX_PER_FUNCTION:
                ent = [r, co.co_firstlineno, co.co_name, _args(frame), None]
                lst.append(ent)
                open_calls[id(frame)] = ent
    elif event == "return" and open_calls:
        ent = open_calls.pop(id(frame), None)
        if ent is not None:
            ent[4] = _short(arg)


out_buf = io.StringIO()
results = []
state = {"timed_out": False, "done": False}
lock = threading.Lock()


def dump():
    with lock:
        if state["done"]:
            return
        state["done"] = True
        current[0] = None
        sys.setprofile(None)

        def rows(d):
            for _ in range(5):
                try:
                    return [[f, l, n, c] for (f, l, n), c in list(d.items())]
                except RuntimeError:
                    continue
            return []
        if job.get("tests"):
            res = {"tests": dict(results_tests), "timed_out": state["timed_out"]}
        else:
            res = {"calls": rows(calls), "baseline_calls": rows(baseline), "statements": list(results),
                   "timed_out": state["timed_out"], "stdout": out_buf.getvalue()[:3000]}
            if PER:
                res["per_statement_calls"] = {str(i): rows(d) for i, d in list(per_calls.items())}
            if WATCH:
                res["trail"] = {str(i): [list(e) for e in lst] for i, lst in list(trail.items())}
            if LINES:
                res["lines"] = {str(i): {f: sorted(v) for f, v in list(d.items())} for i, d in list(line_hits.items())}
        with open(job["out"], "w", encoding="utf-8") as fh:
            json.dump(res, fh)


def on_deadline():
    state["timed_out"] = True
    dump()
    os._exit(0)


timer = threading.Timer(job["deadline"], on_deadline)
timer.daemon = True
timer.start()

g = {"__name__": "__main__"}
real_stdout = sys.stdout


def run(code, i, target):
    """Run one top-level statement; an expression's repr is taken too (as a doctest would show it)."""
    rec = {"i": i, "ok": True, "error": None, "frames": [], "value": None, "out": ""}
    start = out_buf.tell()
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        rec.update(ok=False, error="SyntaxError: %s" % e.msg)
        return rec
    for node in tree.body:
        is_import = isinstance(node, (ast.Import, ast.ImportFrom))
        sys.stdout = out_buf
        try:
            current[0] = None if is_import else target
            if isinstance(node, ast.Expr):
                value = eval(compile(ast.Expression(node.value), "<report>", "eval"), g)
                if value is not None:
                    rec["value"] = repr(value)[:400]
                    if DISPLAY and hasattr(value, "_repr_png_"):
                        value._repr_png_()   # as a notebook shows it: seaborn.objects draws only now
            else:
                exec(compile(ast.Module([node], []), "<report>", "exec"), g)
        except BaseException as e:  # noqa: B902 - report code may raise anything, even SystemExit
            current[0] = None
            tb = traceback.extract_tb(e.__traceback__)
            rec.update(ok=False, error=("%s: %s" % (type(e).__name__, e))[:300],
                       frames=[[rel(f.filename), f.lineno, f.name] for f in tb if rel(f.filename)][-8:])
            break
        finally:
            current[0] = None
            sys.stdout = real_stdout
    rec["out"] = out_buf.getvalue()[start:start + 400]
    return rec


SKIP_ERRORS = ("Skipped", "Skip", "SkipTest", "XFail", "XPass")


def run_tests(files):
    """Every module-level test_* function of the given test files, one by one, without pytest
    (sympy's own pytest stand-in supplies raises/XFAIL/slow/skip). Slow tests are skipped."""
    import importlib
    res = results_tests
    for f in files:
        mod_name = f[:-3].replace("/", ".")
        try:
            mod = importlib.import_module(mod_name)
        except BaseException as e:  # noqa: B902
            res[f + "::<import>"] = ("fail: %s: %s" % (type(e).__name__, e))[:300]
            continue
        for name in sorted(n for n in vars(mod) if n.startswith("test_")):
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            if getattr(fn, "_slow", False) or getattr(fn, "_skip", False):
                res[f + "::" + name] = "skip"
                continue
            try:
                fn()
                res[f + "::" + name] = "pass"
            except BaseException as e:  # noqa: B902
                kind = type(e).__name__
                if kind in ("XFail",):
                    res[f + "::" + name] = "xfail"
                elif kind in SKIP_ERRORS:
                    res[f + "::" + name] = "skip"
                else:
                    res[f + "::" + name] = ("fail: %s: %s" % (kind, e))[:300]


results_tests = {}


def run_pytest(files):
    """The test files under pytest (fixtures, test classes, the snapshot's conftest.py), in this
    process so the edited files are imported through the same overrides."""
    import pytest
    res = results_tests

    class Collect:
        def pytest_runtest_logreport(self, report):
            key = report.nodeid
            if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
                if getattr(report, "wasxfail", None) is not None:
                    res[key] = "xfail"
                elif report.outcome == "passed":
                    res[key] = "pass"
                elif report.outcome == "skipped":
                    res[key] = "skip"
                else:
                    msg = str(getattr(report, "longreprtext", "") or report.longrepr).strip().splitlines()
                    res[key] = ("fail: " + (msg[-1] if msg else "failed"))[:300]

        def pytest_collectreport(self, report):
            if report.outcome == "failed":
                res[report.nodeid + "::<import>"] = ("fail: " + str(report.longrepr).strip().splitlines()[-1])[:300]

    args = ["-q", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", SNAP, "-W", "ignore"]
    pytest.main(args + [os.path.join(SNAP, *f.split("/")) for f in files], plugins=[Collect()])


if job.get("tests"):
    sys.stdout = out_buf
    (run_pytest if job.get("test_runner") == "pytest" else run_tests)(job["tests"])
    sys.stdout = real_stdout
    dump()
    os._exit(0)
if LINES:
    sys.settrace(_global_tracer)
    threading.settrace(_global_tracer)
if job.get("trace", True) or PER or WATCH:
    sys.setprofile(prof)
    threading.setprofile(prof)
for i, code in enumerate(job["preamble"]):
    run(code, -1 - i, None)
for i, code in enumerate(job["baseline"]):
    run(code, -100 - i, baseline)
for i, code in enumerate(job["statements"]):
    cur_i[0] = i
    results.append(run(code, i, per_calls.setdefault(i, {}) if PER else calls))
    cur_i[0] = None
dump()
os._exit(0)
