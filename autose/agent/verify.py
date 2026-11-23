"""Bounded context 3: Verification.

* Sandbox / Executor     - Function/Tool : see workspace.py (local subprocess or SWE-bench Docker image).
* ResultAnalyzer         - Rules + AI    : classifies the run (invalid test / passed / wrong failure /
                                           reproduction) and produces feedback for the next attempt.
* FailToPassChecker      - Function/Tool : re-runs the final test on the fixed version (evaluation only).
* PatchValidator         - Function/Tool : applies a proposed patch in a sandbox, re-runs the reproduction
                                           test (must now pass) and the related existing tests (no
                                           regressions), and turns the result into feedback.
* run_python_check       - Function/Tool : runs a hidden oracle snippet on a patched tree (evaluation only).
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from autose.intel.index import module_name

from .llm import LLM, parse_json
from .models import Analysis, BugReport, BugSpec, ExecutionResult, Patch, PatchCheck, ReproTest
from .workspace import TEST_FILE, Executor, LocalExecutor, Sandbox, TestExecutor  # noqa: F401

_EXC = re.compile(r"^E\s+(?:[\w.]+\.)?([A-Z]\w*(?:Error|Exception|Exit|Interrupt))\b", re.M)


def parse_pytest(out: str, code: int, dur: float) -> ExecutionResult:
    if code == 0:
        status = "passed"
    elif "ERROR collecting" in out or "error during collection" in out or code in (2, 3, 4):
        status = "collection_error"
    elif re.search(r"\d+ failed", out):
        status = "failed"
    elif re.search(r"\d+ error", out):
        status = "error"
    elif code == 5:
        status = "collection_error"   # no tests collected
    else:
        status = "error"
    exc = None
    m = _EXC.search(out)
    if m:
        exc = m.group(1)
    elif re.search(r"^E\s+assert\b", out, re.M):
        exc = "AssertionError"
    fl = None
    m2 = re.search(r"^E\s+(.+)$", out, re.M)
    if m2:
        fl = m2.group(1).strip()[:300]
    summary = next((l for l in reversed(out.splitlines()) if re.search(r"\d+ (?:passed|failed|errors?)\b", l)), "")
    nf = sum(int(x) for x in re.findall(r"(\d+) (?:failed|errors?)\b", summary)) if summary else None
    npass = re.search(r"(\d+) passed", summary)
    failing = sorted(set(re.findall(r"^(?:FAILED|ERROR) [^\s:]+::(\w+)", out, re.M)))
    lines = [l for l in out.splitlines() if "Warning:" not in l]
    if len(lines) > 60:   # keep the FAILURES section (where the evidence is) rather than head/tail
        start = next((i for i, l in enumerate(lines) if "= FAILURES =" in l or "= ERRORS =" in l), 0)
        lines = lines[start:start + 60]
    out = "\n".join(lines)
    return ExecutionResult(status, code, round(dur, 2), out, exc, fl, nf,
                           int(npass.group(1)) if npass else 0, failing)


_FRAME = re.compile(r"^([^\s:]+\.py):\d+: in ", re.M)


def raised_in_test(out: str) -> bool:
    """True when the deepest traceback frame before the first 'E ' line is the test file."""
    head = out.split("\nE ", 1)[0]
    frames = _FRAME.findall(head)
    return bool(frames) and frames[-1].replace("\\", "/").endswith(TEST_FILE)


INVALID_EXC = {"ImportError", "ModuleNotFoundError", "SyntaxError", "NameError", "IndentationError",
               "FixtureLookupError"}
# The test failed while setting up the program (CLI exit, app/settings not configured), before it reached
# the code the report is about. Replayed on night 1: rejects 3 of 20 invalid reproductions, 0 of 12 valid.
SETUP_EXC = {"SystemExit", "UsageError", "NoAppException", "ImproperlyConfigured", "AppRegistryNotReady"}
API_MISUSE = re.compile(r"positional argument|unexpected keyword|takes \d+ positional|has no attribute|"
                        r"not callable|cannot import name|missing \d+ required", re.I)

JUDGE_SYSTEM = """You check whether a FAILING test is evidence of a reported bug.
A test that fails because the project code shows the wrong behaviour described in the report IS evidence.
It is NOT evidence if the test itself is wrong: inputs that differ from the report, an expected value that
contradicts the report, or an assertion on a property the report does not mention.
Return ONLY JSON, keys in this order:
  {"actual": "<what the code produced, from the E line>", "reason": "<one sentence>", "reproduces": true|false}"""


_ASSERT = re.compile(r"^E\s+(?:AssertionError:\s+)?assert\s+(.+?)\s+(?:==|!=|is not|is|in|not in|<=|>=|<|>)\s+(.+)$", re.M)
_TOK = re.compile(r"-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|None|True|False")


def _norm_tok(t: str) -> str:
    t = t.strip("'\"")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        t = t.rstrip("0").rstrip(".") if "." in t else t
    return t


def actual_value(out: str) -> str | None:
    """Left-hand side of the first failing `assert a == b` (pytest prints the actual value there)."""
    m = _ASSERT.search(out)
    if not m:
        return None
    lhs = m.group(1)
    lhs = re.sub(r"Decimal\('([^']*)'\)", r"\1", lhs)
    toks = _TOK.findall(lhs)
    return _norm_tok(toks[0]) if len(toks) == 1 else None


def evidence_match(report: BugReport, spec: BugSpec, res: ExecutionResult) -> tuple[bool | None, str]:
    """Deterministic check of the failure against the report's OBSERVED behaviour.

    Returns (True, why) for a match, (False, why) for a clear mismatch, (None, '') when undecidable.
    """
    text = f"{report.title}\n{report.body}"
    observed = spec.observed or ""
    exc = res.exception_type
    if exc and exc != "AssertionError":
        if _exception_in_report(exc, report, spec):
            return True, f"project raised {exc}, as the report describes"
        if spec.failure_kind == "exception":
            return None, ""
    val = actual_value(res.output)
    if val is None or val in ("", "0", "True", "False"):
        return None, ""
    obs_tokens = {_norm_tok(t) for t in _TOK.findall(re.sub(r"(?<=\d),(?=\d)", "", observed))}
    rep_tokens = {_norm_tok(t) for t in _TOK.findall(re.sub(r"(?<=\d),(?=\d)", "", text))}
    if val in obs_tokens or (val in rep_tokens and val not in spec.expected):
        return True, f"actual value {val} is the wrong value the report observed"
    numeric_obs = {t for t in obs_tokens if re.fullmatch(r"-?\d+(?:\.\d+)?", t)}
    if numeric_obs and re.fullmatch(r"-?\d+(?:\.\d+)?", val):
        return False, (f"the report observed {', '.join(sorted(numeric_obs))} but your test produced {val}, "
                       "so your inputs do not match the report's scenario")
    return None, ""


def misuse_in_report(err: str, report: BugReport) -> bool:
    """An argument error is the bug only when the report is about that argument or that error text."""
    text = f"{report.title}\n{report.body}"
    names = re.findall(r"'([A-Za-z_]\w*)'", err)
    # code-like mentions only (`using=`, `using`, 'using', using(...)): plain words like "using" are prose
    return any(re.search(rf"\b{re.escape(n)}\s*=|[`'\"]{re.escape(n)}[`'\"]|\b{re.escape(n)}\(", text)
               for n in names if n != "self") \
        or _norm_msg(err.split(":", 1)[-1]) in _norm_msg(text)


def _exception_in_report(exc: str, report: BugReport, spec: BugSpec) -> bool:
    """The report names this exception: `ValueError`, or 'value error' / 'raises a ValueError'.
    A bare stem ('value', 'type', 'key') is far too common in prose to count."""
    text = f"{report.title}\n{report.body}\n{spec.observed}".lower()
    stem = re.sub(r"(error|exception)$", "", exc.lower())
    return exc.lower() in text or bool(stem and re.search(rf"\b{re.escape(stem)}[\s_-]*(error|exception)\b", text))


_RAISES_REGEX = {"assertRaisesRegex": "assertRaises", "assertRaisesRegexp": "assertRaises",
                 "assertWarnsRegex": "assertWarns"}


def relax_invented_messages(code: str, report: BugReport) -> tuple[str, list[str]]:
    """Drop exact error-message checks the report does not support.

    A small model often invents the wording of the expected error (`pytest.raises(ValueError,
    match="Names must not contain dots")`). The real fix raises the right exception with other words, so
    the test fails on the fixed version too: an invalid reproduction. Unless the report quotes that
    wording or is about the message itself, keep the exception type and drop the wording.
    Returns (new code, removed patterns)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    removed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ("raises", "warns"):
            for kw in list(node.keywords):
                if kw.arg == "match" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) \
                        and not message_is_the_bug(report, kw.value.value):
                    node.keywords.remove(kw)
                    removed.append(kw.value.value)
        elif name in _RAISES_REGEX and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and isinstance(node.args[1].value, str) and not message_is_the_bug(report, node.args[1].value):
            removed.append(node.args.pop(1).value)
            if isinstance(f, ast.Attribute):
                f.attr = _RAISES_REGEX[name]
            else:
                f.id = _RAISES_REGEX[name]
    if not removed:
        return code, []
    return ast.unparse(tree) + "\n", removed


