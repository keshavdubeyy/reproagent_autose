"""Independent Review agent: read-only check of a patch the tests accepted -> REVIEW_REPORT.

Tests only check behaviour the tests cover. The reviewer looks at the patch itself:

  rules (deterministic, always on when review is enabled)
    * the patch edits test files                                   -> REJECT (fix the code, not the tests)
    * the patch uses a name that exists nowhere in the project      -> REJECT (typo / hallucinated API),
      e.g. flask-4045: `static_url_path=` silently became `static_tpath=`
    * the patch is large (many definitions or many deleted lines)   -> NEEDS_HUMAN (recorded, not blocking)
  slm (optional, `--review slm`)
    * the local model reads the expected behaviour and the diff and may REJECT with a concrete problem

A REJECT is fed back to the repair loop as feedback, and the blackboard refuses to freeze that patch.
"""
from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass, field
from pathlib import Path

from autose.intel.index import is_test_path

from .llm import LLM, parse_json
from .models import BugReport, BugSpec, Patch

MAX_DELETED_LINES = 40
MAX_DEFINITIONS = 5
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BUILTINS = set(dir(builtins)) | {"self", "cls", "__class__", "__name__", "__file__", "__dict__"}

REVIEW_SYSTEM = """You review a code patch for a reported bug. The tests already pass; look for problems tests miss:
changes unrelated to the bug, behaviour the report did not ask for, or a fix that only hides the symptom.
Approve unless you can name a concrete problem in the diff.
Return ONLY JSON: {"decision": "APPROVE" | "REJECT", "problem": "<one sentence, empty when approving>"}"""


@dataclass
class Review:
    decision: str                       # APPROVE | REJECT | NEEDS_HUMAN
    reasons: list[str] = field(default_factory=list)
    by: str = "rules"


class Reviewer:
    _vocab: dict[str, set[str]] = {}    # repo path -> every identifier in its .py files (cached)

    def __init__(self, llm: LLM | None = None, use_slm: bool = False):
        self.llm = llm
        self.use_slm = use_slm and llm is not None

    def review(self, report: BugReport, spec: BugSpec, repo: Path, patch: Patch) -> Review:
        reasons, needs_human = [], []
        tests = [rel for rel in patch.files if is_test_path(rel)]
        if tests:
            reasons.append(f"the patch edits test files ({', '.join(tests)}); fix the project code instead")
        unknown = self.unknown_names(Path(repo), patch)
        for rel, names in unknown.items():
            reasons.append(f"in {rel} the patch uses {', '.join(f'`{n}`' for n in names)}, which exists nowhere "
                           "in the project (a typo or an invented API); keep the original names")
        deleted = sum(1 for l in (patch.diff or "").splitlines() if l.startswith("-") and not l.startswith("---"))
        if deleted > MAX_DELETED_LINES or len(patch.changed) > MAX_DEFINITIONS:
            needs_human.append(f"large change: {deleted} deleted lines, {len(patch.changed)} definitions")
        if reasons:
            return Review("REJECT", reasons, "rules")
        if self.use_slm:
            user = (f"Bug report: {report.title}\n{report.body[:1500]}\n\nExpected behaviour: {spec.expected}\n\n"
                    f"Patch:\n```diff\n{(patch.diff or '')[:4000]}\n```")
            d = parse_json(self.llm.chat(REVIEW_SYSTEM, user, json_mode=True, temperature=0.0))
            problem = str(d.get("problem") or "").strip()
            if str(d.get("decision", "")).upper() == "REJECT" and len(problem.split()) >= 4:
                return Review("REJECT", [problem], "rules+slm")
        if needs_human:
            return Review("NEEDS_HUMAN", needs_human, "rules+slm" if self.use_slm else "rules")
        return Review("APPROVE", [], "rules+slm" if self.use_slm else "rules")

    # ---------------------------------------------------------------- unknown names
    def unknown_names(self, repo: Path, patch: Patch) -> dict[str, list[str]]:
        out = {}
        vocab = None
        for rel, new_src in patch.files.items():
            if not rel.endswith(".py"):
                continue
            try:
                old_src = (repo / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                old_src = ""
            try:
                new_tree = ast.parse(new_src)
                old_used = _used(ast.parse(old_src)) if old_src else set()
            except SyntaxError:
                continue
            introduced = _used(new_tree) - old_used - _defined(new_tree) - _BUILTINS
            if not introduced:
                continue
            if vocab is None:
                vocab = self.vocabulary(repo)
            missing = sorted(n for n in introduced if n not in vocab)
            if missing:
                out[rel] = missing
        return out

    @classmethod
    def vocabulary(cls, repo: Path) -> set[str]:
        key = str(Path(repo).resolve())
        if key not in cls._vocab:
            words: set[str] = set()
            for p in Path(repo).rglob("*.py"):
                try:
                    words.update(_WORD.findall(p.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    pass
            cls._vocab[key] = words
        return cls._vocab[key]


def _used(tree: ast.AST) -> set[str]:
    """Names the code reads: variables, attributes and keyword-argument names."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            out.add(n.attr)
        elif isinstance(n, ast.keyword) and n.arg:
            out.add(n.arg)
    return out


def _defined(tree: ast.AST) -> set[str]:
    """Names the file itself defines (so a new helper or local variable is not 'unknown')."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            out.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out
