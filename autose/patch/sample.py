"""Layer 4, sampling strategy: many independent candidates, filtered by tests, chosen by vote.

After Agentless (Xia et al. 2024, arXiv 2407.01489) and the observation in Meng et al. 2024
(arXiv 2411.10213, insight 7) that agent systems fail for lack of sampling: on sympy-24909 every
system found the right function, the model was unsure how to fix it, and only Agentless's 42
samples with a majority vote got it right. For a 4B model each candidate is one cheap call.
  spread    the candidates go over several suspect locations, most to Layer 3's located code and
            one each to the next suspects (Layer 3's other ranked candidates, then Layer 2's), so
            the budget is not spent on the first suspect alone (their insight 1)
  sample    each candidate starts from the original code; the first at the profile's temperature,
            the rest sampled; a refused answer gets one retry with the refusal
  filter    a candidate that breaks existing tests or newly breaks the report's example is out
            (the most reliable signal: on fresh bugs the report-example check missed real fixes)
  vote      the rest are grouped by their edited files' syntax trees (formatting and comments do
            not count); the largest group wins; ties go to more evidence from the report's
            example (error gone, expected output), then to the smaller patch
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace

from autose.patch import check, prompts
from autose.patch.edits import Workspace, apply_edits


def patch_key(files: dict) -> str:
    """The patch's edited files by syntax tree, so two candidates that differ only in formatting
    or comments vote together."""
    h = hashlib.sha1()
    for f in sorted(files):
        try:
            norm = ast.dump(ast.parse(files[f]))
        except SyntaxError:
            norm = files[f]
        h.update(f.encode() + b"\0" + norm.encode() + b"\0")
    return h.hexdigest()[:16]


def behaviour(c: dict | None) -> str:
    """What a patched run produced on every report line and reproduction test."""
    rows = (c or {}).get("rows") or []
    return hashlib.sha1(json.dumps([str(r.get("after"))[:300] for r in rows]).encode()).hexdigest()[:16]


def evidence(c: dict | None) -> int:
    return 0 if not c else c["fixed"] + c["expected"] + c["observed_gone"]


def valid(c: dict | None) -> bool:
    return bool(c) and not c["regressions"] and not c["broke"]


def alternatives(job, n: int) -> list[dict]:
    """Next suspects outside the located code: Layer 3's other ranked candidates, then Layer 2's."""
    ents = {e["id"]: e for e in job.index["entities"]}
    known = {t["id"] for t in job.targets} | {m["id"] for ms in job.members.values() for m in ms}
    order = list(job.agent.get("primary_ranked") or []) + list(job.agent.get("candidates") or [])
    order += sorted(job.layer2, key=job.layer2.get)[:30]
    out = []
    for i in order:
        i = i if isinstance(i, str) else (i.get("id") if isinstance(i, dict) else None)
        e = ents.get(i or "")
        if e and e["id"] not in known and not e["is_test"] and e["kind"] in ("function", "method") \
                and e not in out:
            out.append(e)
        if len(out) >= n:
            break
    return out


def group_job(job, ent: dict):
    """A Job whose only location is one alternative suspect (its class kept for new methods)."""
    ents = {e["id"]: e for e in job.index["entities"]}
    parent = ents.get(ent.get("parent") or "")
    classes = [parent] if parent and parent["kind"] == "class" else []
    members = {c["id"]: [e for e in job.index["entities"] if e.get("parent") == c["id"]] for c in classes}
    g = replace(job, targets=[ent], classes=classes, members=members, paged=[], hot={})
    tfiles = check.test_files_for(job.snapshot, [ent["file"]])
    if tfiles and set(tfiles) - set(job.tfiles):
        tb = check.tests_run(job.repo, job.snapshot, tfiles)
        g = replace(g, tfiles=tfiles, tests_before={"tests": {**job.tests_before.get("tests", {}), **tb.get("tests", {})}})
    return g


def sample_loop(job, llm, steps: list[str]) -> tuple[list[dict], int | None]:
    from autose.patch.loop import check_line, edit_schema, parse_edits, record

    prof = job.prof
    n_alt = min(prof.alt_locations, max(0, prof.candidates - 3))
    alts = alternatives(job, n_alt)
    plan = [(job, None)] * (prof.candidates - len(alts)) + [(group_job(job, e), e["id"]) for e in alts]
    temperature = getattr(llm, "temperature", 0.0)
    attempts: list[dict] = []
    seen: dict[str, dict] = {}    # patch key -> the attempt that was checked
    for n, (g, alt) in enumerate(plan):
        if hasattr(llm, "temperature"):
            llm.temperature = temperature if n == 0 else prof.retry_temperature
        system = prompts.SYSTEM + (prompts.FIX_RULES if prof.fix_rules else "")
        task = prompts.TASK.format(report=g.report, diagnosis=g.diagnosis, code=g.code({}))
        if steps and alt is None:
            task += prompts.STEPS.format(steps="\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        schema = edit_schema(g.allowed)
        for retry in range(2):                      # one retry only when the answer was refused
            answer = llm.chat(messages, max_tokens=prof.answer_tokens, fmt=schema)["content"]
            ws = Workspace(read=g.original)
            edits, parsed = parse_edits(answer)
            errors = apply_edits(ws, edits, g.targets, g.classes, g.members, g.others) if edits else \
                ["your answer was not complete JSON with at least one edit (keep it shorter)"]
            if ws.diff() or retry:
                break
            messages = messages + [{"role": "assistant", "content": answer}, {"role": "user", "content":
                                   prompts.REFUSED.format(errors="\n".join(f"- {e}" for e in errors))}]
        diff = ws.diff()
        att = {"n": n, "location": alt or "located", "answer": answer, "json": parsed, "errors": errors,
               "edits": list(ws.log), "diff": diff}
        attempts.append(att)
        if not diff:
            job.log(f"  candidate {n} ({att['location'].split(':')[-1]}): refused {errors[:1]}")
            continue
        key = patch_key(ws.files)
        att["key"] = key
        if key in seen:                               # same patch as an earlier candidate: one more vote
            att["same_as"] = seen[key]["n"]
            att["check"] = seen[key].get("check")
            job.log(f"  candidate {n}: same patch as candidate {seen[key]['n']}")
            continue
        c = g.check(dict(ws.files))
        record(att, c)
        seen[key] = att
        job.log(f"  candidate {n} ({att['location'].split(':')[-1]}): {check_line(c)}")
    if hasattr(llm, "temperature"):
        llm.temperature = temperature
    # vote among the candidates that keep every existing test and example line that worked
    # behaviour vote: candidates that give the same results on the report's code and the
    # reproduction tests vote together, however differently they are written (identical code
    # already shares one check); with nothing to run, the vote falls back to the code itself
    for a in seen.values():
        a["behaviour"] = behaviour(a.get("check_full")) if job.stmts else a["key"]
    votes: dict[str, int] = {}
    for a in attempts:
        if a.get("key"):
            b = seen[a["key"]]["behaviour"]
            votes[b] = votes.get(b, 0) + 1
    reps: dict[str, dict] = {}
    for a in sorted(seen.values(), key=lambda a: a["n"]):
        if valid(a.get("check_full")):
            b = a["behaviour"]
            if b not in reps or (evidence(a["check_full"]), -len(a["diff"].splitlines())) > \
                    (evidence(reps[b]["check_full"]), -len(reps[b]["diff"].splitlines())):
                reps[b] = a
    groups = list(reps.values())
    if groups:
        bonus = lambda a: votes[a["behaviour"]] + (1 if check.good_enough(a["check_full"]) else 0)
        if job.repro_tests:   # gated reproduction tests first, then the vote (as Agentless orders them)
            rank = lambda a: (evidence(a["check_full"]), bonus(a), -len(a["diff"].splitlines()))
        else:                 # only the report's example, often incomplete: the vote first
            rank = lambda a: (bonus(a), evidence(a["check_full"]), -len(a["diff"].splitlines()))
        best = max(groups, key=rank)
    else:                                             # nothing passes the filter: the best by score
        checked = [a for a in seen.values() if a.get("check_full")]
        best = max(checked, key=lambda a: check.score(a["check_full"])) if checked else None
    for a in attempts:
        a["votes"] = votes.get(seen[a["key"]]["behaviour"], 0) if a.get("key") else 0
    if best is not None:
        job.log(f"  chosen: candidate {best['n']} with {votes.get(best.get('behaviour'), 0)} vote(s) of {len(attempts)}; "
                f"{len(groups)} distinct patch(es) passed the tests")
    return attempts, (best["n"] if best is not None else None)
