"""Layer 4: write a patch for the locations Layer 3 found, check it by running real code, retry.

  task    the public bug report, Layer 3's RootCause and the code of its edit locations
  answer  the model writes whole new functions (or SEARCH/REPLACE blocks); edits.py applies them
          to a fresh copy of the original files and refuses anything outside the located code or
          anything that no longer parses
  check   check.py runs the report's own example code and the edited modules' existing tests on
          the original and on the patched code
  retry   the model sees what its patch did (still the same error, newly failing tests, output now
          matches the report's expectation, ...) and revises; at most MAX_ATTEMPTS answers
The best attempt by check.score is kept. Inputs are only public: the bug report, Layer 1-3
outputs and the code snapshot; the benchmark's hidden tests are used only later, by the evaluator.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from autose.patch import check, prompts
from autose.patch.edits import EditError, Workspace, _span, answer_edits, apply_edits
from autose.patch.profiles import PROFILES, Profile
from autose.trace.snippets import statements as report_statements
from autose.trace.runner import EntityMap

MAX_ATTEMPTS = 4
RETRY_TEMPERATURE = 0.7
ANSWER_TOKENS = 2500
REPORT_CHARS = 2200
DIAGNOSIS_CHARS = 1200
CODE_CHARS = 7000
FULL_CLASS_CHARS = 3000
HOT_LINES = 8
LONG_FUNCTION_LINES = 60


NEW_METHOD, NEW_FUNCTION = "(new method)", "(new function)"


def edit_schema(names: list[str]) -> dict:
    """The answer format, enforced by Ollama's structured output: a short diagnosis first (room to
    reason, but not to ramble past the token limit), then the edits, each naming its function."""
    return {"type": "object", "required": ["problem", "fix", "edits"], "properties": {
        "problem": {"type": "string"},
        "fix": {"type": "string"},
        "edits": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
            "type": "object", "required": ["function", "search", "code"], "properties": {
                "function": {"type": "string", "enum": names},
                "search": {"type": "string"},
                "code": {"type": "string"}}}}}}


PLAN_TOKENS = 700


def plan_steps(llm, report: str, diagnosis: str, code: str, names: list[str]) -> list[str]:
    """Fix steps before code (context-engineering-v2 src/fix_steps.py, there written by a large
    model for mini-swe-agent). Off by default: on the 6-bug pilot qwen3:4b over-planned (an extra,
    wrong step that the edit then followed): 2 of 6 verified with it, 3 of 6 without, at one more
    call per bug. Returns [] if the answer is unusable."""
    schema = {"type": "object", "required": ["steps"], "properties": {"steps": {
        "type": "array", "minItems": 1, "maxItems": 4, "items": {
            "type": "object", "required": ["function", "change"], "properties": {
                "function": {"type": "string", "enum": names}, "change": {"type": "string"}}}}}}
    msg = [{"role": "system", "content": prompts.PLAN_SYSTEM},
           {"role": "user", "content": prompts.PLAN.format(report=report, diagnosis=diagnosis, code=code)}]
    try:
        d = json.loads(llm.chat(msg, max_tokens=PLAN_TOKENS, fmt=schema)["content"])
        return [f"{s['function']}: {s['change'].strip()}" for s in d.get("steps", [])
                if isinstance(s, dict) and str(s.get("change", "")).strip()][:4]
    except (ValueError, KeyError, TypeError, AttributeError):
        return []


def parse_edits(answer: str) -> tuple[list[dict], dict | None]:
    """The edits of a JSON answer; if it is cut off or not JSON, any code blocks in the text."""
    try:
        d = json.loads(answer)
        edits = [e for e in d.get("edits") or [] if isinstance(e, dict) and (e.get("code") or "").strip()]
        return edits, d
    except (ValueError, AttributeError):
        return answer_edits(answer), None


def _field(report: dict, key: str) -> str:
    v = report.get(key)
    if isinstance(v, list):
        return "\n".join(f"{i + 1}. {x}" for i, x in enumerate(v))
    return str(v or "").strip()


def report_text(report: dict, limit: int = REPORT_CHARS) -> str:
    parts = [f"{k}:\n{_field(report, k)}" for k in ("Title", "Description", "StepsToReproduce",
                                                    "ExpectedBehavior", "ObservedBehavior") if _field(report, k)]
    text = "\n\n".join(parts)
    return text if len(text) <= limit else text[:limit] + "\n[... report shortened]"


def select_targets(agent: dict, index: dict) -> tuple[list[dict], list[dict], dict, list[str]]:
    """(edit targets, located classes, class id -> member entities, new methods to add) from Layer 3."""
    ents = {e["id"]: e for e in index["entities"]}
    by_q = {e["qualified_name"]: e for e in index["entities"]}
    pl = agent.get("problem_location") or {}
    ids = list(agent.get("edit_locations") or []) + list(pl.get("entities") or [])
    ids += [by_q[q]["id"] for q in pl.get("methods") or [] if q in by_q]
    targets = []
    for i in dict.fromkeys(ids):
        e = ents.get(i)
        if e and not e["is_test"] and e["kind"] in ("function", "method", "class") and e not in targets:
            targets.append(e)
    # a class that is located only through one of its methods is still the place a new method goes
    classes = [t for t in targets if t["kind"] == "class"]
    for t in targets:
        p = ents.get(t.get("parent", ""))
        if t["kind"] == "method" and p and p["kind"] == "class" and p not in classes:
            classes.append(p)
    members = {c["id"]: [e for e in index["entities"] if e.get("parent") == c["id"]] for c in classes}
    new = [m for m in pl.get("new_methods") or [] if m not in ents]
    return targets[:4], classes[:2], members, new


def code_block(read, ent: dict) -> tuple[str, int, int]:
    """The entity's current code (re-found by name, so it is right after earlier edits moved
    lines) and its 1-based first and last line."""
    text = read(ent["file"])
    try:
        start, end = _span(text, ent)
    except EditError:
        start, end = ent["start_line"] - 1, ent["end_line"]
    return "\n".join(text.splitlines()[start:end]), start + 1, end


def code_text(read, targets: list[dict], members: dict, limit: int = CODE_CHARS,
              full_class: int = FULL_CLASS_CHARS, hot: dict | None = None) -> str:
    """hot: entity id -> [line numbers] that ran when the report's example failed (shown under
    the code, never inside it, so the model's copied lines still match the file)."""
    out, used = [], 0
    for t in targets:
        code, first, last = code_block(read, t)
        if t["kind"] == "class" and len(code) > full_class:
            head = code.splitlines()[:6]
            sigs = []
            for m in members.get(t["id"], [])[:40]:
                mc, mf, ml = code_block(read, m)
                sigs.append(f"    {next((l.strip() for l in mc.splitlines() if l.strip().startswith(('def ', 'async def '))), m['name'])}"
                            f"  # lines {mf}-{ml}")
            code = "\n".join(head + ["    ...", "    # methods:"] + sigs)
            note = "class outline only: to change a method, write it in full; to add one, use (new method)"
        else:
            note = f"lines {first}-{last}"
        block = f"### {t['id']} ({note})\n```python\n{code}\n```"
        lines = read(t["file"]).splitlines()
        marks = [n for n in (hot or {}).get(t["id"], []) if 0 < n <= len(lines)]
        if marks:
            block += ("\nLines of it that ran when the report's example failed (and not when it worked):\n"
                      + "\n".join(f"  {n}: {lines[n - 1].strip()[:120]}" for n in marks[:HOT_LINES]))
        if used + len(block) > limit:
            block = block[:max(0, limit - used)] + "\n[... code shortened]\n```"
        out.append(block)
        used += len(block)
        if used >= limit:
            break
    return "\n\n".join(out)


