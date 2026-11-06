"""Shared models exchanged between the three bounded contexts.

Issue Management  --(BugReport, BugSpec: behaviour + acceptance criteria)-->  Bug Resolution
Bug Resolution    --(ReproTest: candidate reproduction test)--------------->  Verification
Bug Resolution    --(Patch: proposed patch, after a reproduction)---------->  Verification
Verification      --(Analysis / PatchCheck: feedback)---------------------->  Bug Resolution
Verification      --(Outcome + FixOutcome: status/evidence/report)--------->  Issue Management / user
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    NEEDS_INFO = "needs_more_information"


# ---------------------------------------------------------------- Issue Management
@dataclass
class BugReport:
    """Raw report as retrieved from a tracker / file (Issue Retrieval tool output)."""
    id: str
    title: str
    body: str
    source: str = "file"


@dataclass
class BugSpec:
    """Structured interpretation of a report (Bug Specification AI capability output).

    This is the 'behaviour, acceptance criteria' shared model of the architecture.
    """
    summary: str
    expected: str
    observed: str
    symbols: list[str] = field(default_factory=list)      # functions/classes named in report
    files: list[str] = field(default_factory=list)        # file paths named in report
    example_inputs: list[str] = field(default_factory=list)
    failure_kind: str = "wrong_value"                       # wrong_value | exception | other
    is_actionable: bool = True                              # False -> NEEDS_INFO


# ---------------------------------------------------------------- Reproduction
@dataclass
class CodeSnippet:
    file: str
    module: str          # dotted import path, e.g. youtube_dl.utils
    name: str
    lineno: int
    source: str
    score: float = 0.0


@dataclass
class CodeContext:
    snippets: list[CodeSnippet] = field(default_factory=list)
    usage_examples: list[str] = field(default_factory=list)  # lines from existing tests


@dataclass
class ReproTest:
    attempt: int
    code: str
    raw_llm_output: str = ""


# ---------------------------------------------------------------- Verification
@dataclass
class ExecutionResult:
    status: str               # passed | failed | error | collection_error | timeout
    exit_code: int
    duration_s: float
    output: str               # trimmed pytest output
    exception_type: Optional[str] = None
    failure_line: Optional[str] = None
    n_failed: Optional[int] = None    # failed + errored test functions (from the pytest summary)
    n_passed: Optional[int] = None
    failing: list = field(default_factory=list)   # names of the failing test functions


@dataclass
class Analysis:
    """Result Analyzer output: is this failure evidence for the reported bug?"""
    category: str             # reproduced | test_passed | invalid_test | wrong_failure
    is_reproduction: bool
    feedback: str             # what the generator should change next time
    llm_judgement: Optional[str] = None


@dataclass
class AttemptRecord:
    test: ReproTest
    execution: ExecutionResult
    analysis: Analysis


# ---------------------------------------------------------------- Bug Resolution: fixing
@dataclass
class FaultCandidate:
    """A suspicious definition found by the Fault Localizer."""
    file: str
    module: str
    name: str                 # function, method (Class.method) or module constant
    lineno: int
    end_lineno: int
    source: str
    score: float
    why: str = ""


@dataclass
class Patch:
    attempt: int
    code: str                 # definitions returned by the SLM
    diff: str = ""            # unified diff against the buggy version
    files: dict = field(default_factory=dict)   # rel path -> new full source (cumulative)
    raw_llm_output: str = ""
    kind: str = "patch"       # patch | sweep (consistency sweep for the same mistake elsewhere)
    changed: list = field(default_factory=list)  # "file:qualname" of every definition changed so far


@dataclass
class PatchCheck:
    """Patch Validator output: does the patch make the reproduction pass without regressions?"""
    category: str             # plausible | partial (kept, progress) | not_applicable | repro_still_fails | regression
    accepted: bool
    feedback: str
    repro_status: Optional[str] = None
    regressions: list = field(default_factory=list)
    output: str = ""
    repro_failed: Optional[int] = None        # failing repro test functions after the patch
    repro_failing: list = field(default_factory=list)


@dataclass
class PatchAttempt:
    patch: Patch
    check: PatchCheck


@dataclass
class FixOutcome:
    bug_id: str
    mode: str
    status: str               # fixed (plausible patch found) | not_fixed | skipped
    attempts: list
    candidates: list
    final_diff: Optional[str]
    total_time_s: float
    llm_calls: int
    regression_tests: list = field(default_factory=list)
    locations: list = field(default_factory=list)   # "file:qualname" changed by the final patch
    # evaluation only (hidden from the agent)
    correct: Optional[bool] = None          # hidden oracle passes on the patched project
    no_regression: Optional[bool] = None
    eval_output: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for a in d["attempts"]:          # full patched sources are large; the diff is kept
            a["patch"].pop("files", None)
        return d


@dataclass
class Outcome:
    """Final report ('Outcomes: failures, status, report' shared model)."""
    bug_id: str
    mode: str
    verdict: Verdict
    attempts: list[AttemptRecord]
    spec: Optional[BugSpec]
    final_test: Optional[str]
    total_time_s: float
    llm_calls: int
    fail_to_pass: Optional[bool] = None     # None = no fixed version available
    fixed_version_output: Optional[str] = None
    # strict fail-to-pass, OR the only failures on the fixed version are error-message wording
    # differences that mean the same thing (MessageJudge); evaluation only
    fail_to_pass_semantic: Optional[bool] = None
    message_checks: list = field(default_factory=list)
    fix: Optional[FixOutcome] = None        # filled when bug resolution (fixing) is enabled

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        if self.fix is not None:
            d["fix"] = self.fix.to_dict()
        return d
