"""Layer 4, sampling strategy: many independent candidates, filtered by tests, chosen by vote.

After Agentless (Xia et al. 2024, arXiv 2407.01489) and the observation in Meng et al. 2024
(arXiv 2411.10213, insight 7) that agent systems fail for lack of sampling: on sympy-24909 every
system found the right function, the model was unsure how to fix it, and only Agentless's 42
samples with a majority vote got it right. For a 4B model each candidate is one cheap call.
  causes    (profile hypotheses) one thinking call names a few different root causes and the
            other places with the same faulty logic; after the first plain candidates, each
            candidate is given one cause, so the candidates differ in more than their sampling
  spread    the candidates go over several suspect locations, most to Layer 3's located code and
            one each to the next suspects (Layer 3's other ranked candidates, then Layer 2's), so
            the budget is not spent on the first suspect alone (their insight 1)
  sample    each candidate starts from the original code; the first at the profile's temperature,
            the rest sampled; a refused answer gets one retry with the refusal
  copy      (profile propagate) an edit is also tried at the other identical places in the located
            functions, as one more candidate (the 4B model often fixes one of several such sites)
  filter    a candidate that breaks existing tests or newly breaks the report's example is out
            (the most reliable signal: on fresh bugs the report-example check missed real fixes)
  vote      the rest are grouped by what they produce on the report's example (or by their
            syntax trees when there is nothing to run); the largest group wins; ties go to more
            evidence from the report's example (error gone, expected output), then by the
            profile's tie_break: the smaller patch, or the one changing more code
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
from dataclasses import replace

from autose.patch import check, prompts
from autose.patch.edits import Workspace, apply_edits, propagate

HYP_TOKENS = 700
HYP_NOTES_CHARS = 6000   # the end of a cut-off reasoning given back for the answer


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


def _canon(text: str) -> list[str]:
    """The code without docstrings, comments or formatting, one statement per line."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text.splitlines()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body \
                and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree).splitlines()


def code_size(read, files: dict) -> int:
    """How much code a patch changes: changed lines once docstrings, comments and formatting are
    left out (a rewrite that only drops a docstring counts nothing)."""
    n = 0
    for f, new in files.items():
        a, b = _canon(read(f)), _canon(new)
        ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
        n += sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
    return n


def behaviour(c: dict | None) -> str:
    """What a patched run produced on every report line and reproduction test."""
    rows = (c or {}).get("rows") or []
    return hashlib.sha1(json.dumps([str(r.get("after"))[:300] for r in rows]).encode()).hexdigest()[:16]


def evidence(c: dict | None) -> int:
    return 0 if not c else c["fixed"] + c["expected"] + c["observed_gone"]


def valid(c: dict | None) -> bool:
    return bool(c) and not c["regressions"] and not c["broke"]