def diagnosis_text(agent: dict, new_methods: list[str], limit: int = DIAGNOSIS_CHARS) -> str:
    rc = _field(agent.get("bug_report") or {}, "RootCause") or (agent.get("analysis") or "")[:limit]
    text = rc[:limit]
    if new_methods:
        text += "\nThe fix probably adds: " + ", ".join(new_methods)
    return text


def expectation(original: dict, revised: dict) -> tuple[str, str]:
    exp = "\n".join(filter(None, [_field(original, "ExpectedBehavior"), _field(revised, "ExpectedBehavior")]))
    obs = "\n".join(filter(None, [_field(original, "ObservedBehavior"), _field(revised, "ObservedBehavior")]))
    return exp, obs


def progress(c: dict | None) -> bool:
    """Partly right: some evidence the bug is fixed and nothing newly broken (build_on_best)."""
    return bool(c) and (c["fixed"] + c["expected"] + c["observed_gone"]) > 0 and not c["regressions"] \
        and not c["broke"]


MAX_FAULTS = 2      # functions paged in per check
MAX_TARGETS = 6     # located + paged in


@dataclass
class Job:
    """Everything one bug's patch attempts share, whichever edit mode drives them."""
    repo: str
    snapshot: Path
    index: dict
    prof: Profile
    original_report: dict
    agent: dict
    targets: list
    classes: list
    members: dict
    new_methods: list
    stmts: list
    exp: str
    obs: str
    before: dict
    tfiles: list
    tests_before: dict
    report: str
    diagnosis: str
    log: object = print
    paged: list = field(default_factory=list)
    layer2: dict = field(default_factory=dict)     # entity id -> Layer 2 rank (0 = first)
    hot: dict = field(default_factory=dict)        # entity id -> lines that ran when the example failed
    repro_tests: int = 0                           # reproduction tests at the end of stmts
    baseline: set = field(default_factory=set)     # functions that run for trivial use of the library (trace)
    _suspects: object = None

    def original(self, f: str) -> str:
        return (self.snapshot / f).read_text(encoding="utf-8")

    @property
    def files(self) -> set:
        return {t["file"] for t in self.targets}

    @property
    def allowed(self) -> list[str]:
        return list(dict.fromkeys([t["name"] for t in self.targets]
                                  + [m["name"] for ms in self.members.values() for m in ms][:40])) + [NEW_METHOD, NEW_FUNCTION]

    @property
    def others(self) -> set:
        member_ids = {m["id"] for ms in self.members.values() for m in ms} | {t["id"] for t in self.targets}
        return {e["name"] for e in self.index["entities"] if e["file"] in self.files and e["id"] not in member_ids}

    def code(self, state: dict) -> str:
        # line hints describe the original code; once a patch moved lines they would point wrong
        return code_text(lambda f: state.get(f) or self.original(f), self.targets, self.members,
                         self.prof.code_chars, self.prof.full_class_chars, None if state else self.hot)

    def find_hot_lines(self) -> None:
        """Line-level hints (spectrum-based, from the report's own example, never the hidden
        tests): for each located function, the lines that ran for a failing example line and not
        for a working one (or all that ran for the failing ones when none works). After Meng et
        al. 2024 (arXiv 2411.10213): line-level localization tracks fixing far better than
        file-level."""
        spans = [t for t in self.targets if t["kind"] in ("function", "method")]
        if not spans or not self.stmts:
            return
        run = check.line_run(self.repo, self.snapshot, self.stmts, [[t["file"], t["start_line"], t["end_line"]] for t in spans])
        bad, good = check.failing_statements(run, self.exp, self.obs)
        if not bad:
            return
        def ran(ids):
            out: dict[str, set] = {}
            for i in ids:
                for f, ls in run.get("lines", {}).get(str(i), {}).items():
                    out.setdefault(f, set()).update(ls)
            return out
        b, g = ran(bad), ran(good)
        for t in spans:
            lines = {n for n in b.get(t["file"], set()) if t["start_line"] <= n <= t["end_line"]}
            only = lines - g.get(t["file"], set())
            pick = sorted(only or lines)
            if pick and len(pick) < (t["end_line"] - t["start_line"] + 1):   # all lines ran: no information
                self.hot[t["id"]] = pick
        if self.hot:
            self.log(f"  line hints: {[(k.split(':')[-1], v[:6]) for k, v in self.hot.items()]}")

    def check(self, files: dict) -> dict:
        after = check.report_run(self.repo, self.snapshot, self.stmts, files)
        tests_after = check.tests_run(self.repo, self.snapshot,
                                      check.test_files_for(self.snapshot, sorted(files)) or self.tfiles, files)
        return check.compare(self.before, after, self.exp, self.obs, self.tests_before, tests_after)

    def page_faults(self, c: dict, files_now: dict | None = None) -> tuple[list[dict], str]:
        """Page faults: code the failing run went through that the model cannot see or edit yet.
        For each example line that still fails, walk its traceback from the innermost frame out:
        - the traceback never reaches located code: the failing path is elsewhere, so its
          innermost repository function is paged in (preferring the located files);
        - it does reach located code: only functions in the located files, below it (a local
          helper the located code calls) are paged in; library code it calls into is not.
        When a line is still wrong (with or without an exception) and the patched files are given,
        suspects.py adds what the run itself shows: the same changed code elsewhere (A), the
        functions on the wrong line's run path (C) and the value trail (D).
        Up to MAX_FAULTS per check, MAX_TARGETS in all. Returns the new targets and the text that
        tells the model why each was added (and the value trail)."""
        ents = {e["id"]: e for e in self.index["entities"]}
        emap = EntityMap(self.index)
        known = {t["id"] for t in self.targets} | {m["id"] for ms in self.members.values() for m in ms}
        target_ids = {t["id"] for t in self.targets}
        cands: list[str] = []
        for r in c.get("rows", []):
            frames = [(f, ln, emap.innermost(f, ln)) for f, ln in r.get("frames", [])]
            reaches = any(e in target_ids or (e and ents[e].get("parent") in target_ids) for _, _, e in frames)
            for f, _, e in reversed(frames):              # innermost first
                if e in target_ids or (e and ents[e].get("parent") in target_ids):
                    break
                if not e or e in known or e in cands or ents[e]["is_test"] or ents[e]["kind"] not in ("function", "method"):
                    continue
                if reaches and f not in self.files:
                    continue
                cands.append(e)
        cands.sort(key=lambda e: ents[e]["file"] not in self.files)
        reasons = {e: "the failing traceback runs through it" for e in cands}
        evidence = ""
        if files_now is not None and c.get("wrong"):
            if self._suspects is None:
                from autose.patch.suspects import Suspects
                self._suspects = Suspects(self)
            found, evidence = self._suspects.investigate(c, files_now)
            for e, why in found:
                if e not in reasons and e not in known:
                    cands.append(e)
                    reasons[e] = why
        room = max(0, MAX_TARGETS - len(self.targets))
        new = [ents[e] for e in cands[:min(MAX_FAULTS, room)]]
        for e in new:
            self.targets.append(e)
            self.paged.append(e["id"])
            p = ents.get(e.get("parent", ""))
            if e["kind"] == "method" and p and p["kind"] == "class" and p["id"] not in self.members:
                self.classes.append(p)
                self.members[p["id"]] = [x for x in self.index["entities"] if x.get("parent") == p["id"]]
        if new:
            self.log(f"  page fault: added {[(e['id'], reasons[e['id']][:60]) for e in new]}")
        note = "" if not new else ("\nThe fix may also need code you could not see before (its code is now shown "
                                   "and you may edit it):\n" + "\n".join(f"- `{e['id']}`: {reasons[e['id']]}" for e in new))
        return new, note + (("\n" + evidence) if evidence else "")


