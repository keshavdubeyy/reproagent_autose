"""Rank traced code for Layer 3: exception frames first (innermost first), then the functions that
ran for the report's code but not for the baseline, by BM25 similarity to the report. BM25 is
built over just those functions, so a word common to all the code that ran counts for little."""
from __future__ import annotations

from pathlib import Path

from autose.retrieval.lexical import BM25, DOC_CHARS


def new_entities(trace: dict) -> list[str]:
    base = set(trace.get("baseline", []))
    return [e for e in trace.get("executed", {}) if e not in base]


RRF_K = 60


def fuse(layer2: list[str], traced: list[str], frames: list[str]) -> list[str]:
    """Exception frames (innermost first), then Layer 2 and the trace ranking by reciprocal rank
    fusion with equal weights. On the 73 traced sympy bugs this put the right function in the
    top 8 on 44, against 36 for Layer 2 alone (chosen there from 7 simple variants)."""
    score: dict[str, float] = {}
    for ranked in (layer2, traced):
        for r, e in enumerate(ranked):
            score[e] = score.get(e, 0.0) + 1.0 / (RRF_K + r + 1)
    return list(dict.fromkeys(list(reversed(frames)) + sorted(score, key=lambda e: (-score[e], e))))


def summary(trace: dict, limit: int = 4) -> str:
    """What happened when the harness ran the report's code, for the model."""
    if trace.get("status") != "ran":
        return "(the report's code was not run)"
    lines = [f"{trace.get('ok_statements', 0)} of {trace.get('statements', 0)} statements from the report ran "
             f"without error{' (stopped at the time limit)' if trace.get('timed_out') else ''}."]
    errs = [e["error"] for e in trace.get("errors", []) if not str(e["error"]).startswith(("SyntaxError", "NameError"))]
    for e in list(dict.fromkeys(errs))[:limit]:
        lines.append(f"Raised: {e[:200]}")
    if trace.get("frames"):
        lines.append("Repository functions in the traceback (innermost last): " + " -> ".join(trace["frames"][-limit:]))
    return "\n".join(lines)


def ranking(trace: dict, report: str, index: dict, code_dir: str | Path) -> list[str]:
    if trace.get("status") != "ran":
        return []
    ents = {e["id"]: e for e in index["entities"]}
    ids = [i for i in new_entities(trace) if i in ents and not ents[i]["is_test"]]
    files: dict[str, list[str]] = {}
    docs = []
    for i in ids:
        e = ents[i]
        if e["file"] not in files:
            try:
                files[e["file"]] = (Path(code_dir) / e["file"]).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                files[e["file"]] = []
        code = "\n".join(files[e["file"]][e["start_line"] - 1:e["end_line"]])
        docs.append(f"{i} {code[:DOC_CHARS]}")
    score = dict(BM25(ids, docs).rank(report, top_k=len(ids))) if ids else {}
    frames = [f for f in reversed(trace.get("frames", [])) if f in ents and not ents[f]["is_test"]]
    return list(dict.fromkeys(frames + sorted(ids, key=lambda i: (-score.get(i, 0.0), i))))
