"""Reproduction stage: the model writes small assert-style tests from the bug report, and the
harness keeps only those that fail on the buggy code in the way the report describes.

After MarsCode Agent and Honeycomb (reproduction scripts written by the model, the final patch
chosen among candidates by them) and Agentless's generated reproduction tests, with the warning
of Meng et al. 2024 (arXiv 2411.10213, insight 12) taken as the design rule: an incorrect
reproduction can sink the whole fix, so a test is trusted only after the gate.
  write   one call (a second, sampled, if nothing survives): tests for the report's own case
          ("report") and for one or two nearby inputs of the same kind ("variant"), because the
          benchmark's hidden tests check more cases than the report's example
  gate    each test runs on the untouched snapshot in the sandbox. Kept only if it fails there:
          an AssertionError (the expected value is not what the buggy code gives) or an error
          raised inside the repository. A test that passes on buggy code tests nothing; one that
          fails in its own code (NameError, SyntaxError, a typo in an API name) is broken.
Inputs are only public: the bug report, Layer 3's report, the report's code (trace.json) and
the located functions' signatures. Output: workspaces/<id>/repro.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from autose.patch import check

ANSWER_TOKENS = 1600
MAX_TESTS = 5
REPORT_CHARS = 2200
BROKEN = ("SyntaxError", "NameError", "ImportError", "ModuleNotFoundError", "IndentationError")

SYSTEM = """You write small reproduction tests for a bug report, in Python 3.9. You do not fix
the bug. Each test is a few plain statements ending in one or more `assert` lines that state what
the CORRECT code must do, as the report describes it. Rules:
- Use only the public API used in the report; import what you use in each test.
- No pytest, no fixtures, no prints, no files, no network; each test runs on its own.
- Assert exact values only when the report gives them; otherwise assert what must hold (no
  exception, a type, an equality between two ways of computing the same thing).
Answer in JSON: tests, a list of {name, kind, code}; kind is "report" for the report's own case,
"variant" for the same kind of case with other inputs (the fix must handle those too)."""

TASK = """Bug report:
{report}

The report's own code (as runnable statements):
{code}

{context}Write {n} tests: one or two of kind "report", the rest of kind "variant"."""

RETRY = """None of these tests reproduced the bug on the buggy code:
{why}
Write {n} new tests. A test must FAIL on the buggy code (its assert does not hold, or the bug's
error is raised) and pass once the bug is fixed. Use only names the report uses."""


def schema() -> dict:
    return {"type": "object", "required": ["tests"], "properties": {"tests": {
        "type": "array", "minItems": 1, "maxItems": MAX_TESTS, "items": {
            "type": "object", "required": ["name", "kind", "code"], "properties": {
                "name": {"type": "string"}, "kind": {"type": "string", "enum": ["report", "variant"]},
                "code": {"type": "string"}}}}}}


def report_text(report: dict) -> str:
    from autose.patch.loop import report_text as rt
    return rt(report, REPORT_CHARS)


def gate(run: dict, tests: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, rejected) from a run of the tests on the buggy code."""
    recs = {r["i"]: r for r in run.get("statements", [])}
    kept, rejected = [], []
    for i, t in enumerate(tests):
        r = recs.get(i)
        if r is None:
            rejected.append(t | {"why": "did not run (time limit)"})
        elif r["ok"]:
            rejected.append(t | {"why": "passes on the buggy code, so it does not test the bug"})
        elif str(r["error"]).startswith("AssertionError") or r.get("frames"):
            if str(r["error"]).startswith(BROKEN) and not r.get("frames"):
                rejected.append(t | {"why": f"broken test: {r['error'][:120]}"})
            else:
                kept.append(t | {"before": r["error"][:200]})
        else:
            rejected.append(t | {"why": f"fails in the test's own code: {r['error'][:120]}"})
    return kept, rejected


def reproduce(repo: str, original_report: dict, agent: dict, index: dict, snapshot: Path, llm,
              trace: dict | None = None, n_tests: int = 4, log=print) -> dict:
    t0 = time.time()
    snapshot = Path(snapshot)
    stmts = (trace or {}).get("code") or []
    ents = {e["id"]: e for e in index["entities"]}
    sigs = []
    for i in (agent.get("problem_location") or {}).get("entities", [])[:4]:
        e = ents.get(i)
        if e:
            try:
                line = (snapshot / e["file"]).read_text(encoding="utf-8").splitlines()[e["start_line"] - 1].strip()
            except (OSError, IndexError):
                line = ""
            sigs.append(f"- {e['qualified_name']}: {line}")
    context = ("Code the bug is in (for names and arguments only):\n" + "\n".join(sigs) + "\n\n") if sigs else ""
    from autose.trace.runner import REPOS
    pre = REPOS.get(repo, {}).get("preamble") or []
    if pre:
        context += "Already defined before each test: " + "; ".join(pre)[:300] + "\n\n"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": TASK.format(report=report_text(original_report),
                                                       code="\n".join(stmts)[:1500] or "(none)",
                                                       context=context, n=n_tests)}]
    temperature = getattr(llm, "temperature", 0.0)
    kept, rejected, rounds = [], [], 0
    for rounds in range(1, 3):
        if hasattr(llm, "temperature") and rounds > 1:
            llm.temperature = 0.7
        answer = llm.chat(messages, max_tokens=ANSWER_TOKENS, fmt=schema())["content"]
        try:
            tests = [t for t in json.loads(answer).get("tests", []) if isinstance(t, dict) and str(t.get("code", "")).strip()]
        except (ValueError, AttributeError):
            tests = []
        tests = [{"name": str(t.get("name", f"t{i}"))[:60], "kind": t.get("kind", "report"), "code": t["code"]}
                 for i, t in enumerate(tests[:MAX_TESTS])]
        run = check.report_run(repo, snapshot, [t["code"] for t in tests]) if tests else {"statements": []}
        k, r = gate(run, tests)
        kept += k
        rejected += r
        log(f"  round {rounds}: {len(tests)} written, {len(k)} kept, "
            f"rejected: {[x['why'][:40] for x in r][:4]}")
        if kept or not tests and rounds > 1:
            break
        why = "\n".join(f"- {x['name']}: {x['why']}" for x in r[:5]) or "- the answer had no tests"
        messages = messages + [{"role": "assistant", "content": answer},
                               {"role": "user", "content": RETRY.format(why=why, n=n_tests)}]
    if hasattr(llm, "temperature"):
        llm.temperature = temperature
    return {"status": "ok" if kept else "none_kept", "kept": kept, "rejected": rejected, "rounds": rounds,
            "usage": dict(getattr(llm, "usage", {})), "seconds": round(time.time() - t0, 1)}