def prepare(repo, original_report, agent, index, snapshot, prof, trace, log, repro: dict | None = None) -> Job | None:
    snapshot = Path(snapshot)
    targets, classes, members, new_methods = select_targets(agent, index)
    if not targets:
        return None
    revised = agent.get("bug_report") or {}
    stmts = list((trace or {}).get("code") or report_statements(original_report))
    # the reproduction stage's kept tests (autose/repro): each fails on the buggy code, so a patch that
    # makes one pass counts as fixing it, like the report's own failing lines
    tests = [t["code"] for t in (repro or {}).get("kept", [])] if prof.repro else []
    if tests:
        log(f"  reproduction tests: {len(tests)}")
    stmts += tests
    exp, obs = expectation(original_report, revised)
    before = check.report_run(repo, snapshot, stmts)
    tfiles = check.test_files_for(snapshot, sorted({t["file"] for t in targets}))
    tests_before = check.tests_run(repo, snapshot, tfiles)
    log(f"  profile={prof.name} mode={prof.edit_mode} targets={[t['id'] for t in targets]} statements={len(stmts)} "
        f"tests={len(tests_before.get('tests', {}))}")
    job = Job(repo, snapshot, index, prof, original_report, agent, targets, classes, members, new_methods, stmts,
               exp, obs, before, tfiles, tests_before, report_text(original_report, prof.report_chars),
               diagnosis_text(agent, new_methods, prof.diagnosis_chars), log)
    job.repro_tests = len(tests)
    return job


