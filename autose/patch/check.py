"""Check a candidate patch by running real code, never the benchmark's hidden tests.

Two checks, each run on the untouched snapshot and on the patched one (edited files are loaded
in memory over the snapshot, so nothing on disk changes):
  report  the bug report's own example code (the statements Layer 3's trace extracted): did the
          error go away, did an output change, does it now print what the report expects?
  tests   the repository's existing tests for each edited module (sympy/a/b.py ->
          sympy/a/tests/test_b.py): a test that passed before and fails after is a regression.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from autose.trace.runner import REPOS, interpreter, run_statements

REPORT_DEADLINE = 60
TEST_DEADLINE = 240
MAX_TEST_FILES = 2
WEAK_ERRORS = ("SyntaxError", "NameError", "ImportError", "ModuleNotFoundError")


def report_run(repo: str, snapshot: Path, statements: list[str], overrides: dict | None = None,
               deadline: int = REPORT_DEADLINE) -> dict:
    cfg, python = REPOS.get(repo), interpreter(repo, snapshot)
    if not cfg or not python or not statements:
        return {"status": "unavailable"}
    raw = run_statements(snapshot, statements, cfg["preamble"], [], python, deadline,
                         repo=repo, extra={"overrides": overrides or {}, "trace": False})
    return {"status": raw.get("status"), "timed_out": raw.get("timed_out", False),
            "statements": raw.get("statements", []), "stderr": raw.get("stderr", "")[-500:]}


def probe_run(repo: str, snapshot: Path, statements: list[str], overrides: dict | None = None,
              watch: list | None = None, deadline: int = REPORT_DEADLINE) -> dict:
    """The report's code under the profiler: which repository functions ran for each line (C),
    and, for the `watch`ed functions ([[file, def line, name]]), their arguments and return values
    on each line (D). Slower than report_run; used only when a patch leaves a line wrong."""
    cfg, python = REPOS.get(repo), interpreter(repo, snapshot)
    if not cfg or not python or not statements:
        return {"status": "unavailable"}
    raw = run_statements(snapshot, statements, cfg["preamble"], [], python, deadline,
                         repo=repo, extra={"overrides": overrides or {}, "trace": False, "per_statement": True,
                          "watch": watch or []})
    return {"status": raw.get("status"), "per": raw.get("per_statement_calls", {}), "trail": raw.get("trail", {}),
            "statements": raw.get("statements", [])}


def line_run(repo: str, snapshot: Path, statements: list[str], ranges: list, overrides: dict | None = None,
             deadline: int = REPORT_DEADLINE) -> dict:
    """Which lines of the given ranges ([[file, first, last]]) ran for each report statement:
    {"statements": [...], "lines": {i: {file: [line, ...]}}}. Only frames of those files are traced."""
    cfg, python = REPOS.get(repo), interpreter(repo, snapshot)
    if not cfg or not python or not statements or not ranges:
        return {"status": "unavailable", "lines": {}}
    raw = run_statements(snapshot, statements, cfg["preamble"], [], python, deadline,
                         repo=repo, extra={"overrides": overrides or {}, "trace": False, "lines": ranges})
    return {"status": raw.get("status"), "lines": raw.get("lines", {}), "statements": raw.get("statements", [])}


def failing_statements(run: dict, expected: str = "", observed: str = "") -> tuple[list[int], list[int]]:
    """(failing, working) report statements of a run on the original code: failing = raises inside the
    repository (or a non-trivial error), or shows the report's observed result and not its expected one."""
    bad, good = [], []
    for r in run.get("statements", []):
        shown = _shown(r)
        if not r["ok"]:
            if r.get("frames") or not str(r["error"]).startswith(WEAK_ERRORS):
                bad.append(r["i"])
        elif observed and _appears(shown, observed) and not _appears(shown, expected):
            bad.append(r["i"])
        elif shown:
            good.append(r["i"])
    return bad, good


