"""ReproAgent orchestrator: one SLM-backed agent that drives the three bounded contexts
through a fixed workflow with an execution-feedback loop.

Modes (used for the H1/H2 ablations):
  full         code context + execution feedback, budget N attempts      (ReproAgent)
  no_context   description only  + execution feedback, budget N           (H1 ablation)
  single_shot  code context, 1 attempt, no feedback                       (H2 ablation)
  baseline     description only, 1 attempt                                (simple baseline)

Bug resolution (fixing), run after an accepted reproduction:
  fix_full     localisation from the repro test + patch validation feedback, budget 3
  fix_single   same, 1 attempt, no feedback                                (feedback ablation)
  fix_no_repro localisation from the report only, no repro test shown, 1 attempt
               (does reproducing first help fixing?)
  fix_multi    multi-location repair: like fix_full (budget 3) plus (a) all repro test functions are run and
               a patch that fixes some of them without regressions is KEPT, and the next attempt re-localises
               on the patched code and builds on it (cumulative patch); (b) the prompt allows several
               definitions/files; (c) a consistency sweep looks for the same mistake in similar code
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .issue_management import BugSpecifier
from .llm import LLM
from .models import (AttemptRecord, BugReport, BugSpec, FixOutcome, Outcome, PatchAttempt,
                     PatchCheck, ReproTest, Verdict)
from .reproduction import RepoExplorer, TestGenerator
from .resolution import (FaultLocalizer, PatchApplier, PatchGenerator, changed_definitions,
                         cumulative_diff, find_siblings)
from .verification import (FailToPassChecker, MessageJudge, PatchValidator, ResultAnalyzer, Sandbox,
                           TestExecutor)

MODES = {
    "full_v2": dict(use_context=True, budget=None, symbol_feedback=True),
    "full": dict(use_context=True, budget=None),
    "no_context": dict(use_context=False, budget=None),
    "single_shot": dict(use_context=True, budget=1),
    "baseline": dict(use_context=False, budget=1),
}

FIX_MODES = {
    "fix_full": dict(use_repro=True, budget=3),
    "fix_single": dict(use_repro=True, budget=1),
    "fix_no_repro": dict(use_repro=False, budget=1),
    "fix_multi": dict(use_repro=True, budget=3, max_budget=5, multi=True, sweep=True),
}


@dataclass
class AgentConfig:
    mode: str = "full"
    budget: int = 4
    use_judge: bool = True
    timeout_s: int = 90
    extra_paths: Optional[list] = None   # extra PYTHONPATH entries for the sandbox
    fix: bool = False                    # bug resolution after an accepted reproduction
    fix_mode: str = "fix_full"


class ReproAgent:
    def __init__(self, llm: LLM, config: AgentConfig | None = None,
                 log: Callable[[str], None] = print):
        self.llm = llm
        self.cfg = config or AgentConfig()
        m = MODES[self.cfg.mode]
        self.use_context = m["use_context"]
        self.budget = m["budget"] or self.cfg.budget
        self.symbol_feedback = m.get("symbol_feedback", False)
        self.log = log
        self.specifier = BugSpecifier(llm)
        self.generator = TestGenerator(llm)
        self.executor = TestExecutor(timeout_s=self.cfg.timeout_s, extra_paths=self.cfg.extra_paths)
        self.analyzer = ResultAnalyzer(llm, use_judge=self.cfg.use_judge)
        self.patcher = PatchGenerator(llm)
        self.validator = PatchValidator(self.executor)

    def run(self, report: BugReport, repo: Path, fixed_repo: Optional[Path] = None) -> Outcome:
        t0 = time.time()
        calls0 = self.llm.calls
        # 1. Issue Management: interpret the report
        spec = self.specifier.specify(report)
        self.log(f"[spec] expected={spec.expected!r} symbols={spec.symbols} kind={spec.failure_kind}")
        if not spec.is_actionable:
            return self._outcome(report, Verdict.NEEDS_INFO, [], spec, None, t0, calls0)

        # 2. Reproduction: explore the code base
        ctx, layout = None, ""
        if self.use_context:
            explorer = RepoExplorer(repo)
            ctx = explorer.search(spec, report)
            layout = explorer.repo_layout()
            self.analyzer.explorer = explorer if self.symbol_feedback else None
            self.log("[explore] " + ", ".join(f"{s.module}.{s.name}({s.score:.1f})" for s in ctx.snippets))

        # 3-5. Generate -> execute in sandbox -> analyse -> refine
        sandbox = Sandbox(repo, report.id)
        history: list[AttemptRecord] = []
        final = None
        try:
            for attempt in range(1, self.budget + 1):
                test = self.generator.generate(report, spec, ctx, layout,
                                               history if self.budget > 1 else [], attempt)
                res = self.executor.run(sandbox, test)
                ana = self.analyzer.analyze(report, spec, test, res)
                history.append(AttemptRecord(test, res, ana))
                self.log(f"[attempt {attempt}] exec={res.status} exc={res.exception_type} "
                         f"-> {ana.category}")
                if ana.is_reproduction:
                    final = test
                    break
        finally:
            sandbox.cleanup()

        # 6. Report
        if final is not None:
            verdict = Verdict.REPRODUCED
        elif ctx is not None and not ctx.snippets and all(
                h.analysis.category == "invalid_test" for h in history):
            verdict = Verdict.NEEDS_INFO     # nothing in the code base matches the report
        else:
            verdict = Verdict.NOT_REPRODUCED
        out = self._outcome(report, verdict, history, spec, final, t0, calls0)

        # 7. Bug Resolution: fix the bug, using the accepted reproduction test as localiser and oracle
        if self.cfg.fix and final is not None:
            out.fix = self.fix(report, spec, repo, final, history[-1].execution.output,
                               mode=self.cfg.fix_mode)

        # Evaluation-only oracle: fail-to-pass on the fixed version (never fed back to the agent).
        if fixed_repo is not None:
            # Only an accepted reproduction can be a valid one; a rejected test counts as invalid.
            if final is not None:
                strict, meaning, fixed_out, checks = FailToPassChecker(self.executor).check_with_meaning(
                    fixed_repo, final, report.id, report, MessageJudge(self.llm))
                out.fail_to_pass, out.fail_to_pass_semantic = strict, meaning
                out.fixed_version_output, out.message_checks = fixed_out[-1500:], checks
                self.log(f"[f2p] fails-on-buggy=True passes-on-fixed={strict} valid-by-meaning={meaning}"
                         + "".join(f" | '{c['expected']}' vs '{c['actual']}': same={c['same']} ({c['by']})"
                                   for c in checks))
            else:
                out.fail_to_pass = out.fail_to_pass_semantic = False
        return out

    def _outcome(self, report, verdict, history, spec, final, t0, calls0) -> Outcome:
        return Outcome(bug_id=report.id, mode=self.cfg.mode, verdict=verdict, attempts=history,
                       spec=spec, final_test=final.code if final else None,
                       total_time_s=round(time.time() - t0, 1), llm_calls=self.llm.calls - calls0)

    # ------------------------------------------------------------------ bug resolution
    def fix(self, report: BugReport, spec: BugSpec, repo: Path, repro_test: Optional[ReproTest],
            repro_output: str = "", mode: str = "fix_full") -> FixOutcome:
        """Localise -> generate patch -> validate in sandbox -> refine. The repro test is never edited."""
        t0, calls0 = time.time(), self.llm.calls
        m = FIX_MODES[mode]
        test = repro_test if m["use_repro"] else None
        if m.get("multi") and test is not None:
            return self._fix_multi(report, spec, repo, test, mode, m, t0, calls0)
        loc = FaultLocalizer(repo)
        cands = loc.localize(report, spec, test.code if test else None, repro_output if test else None)
        self.log("[localize] " + ", ".join(f"{c.file}:{c.name}({c.score})" for c in cands))
        applier = PatchApplier(loc)
        reg_files = self.validator.select_tests(repo, list(dict.fromkeys(c.module for c in cands if c.module))[:2])
        self.log(f"[regression tests] {reg_files}")
        history: list[PatchAttempt] = []
        final = None
        for attempt in range(1, m["budget"] + 1):
            patch = self.patcher.generate(report, spec, cands, test.code if test else None,
                                          repro_output, history if m["budget"] > 1 else [], attempt)
            err = applier.apply(patch, cands)
            if err:
                check = PatchCheck("not_applicable", False, err)
            else:
                check = self.validator.check(repo, patch, test, reg_files, report.id)
            history.append(PatchAttempt(patch, check))
            self.log(f"[patch {attempt}] -> {check.category}"
                     + (f" ({', '.join(patch.files)})" if patch.files else ""))
            if check.accepted:
                final = patch
                break
        return FixOutcome(bug_id=report.id, mode=mode, status="fixed" if final else "not_fixed",
                          attempts=history, candidates=cands, final_diff=final.diff if final else None,
                          total_time_s=round(time.time() - t0, 1), llm_calls=self.llm.calls - calls0,
                          regression_tests=reg_files,
                          locations=_locations(repo, final.files) if final else [])

    def _fix_multi(self, report: BugReport, spec: BugSpec, repo: Path, test: ReproTest, mode: str,
                   m: dict, t0: float, calls0: int) -> FixOutcome:
        """Iterative, cumulative repair for bugs that need changes in several places."""
        work = Sandbox(repo, report.id + "_work")      # working copy that holds the kept changes
        try:
            first = self.executor.run(work, test, stop_first=False)   # ALL repro test functions
            (work.repo / "test_reproagent_repro.py").unlink(missing_ok=True)
            failing, out_now = max(first.n_failed or 1, 1), first.output
            loc = FaultLocalizer(work.repo)
            cands = loc.localize(report, spec, test.code, out_now)
            all_cands = list(cands)
            focus_code = test.code
            self.log(f"[localize] {failing} failing repro test function(s); "
                     + ", ".join(f"{c.file}:{c.name}({c.score})" for c in cands))
            reg_files = self.validator.select_tests(
                repo, list(dict.fromkeys(c.module for c in cands if c.module))[:2])
            self.log(f"[regression tests] {reg_files}")
            base: dict[str, str] = {}
            history: list[PatchAttempt] = []
            final = None
            budget, attempt = m["budget"], 0
            while attempt < budget:
                attempt += 1
                patch = self.patcher.generate(report, spec, cands, focus_code, out_now, history, attempt,
                                              multi=True, kept_diff=", ".join(q.split(":", 1)[1] for q in _locations(repo, base)) if base else "")
                err = PatchApplier(loc).apply(patch, cands)
                if err:
                    check = PatchCheck("not_applicable", False, err)
                else:
                    patch.files = {**base, **patch.files}
                    patch.diff = cumulative_diff(repo, patch.files)
                    patch.changed = _locations(repo, patch.files)
                    check = self.validator.check(repo, patch, test, reg_files, report.id, progress_from=failing)
                history.append(PatchAttempt(patch, check))
                self.log(f"[patch {attempt}] -> {check.category}"
                         + (f" ({', '.join(patch.changed)})" if patch.changed else ""))
                if check.accepted:
                    final = patch
                    break
                if check.category == "partial":     # keep it, re-localise on the patched code
                    budget = min(budget + 1, m.get("max_budget", 5))   # progress earns one more attempt
                    base = dict(patch.files)
                    failing, out_now = check.repro_failed, check.output
                    for rel, src in base.items():
                        (work.repo / rel).write_text(src, encoding="utf-8")
                    loc = FaultLocalizer(work.repo)
                    focus_code = focus_test(test.code, check.repro_failing)   # only what still fails
                    kept = {tuple(x.split(":", 1)) for x in _locations(repo, base)}
                    cands = [c for c in loc.localize(report, spec, focus_code, out_now, k=8)
                             if (c.file, c.name) not in kept][:6]           # don't let it undo kept fixes
                    all_cands += [c for c in cands if (c.file, c.name) not in {(a.file, a.name) for a in all_cands}]
                    self.log(f"[re-localize] {failing} still failing; "
                             + ", ".join(f"{c.file}:{c.name}({c.score})" for c in cands))
            if final is not None and m.get("sweep"):
                final = self._sweep(report, repo, test, work, final, all_cands, reg_files, history)
            return FixOutcome(bug_id=report.id, mode=mode, status="fixed" if final else "not_fixed",
                              attempts=history, candidates=all_cands, final_diff=final.diff if final else None,
                              total_time_s=round(time.time() - t0, 1), llm_calls=self.llm.calls - calls0,
                              regression_tests=reg_files, locations=final.changed if final else [])
        finally:
            work.cleanup()

    def _sweep(self, report, repo, test, work, final, cands, reg_files, history):
        """Consistency sweep: the same mistake may be repeated in similar code the test does not cover."""
        for rel, src in final.files.items():
            (work.repo / rel).write_text(src, encoding="utf-8")
        loc = FaultLocalizer(work.repo)
        original = {rel: (repo / rel).read_text(encoding="utf-8", errors="replace") for rel in final.files}
        search = list(dict.fromkeys(list(final.files) + [c.file for c in cands]))
        sibs = find_siblings(loc, original, final.files, search)
        if not sibs:
            self.log("[sweep] no similar code")
            return final
        self.log("[sweep] similar code: " + ", ".join(f"{c.file}:{c.name}" for c in sibs))
        sp = self.patcher.sweep(final.diff, sibs, len(history) + 1)
        if sp is None:
            self.log("[sweep] SLM: same mistake not present")
            return final
        err = PatchApplier(loc).apply(sp, sibs)
        if err:
            check = PatchCheck("not_applicable", False, err)
        else:
            sp.files = {**final.files, **sp.files}
            sp.diff = cumulative_diff(repo, sp.files)
            sp.changed = _locations(repo, sp.files)
            check = self.validator.check(repo, sp, test, reg_files, report.id)
        history.append(PatchAttempt(sp, check))
        self.log(f"[sweep] -> {check.category}" + (f" ({', '.join(sp.changed)})" if sp.changed else ""))
        return sp if check.accepted else final


def focus_test(code: str, failing: list[str]) -> str:
    """The reproduction test reduced to the test functions that still fail (imports/helpers kept)."""
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    names = set(failing)
    if not names:
        return code
    lines = code.splitlines()
    drop = [(min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
            and n.name not in names]
    keep = [l for i, l in enumerate(lines, 1) if not any(s <= i <= e for s, e in drop)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)) + "\n"


def _locations(repo: Path, files: dict) -> list[str]:
    out = []
    for rel, new in files.items():
        old = (Path(repo) / rel).read_text(encoding="utf-8", errors="replace")
        out += [f"{rel}:{q}" for q in changed_definitions(old, new)]
    return out