def summarise(job: Job, attempts: list, best, steps: list, llm, t0: float) -> dict:
    for a in attempts:
        a.pop("check_full", None)
    chosen = attempts[best] if best is not None else None
    return {"status": "patched" if chosen else "no_patch", "profile": job.prof.name, "mode": job.prof.edit_mode,
            "targets": [t["id"] for t in job.targets], "paged_in": job.paged, "plan": steps,
            "new_methods": job.new_methods, "statements": len(job.stmts),
            "tests_before": len(job.tests_before.get("tests", {})), "best": best,
            "verified": bool(chosen and chosen.get("score") and check.good_enough(chosen.get("check"))),
            "diff": chosen["diff"] if chosen else "", "attempts": attempts, "usage": dict(llm.usage),
            "seconds": round(time.time() - t0, 1)}


def record(att: dict, c: dict) -> None:
    att["check"] = {k: v for k, v in c.items() if k != "rows"} | {"rows": c["rows"][:12]}
    att["score"] = list(check.score(c))
    att["check_full"] = c


def check_line(c: dict) -> str:
    return (f"fixed={c['fixed']} expected={c['expected']} gone={c['observed_gone']} same_error={c['same_error']} "
            f"broke={c['broke']} regressions={len(c['regressions'])}/{c['tests_run']}")