class ResultAnalyzer:
    def __init__(self, llm: LLM | None, use_judge: bool = True):
        self.llm = llm
        self.use_judge = use_judge and llm is not None
        self.explorer = None   # set by the agent to enable symbol-aware feedback (mode full_v2)

    def analyze(self, report: BugReport, spec: BugSpec, test: ReproTest,
                res: ExecutionResult) -> Analysis:
        a = self._analyze(report, spec, test, res)
        if self.explorer is not None and a.category == "invalid_test" and res.failure_line:
            hint = self.explorer.symbol_hint(res.failure_line, test.code)
            if hint:
                a.feedback += " " + hint
        return a

    def _analyze(self, report: BugReport, spec: BugSpec, test: ReproTest,
                 res: ExecutionResult) -> Analysis:
        err = res.failure_line or ""
        if res.status == "timeout":
            return Analysis("invalid_test", False, "The test hung / timed out. Avoid network, sleeps, "
                            "infinite loops and interactive input.")
        if res.status == "passed":
            return Analysis("test_passed", False,
                            "The test PASSED, so it did not trigger the bug. Use the exact example input "
                            "from the report and assert the EXPECTED value stated in the report.")
        if res.status in ("collection_error", "error") or res.exception_type in INVALID_EXC:
            return Analysis("invalid_test", False,
                            f"The test could not run because of a mistake in the test itself: {err}. "
                            "Fix imports/names using the module paths and examples shown.")
        setup = res.exception_type in SETUP_EXC or any(re.search(rf"\b{e}\b", err) for e in SETUP_EXC)
        if setup and not any(_exception_in_report(e, report, spec) for e in SETUP_EXC if e in f"{res.exception_type} {err}"):
            return Analysis("invalid_test", False,
                            f"The test failed while setting up, before reaching the reported behaviour: {err}. "
                            "Call the affected function directly instead of a command line or app runner, and "
                            "build only the objects the report's example needs.", f"[guard] setup failure: {err[:160]}")
        if res.exception_type == "TypeError" and API_MISUSE.search(err) and not misuse_in_report(err, report):
            return Analysis("invalid_test", False,
                            f"The test calls the project with arguments it does not accept: {err}. Check the "
                            "function signature and how existing tests call it.", f"[guard] API misuse: {err[:160]}")
        if res.exception_type not in (None, "AssertionError") and \
                raised_in_test(res.output):
            return Analysis("invalid_test", False,
                            f"The exception was raised by the test code itself, not by the project: {err}. "
                            "Check the function signature and how existing tests call it.")
        # Candidate reproduction: test failed with an assertion or with an exception raised by project code.
        # Step 1: deterministic evidence matching against the report's observed behaviour.
        match, why = evidence_match(report, spec, res)
        if match is True:
            return Analysis("reproduced", True, "", f"[evidence-match] {why}")
        # Guard: the report describes a wrong RESULT, but the project raised an exception the report never
        # mentions. The test most likely fed input the function rejects (wrong format / type), so it
        # fails for a reason unrelated to the bug and would fail on the fixed version too.
        exc = res.exception_type
        if spec.failure_kind == "wrong_value" and exc not in (None, "AssertionError") \
                and not _exception_in_report(exc, report, spec):
            why = f"the project raised {exc} ({err[:160]}) although the report describes a wrong result, not an error"
            return Analysis("wrong_failure", False,
                            f"The test failed, but {why}. Your input is probably not valid for this function "
                            "(wrong format or type). Write the input exactly like the report's example or like "
                            f"the existing tests, and assert: {spec.expected}",
                            f"[guard] {why}")
        if match is False:
            return Analysis("wrong_failure", False, f"The test failed, but {why}. "
                            f"Build exactly the report's scenario and assert: {spec.expected}",
                            f"[evidence-match] {why}")
        # Step 2: undecidable deterministically -> SLM judge (or accept when the judge is disabled).
        if not self.use_judge:
            return Analysis("reproduced", True, "")
        user = (f"Bug report:\n{report.title}\n{report.body}\n\nExpected: {spec.expected}\n"
                f"Observed: {spec.observed}\n\nTest:\n```python\n{test.code}```\n\n"
                f"Failure output:\n```\n{res.output[-2500:]}\n```")
        d = parse_json(self.llm.chat(JUDGE_SYSTEM, user, json_mode=True, temperature=0.0))
        reason = f"[slm-judge] {d.get('reason', '')} (actual: {d.get('actual', '?')})"
        if d.get("reproduces") is False:
            hint = (f" The report's scenario is: {'; '.join(spec.example_inputs)}."
                    if spec.example_inputs else "")
            return Analysis("wrong_failure", False,
                            f"The test failed, but not because of the reported bug: {d.get('reason', '')}.{hint} "
                            f"Build exactly that scenario and assert: {spec.expected}", reason)
        return Analysis("reproduced", True, "", reason)


