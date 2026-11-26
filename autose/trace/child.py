"""Run report statements under a call profiler. Executed as a separate process by the trace runner,
inside the snapshot's own interpreter (Python 3.9 for SWE-bench sympy), so: standard library only,
no autose imports, Python 3.9 syntax.

  python -I -X utf8 child.py job.json
job: {snapshot, preamble: [code], baseline: [code], statements: [code], deadline: seconds, out: path}
out: {calls: [[file, firstline, name, count]], baseline_calls: [...], statements: [{i, ok, error, frames}],
      timed_out, stdout}
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


def prof(frame, event, arg):
    if event != "call":
        return
    d = current[0]
    if d is None:
        return
    co = frame.f_code
    r = rel(co.co_filename)
    if r:
        k = (r, co.co_firstlineno, co.co_name)
        d[k] = d.get(k, 0) + 1


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
        res = {"calls": rows(calls), "baseline_calls": rows(baseline), "statements": list(results),
               "timed_out": state["timed_out"], "stdout": out_buf.getvalue()[:3000]}
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
    rec = {"i": i, "ok": True, "error": None, "frames": []}
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
                    repr(value)
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
    return rec


sys.setprofile(prof)
threading.setprofile(prof)
for i, code in enumerate(job["preamble"]):
    run(code, -1 - i, None)
for i, code in enumerate(job["baseline"]):
    run(code, -100 - i, baseline)
for i, code in enumerate(job["statements"]):
    results.append(run(code, i, calls))
dump()
os._exit(0)
