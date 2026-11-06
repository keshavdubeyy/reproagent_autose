"""Render an Outcome as a human-readable Markdown evidence report + JSON."""
from __future__ import annotations

import json
from pathlib import Path

from .models import FixOutcome, Outcome

ICON = {"reproduced": "REPRODUCED", "not_reproduced": "NOT REPRODUCED",
        "needs_more_information": "NEEDS MORE INFORMATION"}


def to_markdown(o: Outcome) -> str:
    v = o.verdict.value
    lines = [f"# ReproAgent report: `{o.bug_id}`", "",
             f"**Verdict: {ICON[v]}**  (mode `{o.mode}`, {len(o.attempts)} attempt(s), "
             f"{o.llm_calls} SLM calls, {o.total_time_s}s)", ""]
    if o.fail_to_pass is not None:
        lines.append(f"**Fail-to-pass check against fixed version:** "
                     f"{'PASS (valid reproduction)' if o.fail_to_pass else 'FAIL'}\n")
        if not o.fail_to_pass and o.message_checks:
            lines.append(f"**Valid by meaning:** {'YES' if o.fail_to_pass_semantic else 'NO'} - the only failures "
                         "on the fixed version are error-message wording differences:\n")
            for c in o.message_checks:
                lines.append(f"- expected `{c['expected']}`, fixed code says `{c['actual']}` -> "
                             f"{'same meaning' if c['same'] else 'NOT the same'} ({c['by']}: {c['why']})")
            lines.append("")
    if o.spec:
        lines += ["## Interpreted bug specification",
                  f"- Expected: {o.spec.expected}", f"- Observed: {o.spec.observed}",
                  f"- Symbols: {', '.join(o.spec.symbols)}", f"- Kind: {o.spec.failure_kind}", ""]
    if o.final_test:
        last = o.attempts[-1].execution
        lines += ["## Reproduction test", "```python", o.final_test.rstrip(), "```", "",
                  "## Evidence (pytest output on the buggy version)", "```",
                  last.output[-1800:], "```", ""]
        if o.attempts[-1].analysis.llm_judgement:
            lines.append(f"Analyzer judgement: {o.attempts[-1].analysis.llm_judgement}\n")
    lines.append("## Attempt log")
    for a in o.attempts:
        lines.append(f"- Attempt {a.test.attempt}: run=`{a.execution.status}` "
                     f"exception=`{a.execution.exception_type}` -> **{a.analysis.category}**"
                     + (f" — {a.analysis.feedback[:200]}" if a.analysis.feedback else ""))
    if o.fix is not None:
        lines += ["", fix_markdown(o.fix)]
    return "\n".join(lines) + "\n"


def fix_markdown(f: FixOutcome) -> str:
    status = "PLAUSIBLE FIX FOUND" if f.status == "fixed" else "NO VALID FIX"
    lines = [f"## Bug resolution: **{status}**  (mode `{f.mode}`, {len(f.attempts)} patch attempt(s), "
             f"{f.llm_calls} SLM calls, {f.total_time_s}s)", ""]
    if f.correct is not None:
        lines.append(f"**Hidden oracle (evaluation only):** {'PASS (correct)' if f.correct else 'FAIL'}\n")
    lines.append("Fault localisation (most suspicious first): " +
                 ", ".join(f"`{c.file}:{c.name}`" for c in f.candidates[:6]) + "\n")
    lines.append(f"Regression tests run: {', '.join(f.regression_tests) or '(none found)'}\n")
    if f.final_diff:
        if f.locations:
            lines.append(f"Changed locations ({len(f.locations)}): " + ", ".join(f"`{x}`" for x in f.locations) + "\n")
        lines += ["### Proposed patch", "```diff", f.final_diff.rstrip(), "```", "",
                  "Validation: the reproduction test now passes and the related existing tests "
                  "did not regress.", ""]
    lines.append("### Patch attempts")
    for a in f.attempts:
        kind = " (consistency sweep)" if getattr(a.patch, "kind", "patch") == "sweep" else ""
        lines.append(f"- Patch {a.patch.attempt}{kind}: **{a.check.category}**"
                     + (f" — {a.check.feedback[:220]}" if a.check.feedback else ""))
    return "\n".join(lines) + "\n"


def save_fix(f: FixOutcome, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{f.bug_id}.{f.mode}.json").write_text(json.dumps(f.to_dict(), indent=2), encoding="utf-8")
    md = out_dir / f"{f.bug_id}.{f.mode}.md"
    md.write_text(f"# ReproAgent fix report: `{f.bug_id}`\n\n" + fix_markdown(f), encoding="utf-8")
    return md


def save(o: Outcome, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{o.bug_id}.{o.mode}.json").write_text(json.dumps(o.to_dict(), indent=2),
                                                       encoding="utf-8")
    md = out_dir / f"{o.bug_id}.{o.mode}.md"
    md.write_text(to_markdown(o), encoding="utf-8")
    return md
