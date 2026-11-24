"""Replay of real patches the SLM wrote in night runs 1 and 2 (tests/fixtures/night_patches.json: the original
definition and the model's rewrite, for every attempt that deleted code or used a placeholder).

Invariant: the applier never splices in a rewrite that silently loses existing code. Each case is either
refused with feedback for the model, or the spliced text has no placeholder, parses, and drops at most
GUARD_MAX_DROPPED code lines in a block. The accept/refuse decision is also pinned, so a later change
that flips one shows up here. No model, no Docker, no benchmark checkout needed."""

import ast
import difflib
import json
from pathlib import Path

import pytest

from autose.agent.repair import GUARD_MAX_DROPPED, _elision_lines, _soft_lines, guard_definition

ALL = json.loads((Path(__file__).parent / "fixtures" / "night_patches.json").read_text())
CASES = [c for c in ALL if "orig" in c]     # the rest were refused before splicing (e.g. def replacing a class)


def _worst_dropped(orig: str, merged: str) -> int:
    """Largest block of original code lines (not docs/comments/blank) missing from `merged`."""
    o, m, soft = orig.splitlines(), merged.splitlines(), _soft_lines(orig)
    worst = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [l.strip() for l in o], [l.strip() for l in m],
                                                       autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            worst = max(worst, sum(k not in soft for k in range(i1, i2)) - (j2 - j1))
    return worst


def test_fixture_covers_the_known_failures():
    ids = {c["id"].split("/")[1] for c in ALL}
    assert {"django__django-12497", "sympy__sympy-13437", "sympy__sympy-14774"} <= ids
    assert len(CASES) >= 10


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_no_recorded_patch_silently_loses_code(case):
    merged, problem = guard_definition(case["orig"], case["new"])
    assert (problem is not None) == case["refused"], problem
    if problem:
        assert "COMPLETE" in problem or "placeholder" in problem
        return
    ast.parse(merged)
    assert _elision_lines(merged, case["orig"]) == []
    assert _worst_dropped(case["orig"], merged) <= GUARD_MAX_DROPPED