def select(attempts: list[dict], seen: dict, by_behaviour: bool, repro_first: bool,
           tie_break: str = "smallest") -> tuple[dict | None, int]:
    """The chosen candidate (None if none was checked) and how many distinct behaviours passed
    the filter. Sets each attempt's votes. Copied-edit candidates (propagated_from) cast no vote
    of their own but count with their source's votes; only signals the pipeline sees are used."""
    for a in seen.values():
        a["behaviour"] = behaviour(a.get("check_full")) if by_behaviour else a["key"]
    votes: dict[str, int] = {}
    for a in attempts:
        if a.get("key") and "propagated_from" not in a:
            b = seen[a["key"]]["behaviour"]
            votes[b] = votes.get(b, 0) + 1
    by_n = {a["n"]: a for a in attempts}

    def vote_of(a: dict) -> int:
        v = votes.get(a["behaviour"], 0)
        p = by_n.get(a.get("propagated_from"))
        return max(v, votes.get(seen[p["key"]]["behaviour"], 0)) if p is not None and p.get("key") in seen else v

    def tb(a: dict) -> int:            # larger is preferred
        return a.get("size", 0) if tie_break == "size" else -len(a["diff"].splitlines())
    reps: dict[str, dict] = {}
    for a in sorted(seen.values(), key=lambda a: a["n"]):
        if valid(a.get("check_full")):
            b = a["behaviour"]
            if b not in reps or (evidence(a["check_full"]), tb(a)) > (evidence(reps[b]["check_full"]), tb(reps[b])):
                reps[b] = a
    groups = list(reps.values())
    if groups:
        bonus = lambda a: vote_of(a) + (1 if check.good_enough(a["check_full"]) else 0)
        if repro_first:   # gated reproduction tests first, then the vote (as Agentless orders them)
            rank = lambda a: (evidence(a["check_full"]), bonus(a), tb(a))
        else:             # only the report's example, often incomplete: the vote first
            rank = lambda a: (bonus(a), evidence(a["check_full"]), tb(a))
        best = max(groups, key=rank)
    else:                 # nothing passes the filter: the best by score (fewest regressions first)
        checked = [a for a in seen.values() if a.get("check_full")]
        best = max(checked, key=lambda a: (check.score(a["check_full"]), tb(a) if tie_break == "size" else 0)) \
            if checked else None
    for a in attempts:
        a["votes"] = votes.get(seen[a["key"]]["behaviour"], 0) if a.get("key") else 0
    return best, len(groups)


