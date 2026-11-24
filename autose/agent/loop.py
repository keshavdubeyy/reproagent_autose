"""ReproAgent orchestrator (ported from the mid-term version; tests now run through an Executor,
so the same workflow runs locally or inside a SWE-bench Docker image): one SLM-backed agent that drives the three bounded contexts
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

import functools
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .issue import BugSpecifier
from .llm import LLM
from .models import (AttemptRecord, BugReport, BugSpec, FixOutcome, Outcome, PatchAttempt,
                     PatchCheck, ReproTest, Verdict)
from .explore import IndexedExplorer, RepoExplorer, TestGenerator
from .repair import (FaultLocalizer, PatchApplier, PatchGenerator, changed_definitions,
                         cumulative_diff, find_siblings)
from .blackboard import Blackboard, BlackboardError, patch_sha
from .control import LoopController
from .review import Reviewer
from .verify import (FailToPassChecker, MessageJudge, PatchValidator, ResultAnalyzer, relax_invented_messages,
                     wrap_top_level)
from .workspace import TEST_FILE, Executor, LocalExecutor

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
    retrieval: str = "explorer"          # explorer (mid-term keyword search) | bm25 | hybrid | graph
    repo_id: Optional[str] = None        # knowledge package key; None = local project, re-indexed
    commit: Optional[str] = None
    board_dir: Optional[str] = None      # blackboard folder (one sub-folder per bug); None = in memory
    review: str = "off"                  # off | rules | slm: independent review of accepted patches
    loop_control: bool = False           # failure fingerprints: switch strategy / stop on repeats


class ReproAgent:
    def __init__(self, llm: LLM, config: AgentConfig | None = None,
                 log: Callable[[str], None] = functools.partial(print, flush=True), executor: Executor | None = None):
        self.llm = llm
        self._ctx = None          # code retrieved by Bug Understanding (kept for the outcome)
        self.board = Blackboard(None, "-")
        self._review_ctx = None
        self.ctl: Optional[LoopController] = None
        self.cfg = config or AgentConfig()
        self.reviewer = (Reviewer(llm, use_slm=self.cfg.review == "slm")
                         if self.cfg.review in ("rules", "slm") else None)
        m = MODES[self.cfg.mode]
        self.use_context = m["use_context"]
        self.budget = m["budget"] or self.cfg.budget
        self.symbol_feedback = m.get("symbol_feedback", False)
        self.log = log
        self.specifier = BugSpecifier(llm)
        self.generator = TestGenerator(llm)
        self.executor = executor or LocalExecutor(timeout_s=self.cfg.timeout_s, extra_paths=self.cfg.extra_paths)
        self.analyzer = ResultAnalyzer(llm, use_judge=self.cfg.use_judge)
        self.patcher = PatchGenerator(llm)
        self.validator = PatchValidator(self.executor)

    def run(self, report: BugReport, repo: Path, fixed_repo: Optional[Path] = None,
            fixed_executor: Optional[Executor] = None) -> Outcome:
        """`fixed_repo` (local) or `fixed_executor` (SWE-bench: Docker with the gold patch) are evaluation
        only: the final test is re-run on the fixed version and the result is never shown to the agent."""
        t0 = time.time()
        calls0 = self.llm.calls
        self.board = Blackboard(Path(self.cfg.board_dir) if self.cfg.board_dir else None, report.id)
        self.ctl = LoopController() if self.cfg.loop_control else None
        # 1. Issue Management: interpret the report
        spec = self.specifier.specify(report)
        self.log(f"[spec] expected={spec.expected!r} symbols={spec.symbols} kind={spec.failure_kind}")
        if not spec.is_actionable:
            return self._outcome(report, Verdict.NEEDS_INFO, [], spec, None, t0, calls0)

        # 2. Reproduction: explore the code base
        ctx, layout = None, ""
        self._ctx = None
        if self.use_context:
            explorer = (RepoExplorer(repo) if self.cfg.retrieval == "explorer" else
                        IndexedExplorer(repo, self.cfg.retrieval, self.cfg.repo_id, self.cfg.commit))
            ctx = explorer.search(spec, report)
            self._ctx = ctx
            layout = explorer.repo_layout()
            self.analyzer.explorer = explorer if self.symbol_feedback else None
            self.log("[explore] " + ", ".join(f"{s.module}.{s.name}({s.score:.1f})" for s in ctx.snippets))

        # 3-5. Generate -> execute in sandbox -> analyse -> refine
        sandbox = self.executor.sandbox(repo, report.id)
        history: list[AttemptRecord] = []
        final = None
        try:
            for attempt in range(1, self.budget + 1):
                test = self.generator.generate(report, spec, ctx, layout,
                                               history if self.budget > 1 else [], attempt)
                test.code, wrapped = wrap_top_level(test.code)
                if wrapped:
                    self.log("[wrap] moved module-level test code into test_reproduction()")
                test.code, relaxed = relax_invented_messages(test.code, report)
                if relaxed:
                    self.log(f"[relax] dropped invented message check(s): {relaxed}")
                res = self.executor.run(sandbox, test)
                ana = self.analyzer.analyze(report, spec, test, res)
                history.append(AttemptRecord(test, res, ana))
                self.log(f"[attempt {attempt}] exec={res.status} exc={res.exception_type} "
                         f"-> {ana.category}")
                if ana.is_reproduction:
                    final = test
                    break
                if self.ctl is not None:
                    d = self.ctl.observe("reproduce", ana.category, res.exception_type, res.failure_line)
                    if d.action == "switch":
                        ana.feedback = (ana.feedback + " " + d.message).strip()
                    elif d.action == "stop":
                        self.log(f"[control] {d.message}")
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
        last = history[-1].analysis if history else None
        self.board.post("BUG_SPEC", "bug_understanding", {
            "summary": spec.summary or "", "expected": spec.expected or "", "observed": spec.observed or "",
            "failure_kind": spec.failure_kind or "", "retrieved": out.retrieved, "verdict": verdict.value,
            "reproduction_test": final.code if final else None, "attempts": len(history),
            "evidence": (last.llm_judgement or last.category) if last else ""})

        # 7. Bug Resolution: fix the bug, using the accepted reproduction test as localiser and oracle
        if self.cfg.fix and final is not None:
            out.fix = self.fix(report, spec, repo, final, history[-1].execution.output,
                               mode=self.cfg.fix_mode)
            if out.fix.final_diff:
                try:
                    self.board.freeze(out.fix.final_diff)
                except BlackboardError as e:     # rule broken: do not report an unchecked patch as fixed
                    self.log(f"[blackboard] {e}")
                    out.fix.status, out.fix.final_diff = "not_fixed", None

        if self.ctl is not None:
            out.control_log = list(self.ctl.log)

        # Evaluation-only oracle: fail-to-pass on the fixed version (never fed back to the agent).
        if fixed_repo is not None or fixed_executor is not None:
            # Only an accepted reproduction can be a valid one; a rejected test counts as invalid.
            if final is not None:
                strict, meaning, fixed_out, checks = FailToPassChecker(fixed_executor or self.executor).check_with_meaning(
                    fixed_repo or repo, final, report.id, report, MessageJudge(self.llm))
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
                       total_time_s=round(time.time() - t0, 1), llm_calls=self.llm.calls - calls0,
                       retrieved=[f"{sn.file}:{sn.name}" for sn in self._ctx.snippets] if self._ctx else [],
                       retrieval=self.cfg.retrieval if self._ctx else "")

    # ------------------------------------------------------------------ bug resolution
    def fix(self, report: BugReport, spec: BugSpec, repo: Path, repro_test: Optional[ReproTest],
            repro_output: str = "", mode: str = "fix_full") -> FixOutcome:
        """Localise -> generate patch -> validate in sandbox -> refine. The repro test is never edited."""
        t0, calls0 = time.time(), self.llm.calls
        m = FIX_MODES[mode]
        self._review_ctx = (report, spec, repo)
        test = repro_test if m["use_repro"] else None
        if m.get("multi") and test is not None:
            return self._fix_multi(report, spec, repo, test, mode, m, t0, calls0)
        loc = FaultLocalizer(repo)
        cands = loc.localize(report, spec, test.code if test else None, repro_output if test else None,
                             retrieved=self._retrieved())
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
            self._record(history, patch, check, cands)
            self.log(f"[patch {attempt}] -> {check.category}"
                     + (f" ({', '.join(patch.files)})" if patch.files else ""))
            if check.accepted:
                final = patch
                break
            if self.ctl is not None:
                d = self.ctl.observe("repair", check.category, check.repro_status,
                                     (check.feedback or "").split(". ")[0])
                if d.action == "switch":           # same failure again: focus on the next candidate
                    check.feedback = (check.feedback + " " + d.message).strip()
                    cands = cands[1:] + cands[:1]
                    self.log(f"[control] switch: focus on {cands[0].file}:{cands[0].name}" if cands else "[control] switch")
                elif d.action == "stop":
                    self.log(f"[control] {d.message}")
                    break
        return FixOutcome(bug_id=report.id, mode=mode, status="fixed" if final else "not_fixed",
                          attempts=history, candidates=cands, final_diff=final.diff if final else None,
                          total_time_s=round(time.time() - t0, 1), llm_calls=self.llm.calls - calls0,
                          regression_tests=reg_files,
                          locations=_locations(repo, final.files) if final else [])

    def _retrieved(self) -> list[tuple[str, int, str]]:
        """What Bug Understanding's search ranked for this bug, handed to the Fault Localiser."""
        if self._ctx is None or self.cfg.retrieval == "explorer":
            return []           # mid-term configuration: the localiser works as before (ablation A/baseline)
        return list(self._ctx.ranked) or [(sn.file, sn.lineno, sn.name) for sn in self._ctx.snippets]

    def _record(self, history: list, patch, check, cands) -> None:
        """Keep the attempt and publish it: CHANGE_PLAN (what changed) + TEST_REPORT (bound to its hash)."""
        history.append(PatchAttempt(patch, check))
        sha = patch_sha(patch.diff)
        self.board.post("CHANGE_PLAN", "code_modification", {
            "attempt": patch.attempt, "patch_kind": patch.kind,
            "candidates": [f"{c.file}:{c.name} ({c.score}) {c.why}" for c in cands][:10],
            "changed": list(patch.changed), "files": sorted(patch.files), "patch_sha": sha,
            "diff": patch.diff or "", "applies": check.category != "not_applicable"})
        self.board.post("TEST_REPORT", "testing", {
            "attempt": patch.attempt, "patch_sha": sha, "category": check.category, "accepted": check.accepted,
            "repro_status": check.repro_status, "regressions": [str(r) for r in check.regressions][:50],
            "feedback": (check.feedback or "")[:2000], "regression_checked": check.regression_checked})
        if self.reviewer is not None and check.accepted and self._review_ctx is not None:
            report, spec, repo = self._review_ctx
            r = self.reviewer.review(report, spec, repo, patch)
            self.board.post("REVIEW_REPORT", "review", {"patch_sha": sha, "decision": r.decision,
                                                        "reasons": r.reasons, "by": r.by})
            self.log(f"[review] {r.decision}" + (f": {'; '.join(r.reasons)}" if r.reasons else ""))
            if r.decision == "REJECT":          # back to the repair loop with the reviewer's reasons
                check.accepted, check.category = False, "review_rejected"
                check.feedback = ("An independent review rejected your patch: " + " ".join(r.reasons)
                                  + ". Change only what the bug needs and keep all other code exactly as it was.")

    def _fix_multi(self, report: BugReport, spec: BugSpec, repo: Path, test: ReproTest, mode: str,
                   m: dict, t0: float, calls0: int) -> FixOutcome:
        """Iterative, cumulative repair for bugs that need changes in several places."""
        work = self.executor.sandbox(repo, report.id + "_work")      # working copy that holds the kept changes
        try:
            first = self.executor.run(work, test, stop_first=False)   # ALL repro test functions
            work.remove(TEST_FILE)
            failing, out_now = max(first.n_failed or 1, 1), first.output
            loc = FaultLocalizer(work.repo)
            cands = loc.localize(report, spec, test.code, out_now, retrieved=self._retrieved())
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
                self._record(history, patch, check, cands)
                self.log(f"[patch {attempt}] -> {check.category}"
                         + (f" ({', '.join(patch.changed)})" if patch.changed else ""))
                if check.accepted:
                    final = patch
                    break
                if self.ctl is not None and check.category != "partial":
                    d = self.ctl.observe("repair", check.category, check.repro_status,
                                         (check.feedback or "").split(". ")[0])
                    if d.action == "switch":
                        check.feedback = (check.feedback + " " + d.message).strip()
                    elif d.action == "stop":
                        self.log(f"[control] {d.message}")
                        break
                if check.category == "partial":     # keep it, re-localise on the patched code
                    budget = min(budget + 1, m.get("max_budget", 5))   # progress earns one more attempt
                    base = dict(patch.files)
                    failing, out_now = check.repro_failed, check.output
                    for rel, src in base.items():
                        work.write(rel, src)
                    loc = FaultLocalizer(work.repo)
                    focus_code = focus_test(test.code, check.repro_failing)   # only what still fails
                    kept = {tuple(x.split(":", 1)) for x in _locations(repo, base)}
                    cands = [c for c in loc.localize(report, spec, focus_code, out_now, k=8,
                                                     retrieved=self._retrieved())
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
            work.write(rel, src)
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
        self._record(history, sp, check, cands)
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