class FailToPassChecker:
    """Validity check used for evaluation: the test must fail on buggy and pass on fixed."""

    def __init__(self, executor: Executor):
        self.executor = executor   # for SWE-bench: a DockerExecutor with the gold patch as base_patch

    def check(self, fixed_repo: Path, test: ReproTest, label: str) -> tuple[bool, str]:
        sb = self.executor.sandbox(fixed_repo, label + "_fixed")
        try:
            res = self.executor.run(sb, test)
            return res.status == "passed", res.output
        finally:
            sb.cleanup()

    def check_with_meaning(self, fixed_repo: Path, test: ReproTest, label: str, report: BugReport,
                           judge: "MessageJudge | None") -> tuple[bool, bool, str, list]:
        """(strict, by_meaning, output, message_checks).

        strict     = the test passes on the fixed version as written (SWE-bench style).
        by_meaning = strict, OR every failure on the fixed version is only an error-message WORDING
                     mismatch (right exception type, `pytest.raises(..., match=...)` failed) and the two
                     messages mean the same thing -- unless the report makes the message itself the bug.
        """
        sb = self.executor.sandbox(fixed_repo, label + "_fixed")
        try:
            res = self.executor.run(sb, test, stop_first=False)   # see every failing test function
        finally:
            sb.cleanup()
        if res.status == "passed":
            return True, True, res.output, []
        mismatches = message_mismatches(res.output)
        if res.status != "failed" or not mismatches or len(mismatches) != (res.n_failed or 0):
            return False, False, res.output, []      # some failure is not just wording
        checks = []
        for expected, actual in mismatches:
            if message_is_the_bug(report, expected):
                checks.append({"expected": expected, "actual": actual, "same": False, "by": "report-rule",
                               "why": "the bug report is about the message itself, so the wording must match"})
            elif _norm_msg(expected) in _norm_msg(actual) or _norm_msg(actual) in _norm_msg(expected):
                checks.append({"expected": expected, "actual": actual, "same": True, "by": "text",
                               "why": "one message contains the other"})
            elif judge is not None:
                same, why = judge.same_meaning(expected, actual)
                checks.append({"expected": expected, "actual": actual, "same": same, "by": "slm", "why": why})
            else:
                checks.append({"expected": expected, "actual": actual, "same": False, "by": "none",
                               "why": "no semantic judge available"})
        return False, all(c["same"] for c in checks), res.output, checks