def test_files_for(snapshot: Path, edited: list[str]) -> list[str]:
    """The existing tests of each edited module, by the layouts the repositories use:
    a/b/tests/test_c.py (sympy), tests/<package path>/test_c.py (seaborn: seaborn/_core/plot.py ->
    tests/_core/test_plot.py), tests/test_c.py (flask: src/flask/config.py -> tests/test_config.py);
    else a test file whose name contains the module's (common.py -> test_commonmatrix.py)."""
    snapshot = Path(snapshot)
    out = []
    for f in edited:
        p = Path(f)
        stem = p.parent.name if p.name == "__init__.py" else p.stem
        pkg = p.parent.parent if p.name == "__init__.py" else p.parent
        parts = [x for x in pkg.parts if x != "src"]
        dirs = [pkg / "tests", Path("tests", *parts[1:]), Path("tests")]
        found = []
        for d in dirs:
            exact = snapshot / d / f"test_{stem}.py"
            if exact.exists():
                found = [exact]
                break
        if not found and stem.startswith("_old"):    # seaborn/_oldcore.py is the old copy of core
            found = [snapshot / d / f"test_{stem[4:]}.py" for d in dirs if (snapshot / d / f"test_{stem[4:]}.py").exists()][:1]
        if not found:
            for d in dirs:
                found = sorted((snapshot / d).glob(f"test_*{stem.strip('_')}*.py"))[:1]
                if found:
                    break
        for c in found:
            rel = c.relative_to(snapshot).as_posix()
            if rel not in out:
                out.append(rel)
    return out[:MAX_TEST_FILES]


def tests_run(repo: str, snapshot: Path, files: list[str], overrides: dict | None = None,
              deadline: int = TEST_DEADLINE) -> dict:
    cfg, python = REPOS.get(repo), interpreter(repo, snapshot)
    if not cfg or not python or not files:
        return {"status": "unavailable", "tests": {}}
    raw = run_statements(snapshot, [], [], [], python, deadline,
                         repo=repo, extra={"overrides": overrides or {}, "trace": False, "tests": files})
    return {"status": raw.get("status"), "timed_out": raw.get("timed_out", False), "tests": raw.get("tests", {}),
            "stderr": raw.get("stderr", "")[-500:]}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _shown(rec: dict) -> str:
    return (rec.get("out") or "").strip() or (rec.get("value") or "")


def _forms(shown: str) -> list[str]:
    """A result as the report might write it: as shown, and a string's text without its quotes."""
    out = [shown]
    try:
        v = ast.literal_eval(shown)
        if isinstance(v, str):
            out.append(v)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    return out


def _appears(shown: str, text: str) -> bool:
    """Does this result appear in the report text? Spaces are ignored; a very short result (a
    number, True) must stand on its own there, not inside another word or number."""
    for f in _forms(shown):
        n = _norm(f)
        if not n or not text:
            continue
        if len(n) >= 3 and n in _norm(text):
            return True
        if len(n) < 3 and re.search(rf"(?<![\w.]){re.escape(f.strip())}(?![\w.])", text):
            return True
    return False


def compare(before: dict, after: dict, expected: str = "", observed: str = "",
            tests_before: dict | None = None, tests_after: dict | None = None) -> dict:
    """What the patch changed. All counts are over the report's statements / the module's tests."""
    b = {r["i"]: r for r in before.get("statements", [])}
    a = {r["i"]: r for r in after.get("statements", [])}
    rows, fixed, broke, changed, exp_hits, still_obs, gone = [], 0, 0, 0, 0, 0, 0
    for i in sorted(b):
        x, y = b[i], a.get(i)
        if y is None:
            continue
        sx, sy = _shown(x), _shown(y)
        # an error raised inside the repository is the bug, whatever its type (a NameError in
        # library code is a typo there); one raised by the report's own code (no repository
        # frames) is usually just an incomplete example
        was_bug = not x["ok"] and (bool(x.get("frames")) or not str(x["error"]).startswith(WEAK_ERRORS))
        row = {"i": i, "before": x["error"] or sx, "after": y["error"] or sy}
        if not y["ok"] and y.get("frames"):
            row["frames"] = [f[:2] for f in y["frames"][-6:]]  # [file, line], innermost last
        if was_bug and y["ok"]:
            fixed += 1
            row["fixed"] = True
        if x["ok"] and not y["ok"]:
            broke += 1
            row["broke"] = True
        if (x["ok"], x["error"], sx) != (y["ok"], y["error"], sy):
            changed += 1
            row["changed"] = True
            if y["ok"] and _appears(sy, expected) and not _appears(sx, expected):
                exp_hits += 1
                row["expected"] = True
            elif x["ok"] and y["ok"] and _appears(sx, observed) and not _appears(sx, expected):
                gone += 1  # it showed the report's observed (wrong) result; now it shows something else
                row["observed_gone"] = True
        if y["ok"] and _appears(sy, observed) and not _appears(sy, expected):
            still_obs += 1
            row["still_observed"] = True
        if not y["ok"] and was_bug and _norm(y["error"]) == _norm(x["error"]):
            row["same_error"] = True
        # B: a line is still wrong if it still raises the bug's error, newly fails, or (no
        # exception) still shows the report's observed result, or showed it before and does not
        # show the expected one now: the expected value acts as the line's assertion
        if row.get("same_error") or row.get("broke") or row.get("still_observed") or \
                (y["ok"] and observed and _appears(sx, observed) and not row.get("expected")
                 and not _appears(sy, expected)):
            row["wrong"] = True
        rows.append(row)
    tb, ta = (tests_before or {}).get("tests", {}), (tests_after or {}).get("tests", {})
    # a test missing after the patch only counts when its module no longer imports (a time-out
    # cuts the run short and is not the patch's fault)
    broken_imports = {t.split("::")[0] for t, r in ta.items() if t.endswith("::<import>") and r.startswith("fail")
                      and tb.get(t, "pass") == "pass"}
    regressions = sorted(t for t, r in tb.items() if r == "pass" and
                         (ta.get(t, "").startswith("fail") or (t not in ta and t.split("::")[0] in broken_imports)))
    newly_pass = sorted(t for t, r in tb.items() if r.startswith("fail") and ta.get(t) == "pass")
    return {"fixed": fixed, "broke": broke, "changed": changed, "expected": exp_hits, "observed_gone": gone,
            "still_observed": still_obs,
            "same_error": sum(1 for r in rows if r.get("same_error")), "rows": rows,
            "wrong": [r["i"] for r in rows if r.get("wrong")],
            "tests_run": len(ta), "regressions": regressions, "newly_pass": newly_pass,
            "regression_errors": {t: ta.get(t, "")[:200] for t in regressions[:5]}}


