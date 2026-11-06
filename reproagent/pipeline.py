"""ReproAgent pipeline: Understand -> Reproduce -> (verify) -> Fix -> (verify) -> Report.

The harness, not the model, decides whether a stage succeeded:
  * a reproduction is accepted only if the generated test FAILS on the buggy code
    for a plausible reason (assertion / expected exception, not import or syntax errors);
  * a fix is accepted only if that test now PASSES and the pre-existing suite still passes.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .agent import AgentLoop, Trace, tool
from .llm import LLM
from .workspace import Workspace

INVALID_FAILURE = re.compile(
    r"(ModuleNotFoundError|ImportError|SyntaxError|IndentationError|NameError: name '\w+' is not defined"
    r"|fixture '\w+' not found|ERROR collecting|no tests ran)"
)

READ_TOOLS = [
    tool("list_files", "List python files in the repository.", subdir="Directory relative to repo root, e.g. '.'"),
    tool("search_code", "Regex search over the repository's python files.", pattern="Regex or plain text"),
    tool("read_file", "Read a file with line numbers.", path="Relative path", start="First line", end="Last line"),
]
REPRO_TOOLS = READ_TOOLS + [
    tool("write_test", "Write the complete pytest file that reproduces the bug (overwrites).", content="Python source"),
    tool("run_test", "Run the reproduction test and return pytest output.", ),
    tool("finish", "Stop. Use status 'reproduced', 'not_reproduced' or 'needs_info'.",
         status="One of reproduced|not_reproduced|needs_info", explanation="Short evidence-based explanation"),
]
FIX_TOOLS = READ_TOOLS + [
    tool("edit_file", "Replace `old` with `new` in a source file. Preferred for small fixes. If `old` occurs "
         "several times, also pass `line` (the line number where the occurrence to change starts).",
         optional=("line",), path="Relative path", old="Exact existing text (copy it from read_file, without "
         "line numbers)", new="Replacement text", line="Optional line number to pick one of several occurrences"),
    tool("replace_lines", "Replace lines start..end (1-based, inclusive, as shown by read_file) with new code.",
         path="Relative path", start="First line", end="Last line", new="Replacement code with correct indentation"),
    tool("run_test", "Run the reproduction test against the current code."),
    tool("finish", "Stop when you believe the fix is complete.", explanation="What you changed and why"),
]

REPRO_SYSTEM = """You are ReproAgent. Your ONLY job in this stage is to write a pytest test that reproduces
the reported bug: the test must FAIL on the current (buggy) code and would PASS once the bug is fixed.
Workflow: locate relevant code with search_code/read_file, write_test, run_test, inspect the output, refine.
The test must assert the CORRECT behaviour described in the report. NEVER write a test that expects the
reported error to happen (e.g. pytest.raises(TheBugError) or asserting the wrong output): such a test passes on
buggy code and fails after the fix, which is the opposite of what we need.
Rules: import the project's real modules (the repo root is on sys.path); assert the EXPECTED behaviour from the
report; do not mock the code under test; never modify source files. A failure caused by ImportError,
SyntaxError or a typo in your test is NOT a reproduction. Call finish when done."""

FIX_SYSTEM = """You are ReproAgent (fix stage). A failing reproduction test exists at {test}. Make the minimal
change to the project's SOURCE code so the test passes without breaking other behaviour. You may not edit the
test. Workflow: read the test, read_file the faulty code, change it with edit_file (preferred: smallest exact edit) or replace_lines,
run_test, iterate. Think about the correct algorithm, not just making the assertion pass. Call finish when it passes."""


@dataclass
class Result:
    bug_id: str
    mode: str
    status: str = "not_reproduced"           # reproduced | not_reproduced | needs_info | env_error
    repro_valid: bool = False                 # harness check: fails for a valid reason
    fail_to_pass: bool | None = None          # oracle check against known fixed version
    fix_status: str = "skipped"               # fixed | not_fixed | skipped
    regression_free: bool | None = None       # no previously-passing test broke
    gt_pass: bool | None = None               # agent's patch passes the developers' own regression test
    repro_attempts: int = 0
    fix_attempts: int = 0
    test_code: str = ""
    patch: str = ""
    explanation: str = ""
    evidence: str = ""
    seconds: float = 0.0
    llm: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)


def classify_failure(code: int, out: str) -> tuple[bool, str]:
    """Is a failing test run plausibly a reproduction?"""
    if code == 0:
        return False, "test passed on buggy code (bug not triggered)"
    if code == -1:
        return False, "timeout"
    if code != 1:  # 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected
        return False, f"pytest exit code {code} (not a test failure)"
    m = INVALID_FAILURE.search(out)
    if m:
        return False, f"invalid failure: {m.group(0)}"
    return True, "test fails with an assertion/exception on buggy code"


class ReproAgent:
    def __init__(self, llm: LLM, mode: str = "full", repro_budget: int = 3, fix_budget: int = 3,
                 max_steps: int = 15, do_fix: bool = True):
        assert mode in ("full", "desc_only", "single_shot")
        self.llm, self.mode = llm, mode
        self.repro_budget, self.fix_budget, self.max_steps, self.do_fix = repro_budget, fix_budget, max_steps, do_fix

    # ------------------------------------------------------------------
    def run(self, repo: str | Path, report: str, bug_id: str = "bug", fixed_repo: str | Path | None = None,
            python: str | None = None, gt_test: str | None = None, on_step=None) -> Result:
        t0 = time.time()
        res = Result(bug_id=bug_id, mode=self.mode)
        trace = Trace(on_step=on_step)
        trace.add("stage", name="preflight")
        ws = Workspace(repo, python)
        self.python = python
        try:
            ok, out = ws.preflight()
            if not ok:
                res.status, res.evidence = "env_error", out
                return self._finish(res, trace, t0)
            trace.add("stage", name="reproduce")
            self._reproduce(ws, report, res, trace)
            if res.repro_valid and fixed_repo:
                res.fail_to_pass = self._oracle(fixed_repo, ws.test_path(), res.test_code, python)
            if res.repro_valid and self.do_fix:
                trace.add("stage", name="fix")
                self._fix(ws, report, res, trace)
                if res.fix_status == "fixed" and fixed_repo and gt_test:
                    res.gt_pass = self._gt_check(ws, fixed_repo, gt_test)
        finally:
            ws.cleanup()
        return self._finish(res, trace, t0)

    def _finish(self, res: Result, trace: Trace, t0: float) -> Result:
        res.seconds = round(time.time() - t0, 1)
        res.llm = dict(self.llm.stats)
        res.trace = trace.steps
        return res

    # ---- Stage 1: reproduce -------------------------------------------
    def _reproduce(self, ws: Workspace, report: str, res: Result, trace: Trace):
        feedback, said_needs_info = "", False
        for attempt in range(1, self.repro_budget + 1):
            res.repro_attempts = attempt
            if self.mode == "full":
                finish = self._repro_agentic(ws, report, feedback, trace)
                res.explanation = (finish or {}).get("explanation", "")
                said_needs_info = bool(finish and finish.get("status") == "needs_info")
            else:
                self._repro_oneshot(ws, report, trace)
            code, out = ws.run_pytest(ws.test_path())
            ok, why = classify_failure(code, out)
            trace.add("verify_repro", attempt=attempt, ok=ok, why=why)
            res.test_code = (ws.root / ws.test_path()).read_text() if (ws.root / ws.test_path()).exists() else ""
            res.evidence = out[-2000:]
            if ok:
                res.status, res.repro_valid = "reproduced", True
                return
            if self.mode != "full":
                return  # baselines: no execution feedback
            # needs_info is a soft signal: SLMs often give up on their own test mistakes, so retry with feedback
            feedback = (f"Previous attempt rejected by the harness: {why}.\nPytest output:\n{out[-2500:]}\n"
                        "If the failure comes from your test (wrong API usage, wrong argument types), read the "
                        "relevant source and fix the test. Only answer needs_info if the report truly lacks details.")
        res.status = "needs_info" if self.mode == "full" and said_needs_info else "not_reproduced"

    def _repro_agentic(self, ws: Workspace, report: str, feedback: str, trace: Trace):
        handlers = {
            "list_files": ws.list_files, "search_code": ws.search_code, "read_file": ws.read_file,
            "write_test": ws.write_test,
            "run_test": lambda **_: "exit=%d\n%s" % ws.run_pytest(ws.test_path()),
        }
        user = f"Bug report:\n{report}\n\nTest file path: {ws.test_path()}\n{feedback}"
        return AgentLoop(self.llm, REPRO_TOOLS, handlers, self.max_steps, trace).run(REPRO_SYSTEM, user)

    def _repro_oneshot(self, ws: Workspace, report: str, trace: Trace):
        """Baselines. desc_only: report only. single_shot: report + file list, one generation."""
        ctx = "" if self.mode == "desc_only" else f"\nRepository files:\n{ws.list_files()}\n"
        prompt = (f"Bug report:\n{report}\n{ctx}\nWrite a complete pytest file that reproduces this bug. "
                  "Reply with ONLY one ```python code block.")
        msg = self.llm.chat([{"role": "system", "content": REPRO_SYSTEM}, {"role": "user", "content": prompt}])
        m = re.search(r"```(?:python)?\n(.*?)```", msg.get("content", ""), re.S)
        code = m.group(1) if m else msg.get("content", "")
        ws.write_test(code)
        trace.add("oneshot", content=code[:1500])

    # ---- Stage 2: fix --------------------------------------------------
    def _fix(self, ws: Workspace, report: str, res: Result, trace: Trace):
        handlers = {
            "list_files": ws.list_files, "search_code": ws.search_code, "read_file": ws.read_file,
            "edit_file": ws.edit_file, "replace_lines": ws.replace_lines,
            "run_test": lambda **_: "exit=%d\n%s" % ws.run_pytest(ws.test_path()),
        }
        baseline = ws.failing_tests()  # tests already failing before any edit are not regressions
        trace.add("baseline_failures", n=None if baseline is None else len(baseline))
        feedback = ""
        for attempt in range(1, self.fix_budget + 1):
            res.fix_attempts = attempt
            if attempt > 1:
                ws.revert()  # each attempt starts from the buggy code, guided by feedback
                feedback += "\n(The source has been reset to the original buggy version.)"
            user = f"Bug report:\n{report}\n\nReproduction test ({ws.test_path()}):\n{res.test_code}\n{feedback}"
            fin = AgentLoop(self.llm, FIX_TOOLS, handlers, self.max_steps, trace).run(
                FIX_SYSTEM.format(test=ws.test_path()), user)
            code, out = ws.run_pytest(ws.test_path())
            if code != 0:
                feedback = f"Harness: reproduction test still fails after your edits:\n{out[-2000:]}"
                trace.add("verify_fix", attempt=attempt, ok=False, why="repro test still fails")
                continue
            after = ws.failing_tests()
            new_fail = sorted((after or set()) - (baseline or set())) if after is not None else ["<suite timeout>"]
            res.regression_free = not new_fail
            if not res.regression_free:
                detail = ""
                if new_fail and new_fail[0] != "<suite timeout>":
                    _, detail = ws.run_pytest(new_fail[0])  # show the model WHY it broke, not just which test
                feedback = ("Harness: repro test passes but these previously passing tests now fail:\n"
                            + "\n".join(new_fail[:20]) + f"\n\nOutput of the first one:\n{detail[-1800:]}\n"
                            "Your edit removed or changed behaviour other code relies on. Change only what the bug needs."
                            f"\nYour last diff was:\n{ws.diff()[-1500:]}")
                trace.add("verify_fix", attempt=attempt, ok=False, why="broke previously passing tests: " + ", ".join(new_fail[:5]))
                continue
            res.fix_status, res.patch = "fixed", ws.diff()
            res.explanation += "\nFix: " + (fin or {}).get("explanation", "")
            trace.add("verify_fix", attempt=attempt, ok=True)
            return
        res.fix_status, res.patch = "not_fixed", ws.diff()

    # ---- evaluation oracle -------------------------------------------
    @staticmethod
    def _gt_check(ws: Workspace, fixed_repo, gt_test: str) -> bool:
        """Evaluation only: does the agent's patched code pass the developers' own test for this bug?"""
        import shutil
        rel = gt_test.split("::")[0]
        shutil.copy(Path(fixed_repo) / rel, ws.root / rel)
        code, _ = ws.run_pytest(gt_test, timeout=300)
        return code == 0

    @staticmethod
    def _oracle(fixed_repo, test_rel: str, test_code: str, python: str | None = None) -> bool:
        """Fail-to-pass: the generated test must pass on the known fixed version."""
        ws = Workspace(fixed_repo, python)
        try:
            (ws.root / test_rel).parent.mkdir(parents=True, exist_ok=True)
            (ws.root / test_rel).write_text(test_code)
            code, _ = ws.run_pytest(test_rel)
            return code == 0
        finally:
            ws.cleanup()


def to_dict(r: Result) -> dict:
    return asdict(r)
