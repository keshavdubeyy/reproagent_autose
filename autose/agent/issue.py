"""Bounded context 1: Issue Management.

* IssueRetrieval  - Function/Tool : load a report from a JSON/text file or a public GitHub issue URL.
* BugSpecifier    - AI capability : turn free text into a BugSpec (expected vs observed, symbols...).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from .llm import LLM
from .models import BugReport, BugSpec


# ------------------------------------------------------------------ tool
class IssueRetrieval:
    GH = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")

    def load(self, ref: str) -> BugReport:
        m = self.GH.search(ref)
        if m:
            return self._from_github(*m.groups())
        p = Path(ref)
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            d = json.loads(text)
            return BugReport(id=d.get("id", p.stem), title=d.get("title", ""),
                             body=d.get("body", ""), source=str(p))
        title, _, body = text.partition("\n")
        return BugReport(id=p.stem, title=title.strip("# "), body=body.strip(), source=str(p))

    def _from_github(self, owner: str, repo: str, num: str) -> BugReport:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{num}", timeout=30,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        d = r.json()
        return BugReport(id=f"{repo}_{num}", title=d["title"], body=d.get("body") or "",
                         source=d["html_url"])


# ------------------------------------------------------------------ AI capability
SPEC_SYSTEM = """You analyse software bug reports for a Python project.
Return ONLY a JSON object with these keys:
  "summary": one sentence,
  "expected": the behaviour the reporter expects (concrete values if given),
  "observed": what actually happens,
  "symbols": list of Python function/class/method names mentioned or implied (identifiers only),
  "files": list of file paths mentioned,
  "example_inputs": list of concrete example inputs/calls from the report (verbatim),
  "failure_kind": "exception" if the bug is a crash/raised error, "wrong_value" if a result is wrong, else "other",
  "is_actionable": false only if the report has no information about expected behaviour at all."""

_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(")
_PATH = re.compile(r"[\w./-]+\.py\b")
_STOP = {"print", "list", "str", "int", "len", "dict", "e", "g", "i"}


class BugSpecifier:
    def __init__(self, llm: LLM | None):
        self.llm = llm

    def specify(self, report: BugReport) -> BugSpec:
        text = f"Title: {report.title}\n\n{report.body}"
        heur = self._heuristic(text)
        if self.llm is None:
            return heur
        d = self.llm.chat_json(SPEC_SYSTEM, text)
        if not d:
            return heur
        # Merge: LLM interpretation + deterministic identifiers (SLMs sometimes drop them).
        symbols = _as_list(d.get("symbols")) + heur.symbols
        return BugSpec(
            summary=str(d.get("summary") or report.title),
            expected=str(d.get("expected") or heur.expected),
            observed=str(d.get("observed") or heur.observed),
            symbols=_dedupe([s.split(".")[-1].strip("()") for s in symbols if s]),
            files=_dedupe(_as_list(d.get("files")) + heur.files),
            example_inputs=_dedupe(_as_list(d.get("example_inputs"))),
            failure_kind=d.get("failure_kind") if d.get("failure_kind") in
            ("exception", "wrong_value", "other") else heur.failure_kind,
            is_actionable=bool(d.get("is_actionable", True)) or bool(heur.symbols),
        )

    @staticmethod
    def _heuristic(text: str) -> BugSpec:
        symbols = [m.split(".")[-1] for m in _IDENT.findall(text)]
        symbols = [s for s in symbols if s not in _STOP]
        files = _PATH.findall(text)
        kind = "exception" if re.search(r"crash|raise|error|exception|traceback", text, re.I) else "wrong_value"
        exp = re.search(r"(?:expected|should)[^.]*\.", text, re.I)
        return BugSpec(summary=text.splitlines()[0][7:], expected=exp.group(0) if exp else "",
                       observed="", symbols=_dedupe(symbols), files=_dedupe(files),
                       failure_kind=kind, is_actionable=True)


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v:
        return [v]
    return []


def _dedupe(xs):
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out