def patch_instance(repo: str, original_report: dict, agent: dict, index: dict, snapshot: Path, llm,
                   trace: dict | None = None, max_attempts: int | None = None, log=print,
                   plan: bool | None = None, profile: Profile | None = None, retrieval: dict | None = None,
                   repro: dict | None = None) -> dict:
    """profile: the strategy for the model's size (profiles.for_model); max_attempts / plan, when
    given, override it. The profile's edit_mode picks JSON answers (this loop) or tool calls
    (toolmode.py); both share the edit checks, the sandbox checks and the page faults."""
    t0 = time.time()
    prof = profile or PROFILES["slm4"]
    max_attempts = prof.max_attempts if max_attempts is None else max_attempts
    plan = prof.plan if plan is None else plan
    job = prepare(repo, original_report, agent, index, snapshot, prof, trace, log, repro)
    if job is not None and trace:
        job.baseline = set(trace.get('baseline') or [])
    if job is not None and retrieval:
        job.layer2 = {e['id']: r for r, e in enumerate(retrieval.get('entities', []))}
    if job is not None and prof.line_hints:
        job.find_hot_lines()
    if job is None:
        return {"status": "no_targets", "diff": "", "attempts": [], "profile": prof.name, "mode": prof.edit_mode}
    steps = []
    if plan:
        steps = plan_steps(llm, job.report, job.diagnosis, job.code({}), job.allowed)
        if steps:
            log(f"  plan: {len(steps)} step(s): " + " | ".join(steps)[:300])
    if prof.edit_mode == "tools":
        from autose.patch.toolmode import tool_loop
        attempts, best = tool_loop(job, llm, steps)
        return summarise(job, attempts, best, steps, llm, t0)
    if prof.candidates > 1:
        from autose.patch.sample import sample_loop
        attempts, best = sample_loop(job, llm, steps)
        return summarise(job, attempts, best, steps, llm, t0)

    def task_for(state: dict) -> list[dict]:
        task = prompts.TASK.format(report=job.report, diagnosis=job.diagnosis, code=job.code(state))
        if steps:
            task += prompts.STEPS.format(steps="\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)))
        if state:
            task += prompts.BUILDING
        system = prompts.SYSTEM + (prompts.FIX_RULES if prof.fix_rules else "")
        return [{"role": "system", "content": system}, {"role": "user", "content": task}]

    state: dict[str, str] = {}   # the code attempts start from: the original, or a partly-right patch
    state_from, att_files = None, {}
    base = task_for(state)
    messages, attempts, best = base, [], None
    temperature = getattr(llm, "temperature", 0.0)
    for n in range(max_attempts):
        # the first answer is the model's best guess; retries sample, or it repeats itself exactly
        if hasattr(llm, "temperature"):
            llm.temperature = temperature if n == 0 else prof.retry_temperature
        answer = llm.chat(messages, max_tokens=prof.answer_tokens, fmt=edit_schema(job.allowed))["content"]
        ws = Workspace(read=job.original, files=dict(state))
        edits, parsed = parse_edits(answer)
        errors = apply_edits(ws, edits, job.targets, job.classes, job.members, job.others) if edits else \
            ["your answer was not complete JSON with at least one edit (keep it shorter)"]
        if errors and prof.atomic:
            ws.files = dict(state)   # all or nothing (AutoSE-Labs DR-007): the base stays clean
        diff = ws.diff()
        att = {"n": n, "answer": answer, "json": parsed, "errors": errors, "edits": ws.log, "diff": diff,
               "built_on": state_from}
        attempts.append(att)
        if not diff or (state and ws.files == state):
            log(f"  attempt {n}: refused {errors[:2]}")
            follow = prompts.REFUSED.format(errors="\n".join(f"- {e}" for e in errors) or "- no change was made")
            messages = base + [{"role": "assistant", "content": answer}, {"role": "user", "content": follow}]
            continue
        same = next((a["n"] for a in attempts[:-1] if a["diff"] == diff), None)
        if same is not None:
            att["same_as"] = same
            log(f"  attempt {n}: same patch as attempt {same}")
            follow = prompts.FEEDBACK.format(result="This is the same patch as before, and it did not fix the bug.",
                                             ask=prompts.ASK_DIFFERENT)
            messages = base + [{"role": "assistant", "content": answer}, {"role": "user", "content": follow}]
            continue
        c = job.check(ws.files)
        record(att, c)
        log(f"  attempt {n}: {check_line(c)}" + (f" (on top of attempt {state_from})" if state_from is not None else ""))
        if best is None or check.score(c) > check.score(attempts[best].get("check_full")):
            best = n
        att_files[n] = dict(ws.files)
        if errors:
            att["partial"] = True
        if check.good_enough(c) and not errors:
            break
        result = check.feedback(c, job.stmts, job.exp, ws.text, job.files)
        if errors:
            result += "\nSome of your edits were refused: " + "; ".join(errors[:3])
        new, note = job.page_faults(c, ws.files)
        att["paged_in"] = [e["id"] for e in new]
        result += note
        ask = prompts.ASK_REVISE
        if prof.build_on_best and best == n and progress(c):
            # keep this partly-right patch and let the next attempt improve it, in place
            state, state_from = att_files[n], n
            ask = prompts.ASK_IMPROVE
        if new or state_from == n:
            base = task_for(state)
        follow = prompts.FEEDBACK.format(result=result, ask=ask)
        messages = base + [{"role": "assistant", "content": answer}, {"role": "user", "content": follow}]
    if hasattr(llm, "temperature"):
        llm.temperature = temperature
    return summarise(job, attempts, best, steps, llm, t0)