# ------------------------------------------------------------------ error-message meaning (evaluation)
_MISMATCH = re.compile(r"Expected regex: (['\"])(.*?)\1\s*\n\s*E?\s*Actual message: (['\"])(.*?)\3")
_MSG_IS_BUG = re.compile(r"\b(error )?message\b|\bwording\b|\bshould say\b|\b(it|error|exception) says\b|"
                         r"\bmisleading\b|\bunclear error\b", re.I)


def message_mismatches(output: str) -> list[tuple[str, str]]:
    """(expected regex, actual message) for each `pytest.raises(match=...)` that saw the right exception
    type but different wording ("Regex pattern did not match")."""
    return [(m.group(2), m.group(4)) for m in _MISMATCH.finditer(output)]


def _norm_msg(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\\(.)", r"\1", s.lower())).strip()


def message_is_the_bug(report: BugReport, expected: str) -> bool:
    """Case 1: the report is about the message itself (talks about the message, or quotes the expected
    wording), so a different wording is a real failure, not a paraphrase."""
    text = f"{report.title}\n{report.body}"
    quoted = re.findall(r"['\"`‘“]([^'\"`’”]{3,})['\"`’”]", text)
    return bool(_MSG_IS_BUG.search(text)) or any(_norm_msg(expected) == _norm_msg(q) for q in quoted)


# Chosen on a separate dev set of 14 message pairs (8 same, 6 different; none from the benchmark):
# this prompt 14/14 with qwen2.5-coder:3b; a longer "judge for the test's input" variant scored 12/14 and
# confused paraphrases that use opposite words ("non-negative" vs "cannot be negative").
MSG_JUDGE_SYSTEM = ("Two error messages from a program. Do they express the SAME rule or problem, so that a test "
                    "expecting the first would be satisfied by the second in meaning? Answer ONLY JSON: "
                    "{\"same\": true|false, \"why\": \"...\"}")


class MessageJudge:
    """Semantic check of two error messages with the local SLM (evaluation only, temperature 0).
    Used only after the deterministic rules above could not decide. `context` is optional and NOT used by
    the checker: the prompt was validated without it."""

    def __init__(self, llm: LLM):
        self.llm = llm
        self._cache: dict = {}

    def same_meaning(self, expected: str, actual: str, context: str = "") -> tuple[bool, str]:
        key = (_norm_msg(expected), _norm_msg(actual), context)
        if key not in self._cache:
            user = (f"{context.strip()}\n\n" if context.strip() else "") + f"Message A: {expected}\nMessage B: {actual}"
            d = parse_json(self.llm.chat(MSG_JUDGE_SYSTEM, user, json_mode=True, temperature=0.0))
            self._cache[key] = (d.get("same") is True, str(d.get("why", ""))[:200])
        return self._cache[key]


# ------------------------------------------------------------------ patch validation (fixing)
_RESULT_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - (.*))?$", re.M)