def hypotheses(job, llm, n: int) -> tuple[list[str], list[str]]:
    """One thinking call: n different root causes, and the other places with the same faulty
    logic. ([], []) if the answer is not usable (e.g. the reasoning used up the budget)."""
    schema = {"type": "object", "required": ["causes", "sites"], "properties": {
        "causes": {"type": "array", "minItems": 1, "maxItems": n, "items": {
            "type": "object", "required": ["function", "line", "change"], "properties": {
                "function": {"type": "string"}, "line": {"type": "string"}, "change": {"type": "string"}}}},
        "sites": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "required": ["function", "line"], "properties": {
                "function": {"type": "string"}, "line": {"type": "string"}}}}}}
    messages = [{"role": "system", "content": prompts.HYP_SYSTEM.format(n=n)},
                {"role": "user", "content": prompts.HYP_TASK.format(report=job.report, diagnosis=job.diagnosis,
                                                                   code=job.code({}))}]
    saved = {k: getattr(llm, k) for k in ("think", "think_tokens", "temperature") if hasattr(llm, k)}
    r = {}
    try:
        if "think" in saved:
            # Qwen3 recommends sampling (0.6) in thinking mode: greedy reasoning repeats itself
            llm.think, llm.think_tokens, llm.temperature = True, job.prof.think_tokens, 0.6
        r = llm.chat(messages, max_tokens=HYP_TOKENS, fmt=schema)
        if not r["content"].strip() and r.get("thinking"):
            # the reasoning used up its budget (qwen3 often reasons for 5K+ tokens): answer from
            # the reasoning so far without thinking further ("budget forcing", as in s1)
            if "think" in saved:
                llm.think, llm.temperature = False, 0.0
            notes = prompts.HYP_NOTES.format(notes=r["thinking"][-HYP_NOTES_CHARS:])
            r = llm.chat(messages[:-1] + [{"role": "user", "content": messages[-1]["content"] + notes}],
                         max_tokens=HYP_TOKENS, fmt=schema)
        d = json.loads(r["content"])
    except (ValueError, TypeError, AttributeError):
        job.log(f"  causes: none usable (stopped: {r.get('done_reason')}, {r.get('output_tokens')} tokens)")
        return [], []
    finally:
        for k, v in saved.items():
            setattr(llm, k, v)
    if not isinstance(d, dict):
        return [], []
    causes = [f"{c.get('function', '?')}: `{str(c.get('line', '')).strip()[:160]}`: {str(c['change']).strip()[:300]}"
              for c in d.get("causes") or [] if isinstance(c, dict) and str(c.get("change", "")).strip()][:n]
    sites = list(dict.fromkeys(f"- {s.get('function', '?')}: `{str(s['line']).strip()[:160]}`"
                               for s in d.get("sites") or [] if isinstance(s, dict) and str(s.get("line", "")).strip()))
    return causes, sites[:8]


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
    causes, sites = hypotheses(job, llm, prof.hypotheses) if prof.hypotheses > 0 else ([], [])
    job.hypotheses = {"causes": causes, "sites": sites}
    if prof.hypotheses > 0:
        job.log(f"  causes: {len(causes)}, other sites: {len(sites)}" + (f"; first: {causes[0][:160]}" if causes else ""))
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
        cause = None
        if causes and alt is None and n >= prof.plain_candidates:
            cause = causes[(n - prof.plain_candidates) % len(causes)]
            task += prompts.HYPOTHESIS.format(cause=cause, sites=prompts.SITES.format(sites="\n".join(sites))
                                              if sites else "")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        schema = edit_schema(g.allowed)
        for retry in range(2):                      # one retry only when the answer was refused
            answer = llm.chat(messages, max_tokens=prof.answer_tokens, fmt=schema)["content"]
            ws = Workspace(read=g.original)
            edits, parsed = parse_edits(answer)
            errors = apply_edits(ws, edits, g.targets, g.classes, g.members, g.others, all_places=prof.propagate) if edits else \
                ["your answer was not complete JSON with at least one edit (keep it shorter)"]
            if not errors and not ws.diff():
                errors = [prompts.NO_CHANGE]
            if ws.diff() or retry:
                break
            hint = prompts.REWRITE_HINT if prof.rewrite_hint and any("does not match" in e for e in errors) else ""
            messages = messages + [{"role": "assistant", "content": answer}, {"role": "user", "content":
                                   prompts.REFUSED.format(errors="\n".join(f"- {e}" for e in errors)) + hint}]
        diff = ws.diff()
        att = {"n": n, "location": alt or "located", "answer": answer, "json": parsed, "errors": errors,
               "edits": list(ws.log), "diff": diff}
        if cause:
            att["cause"] = cause
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
        att["size"] = code_size(g.original, ws.files)
        att["_files"], att["_edits"] = dict(ws.files), edits
        seen[key] = att
        job.log(f"  candidate {n} ({att['location'].split(':')[-1]}): {check_line(c)}")
    if hasattr(llm, "temperature"):
        llm.temperature = temperature
    if prof.propagate:                                # the same edits at the other identical places
        for src in sorted(seen.values(), key=lambda a: a["n"]):
            if src["location"] != "located" or "propagated_from" in src:
                continue
            ws = Workspace(read=job.original, files=dict(src["_files"]))
            k = propagate(ws, src["_edits"], job.targets)
            key = patch_key(ws.files) if k else None
            if not k or key in seen:
                continue
            c = job.check(dict(ws.files))
            att = {"n": len(attempts), "location": "located", "propagated_from": src["n"], "answer": "",
                   "json": None, "errors": [], "edits": list(ws.log), "diff": ws.diff(), "key": key}
            record(att, c)
            att["size"] = code_size(job.original, ws.files)
            attempts.append(att)
            seen[key] = att
            job.log(f"  candidate {att['n']} (candidate {src['n']} at {k} more identical place(s)): {check_line(c)}")
    for a in attempts:
        a.pop("_files", None)
        a.pop("_edits", None)
    # vote among the candidates that keep every existing test and example line that worked;
    # behaviour vote: candidates that give the same results on the report's code and the
    # reproduction tests vote together, however differently they are written (identical code
    # already shares one check); with nothing to run, the vote falls back to the code itself
    best, n_groups = select(attempts, seen, bool(job.stmts), bool(job.repro_tests), prof.tie_break)
    if best is not None:
        job.log(f"  chosen: candidate {best['n']} with {best['votes']} vote(s) of {len(attempts)}; "
                f"{n_groups} distinct patch(es) passed the tests")
    return attempts, (best["n"] if best is not None else None)