def score(c: dict | None) -> tuple:
    """Higher is better: no regressions, nothing newly broken, then evidence the bug is fixed."""
    if not c:
        return (0,)
    return (1, -len(c["regressions"]), -c["broke"], c["fixed"] + c["expected"] + c["observed_gone"], -c["same_error"],
            -c["still_observed"], min(c["changed"], 1))


def good_enough(c: dict | None) -> bool:
    # the observed wrong result going away is evidence for ranking attempts (score) but not enough
    # to stop: any change does that
    return bool(c) and not c["regressions"] and not c["broke"] and (c["fixed"] or c["expected"]) \
        and not c["same_error"] and not c["still_observed"]


def feedback(c: dict, statements: list[str], expected: str, source=None, files=()) -> str:
    """source(file) -> the (patched) file's text, to quote the line that raised: the innermost
    line inside the edited `files` (where the model can act), else the innermost line overall."""
    """Tell the model what its patch did, in a few lines."""
    lines = []
    for r in c["rows"]:
        if not (r.get("changed") or r.get("same_error") or r.get("still_observed") or r.get("wrong")):
            continue
        code = statements[r["i"]].strip().splitlines()[-1][:120] if r["i"] < len(statements) else "?"
        tag = ("now fixed" if r.get("fixed") else "now newly FAILS" if r.get("broke")
               else "still raises the same error" if r.get("same_error")
               else "still shows the wrong (observed) result" if r.get("still_observed")
               else "matches the expected result" if r.get("expected")
               else "changed, but still not the expected result" if r.get("wrong")
               else "no longer shows the observed (wrong) result" if r.get("observed_gone") else "changed")
        lines.append(f"- `{code}`: {tag}. Before: {str(r['before'])[:160]!r}; after: {str(r['after'])[:160]!r}")
        if r.get("frames") and source:
            own = [fr for fr in r["frames"] if fr[0] in files]
            f, ln = (own or r["frames"])[-1]
            try:
                src = source(f).splitlines()[ln - 1].strip()
                where = "in the code you edit, at" if own else "at"
                lines.append(f"  raised {where} {f} line {ln}: `{src[:160]}`")
            except (OSError, IndexError, KeyError, UnicodeDecodeError):
                pass
        if len(lines) >= 6:
            break
    if not lines:
        lines.append("- the report's example code behaves exactly as before your change")
    if expected:
        lines.append(f"The report expects: {expected[:300]}")
    if c["regressions"]:
        lines.append(f"{len(c['regressions'])} existing test(s) that passed before now fail, e.g.:")
        lines += [f"- {t.split('::')[-1]}: {e}" for t, e in list(c["regression_errors"].items())[:3]]
    elif c["tests_run"]:
        lines.append(f"The {c['tests_run']} existing tests of the edited module still pass.")
    return "\n".join(lines)