class PatchValidator:
    """Checks a proposed patch: reproduction test must now pass, related existing tests must not regress."""

    def __init__(self, executor: Executor, suite_timeout_s: int = 300, max_files: int = 2):
        self.executor = executor
        self.suite_timeout_s = suite_timeout_s
        self.max_files = max_files
        self._baseline: dict[tuple, tuple] = {}

    def select_tests(self, repo: Path, modules: list[str]) -> list[str]:
        """Existing test files that import one of the patched modules (test_<leaf>.py first, then smallest)."""
        hits = []
        for p in Path(repo).rglob("test*.py"):
            rel = str(p.relative_to(repo)).replace("\\", "/")
            if p.name == TEST_FILE or "/." in "/" + rel:
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            best = None
            for i, mod in enumerate(modules):
                pkg, _, leaf = mod.rpartition(".")
                exact = [rf"^\s*import {re.escape(mod)}\b", rf"^\s*from {re.escape(mod)} import"]
                if pkg:
                    exact.append(rf"^\s*from {re.escape(pkg)} import .*\b{re.escape(leaf)}\b")
                named = p.name == f"test_{leaf}.py"       # tests/test_blueprints.py for flask.blueprints
                if any(re.search(x, src, re.M) for x in exact):
                    tier = 0 if named else 1
                elif pkg and re.search(rf"^\s*(from {re.escape(pkg)} import|import {re.escape(pkg)}\b)", src, re.M):
                    tier = 0 if named else 2    # imports the package that re-exports the module's names
                else:
                    continue
                rank = (tier, i)
                best = rank if best is None else min(best, rank)
            if best is not None:
                hits.append((best, len(src), rel))
        return [rel for _, _, rel in sorted(hits)[:self.max_files]]

    def run_suite(self, sb: Sandbox, files: list[str]) -> tuple[set | None, str]:
        """Failing test ids of the given test files (None if the run timed out)."""
        if not files:
            return set(), ""
        out = self.executor.run_files(sb, files, self.suite_timeout_s)
        if out is None:
            return None, f"timed out after {self.suite_timeout_s}s"
        return {m.group(1) for m in _RESULT_LINE.finditer(out)}, out

    def baseline(self, repo: Path, files: list[str], label: str) -> set | None:
        key = (str(repo), tuple(files))
        if key not in self._baseline:
            sb = self.executor.sandbox(repo, label + "_base")
            try:
                self._baseline[key] = self.run_suite(sb, files)
            finally:
                sb.cleanup()
        return self._baseline[key][0]

    def check(self, repo: Path, patch: Patch, repro_test: ReproTest | None, files: list[str],
              label: str, progress_from: Optional[int] = None) -> PatchCheck:
        """`progress_from`: number of repro test functions failing before this patch. When given (iterative,
        multi-location repair), ALL repro test functions are run and a patch that makes some of them pass
        without regressions is reported as `partial` (kept as the base for the next step)."""
        sb = self.executor.sandbox(repo, label + "_patch")
        try:
            for rel, src in patch.files.items():
                sb.write(rel, src)
            repro_status = None
            partial = None
            if repro_test is not None:
                res = self.executor.run(sb, repro_test, stop_first=progress_from is None)
                repro_status = res.status
                if res.status != "passed":
                    if (progress_from is not None and res.status == "failed" and res.n_failed is not None
                            and 0 < res.n_failed < progress_from and (res.n_passed or 0) > 0):
                        partial = res
                    else:
                        return PatchCheck("repro_still_fails", False,
                                          "After your patch the reproduction test still fails: "
                                          f"{res.failure_line or res.status}. The root cause is not fixed yet "
                                          "(or your change broke the code path).", repro_status, [], res.output,
                                          res.n_failed, res.failing)
                sb.remove(TEST_FILE)
            # Always include the tests of the files this patch actually changed (the localiser's guess of
            # where the bug is can differ from where the patch landed).
            changed_mods = [module_name(rel) for rel in patch.files if rel.endswith(".py")]
            files = list(files) + [f for f in self.select_tests(repo, changed_mods) if f not in files][:1]
            base = self.baseline(repo, files, label)
            fails, out = self.run_suite(sb, files)
            regs = sorted(fails - base) if base is not None and fails is not None else []
            if regs:
                details = [m.group(0)[:200] for m in _RESULT_LINE.finditer(out) if m.group(1) in regs][:5]
                return PatchCheck("regression", False,
                                  f"Your patch breaks {len(regs)} existing test(s) that passed before: "
                                  + "; ".join(details) + ". Keep the existing behaviour and fix only the bug.",
                                  repro_status, regs, out[-3000:])
            if partial is not None:
                return PatchCheck("partial", False,
                                  f"Your patch made progress and was KEPT: {progress_from - partial.n_failed} more "
                                  f"reproduction test function(s) now pass, but {partial.n_failed} still fail(s) "
                                  f"({', '.join(partial.failing) or partial.failure_line}): "
                                  f"{partial.failure_line}. The bug needs a change in ANOTHER place too.",
                                  repro_status, [], partial.output, partial.n_failed, partial.failing)
            if base is None or fails is None:
                return PatchCheck("plausible", True, "", repro_status, [], "regression suite timed out")
            return PatchCheck("plausible", True, "", repro_status, [], out[-1500:])
        finally:
            sb.cleanup()


def run_python_check(repo: Path, files: dict, code: str, extra_paths: list[str] | None = None,
                     timeout_s: int = 120, python: str = sys.executable) -> tuple[bool, str]:
    """Run a hidden oracle snippet on a copy of `repo` with `files` patched in (evaluation only)."""
    sb = Sandbox(repo, "oracle")
    try:
        for rel, src in files.items():
            sb.write(rel, src)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(sb.repo)] + (extra_paths or [])),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        try:
            p = subprocess.run([python, "-c", code], cwd=sb.repo, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        return p.returncode == 0, (p.stdout + p.stderr)[-800:]
    finally:
        sb.cleanup()
