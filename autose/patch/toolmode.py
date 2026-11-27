"""Layer 4, second edit mode: the model edits through tool calls (Ollama native tool calling).

The edit tools follow the editor tools of SWE-agent / AutoSE-Labs/autose (edit_file with
old_str -> new_str, DR-007): one exact, validated change per call, applied in place to the
working copy, which persists between calls. The model reads, edits, checks and submits in
whatever order it likes, as in mini-swe-agent's loop, but with editing tools instead of a shell.
  view_code(function)                 the current code of a function it may edit
  replace(function, old, new)         exact lines -> new lines, inside that function
  write_function(function, code)      a whole new version, or a new method / helper
  run_check()                         run the report's example and the tests on the current code
  submit()                            finish (the code is checked first if it changed)
Same guards as the JSON mode: only located (or paged-in) code can change, every edit must match
exactly once and leave the file parsing, nothing on disk changes. Each run_check is an attempt;
the best by check.score is kept. Old tool output is evicted when the context fills up.
Used by default for the slm8 / slm14 profiles; the slm4 profile answers in JSON.
"""
from __future__ import annotations

import json

from autose.patch import check, prompts
from autose.patch.edits import EditError, Workspace, apply_edits

TOOL_OUT_CHARS = 2000
KEEP_RECENT_TOOL_OUTPUTS = 4
MAX_NUDGES = 3     # turns in a row without a tool call (mini-swe-agent: max_consecutive_format_errors)


def tool_specs(names: list[str]) -> list[dict]:
    fn = {"type": "string", "description": "function or method name", "enum": names}

    def spec(name, desc, props, req):
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": props, "required": req}}}
    return [
        spec("view_code", "Show the current code of a function you may edit.", {"function": fn}, ["function"]),
        spec("replace", "Replace exact existing lines inside a function (copy them from the code shown).",
             {"function": fn, "old": {"type": "string", "description": "exact existing lines, 1 to 5"},
              "new": {"type": "string", "description": "the lines that replace them"}}, ["function", "old", "new"]),
        spec("write_function", "Write a complete new version of a function, or a new method / helper "
             "(function = \"(new method)\" or \"(new function)\").",
             {"function": fn, "code": {"type": "string", "description": "the complete def"}}, ["function", "code"]),
        spec("run_check", "Run the bug report's example code and the existing tests on the current code.", {}, []),
        spec("submit", "Finish: the current code is your fix.", {}, []),
    ]


def _arg(call: dict, key: str) -> str:
    v = (call.get("arguments") or {}).get(key, "")
    return v if isinstance(v, str) else json.dumps(v)


def evict(messages: list[dict], budget_chars: int) -> list[dict]:
    """Shrink old tool outputs once the conversation outgrows the budget (the newest stay whole)."""
    total = sum(len(m.get("content") or "") for m in messages)
    if total <= budget_chars:
        return messages
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    out = list(messages)
    for i in tool_idx[:-KEEP_RECENT_TOOL_OUTPUTS]:
        if len(out[i]["content"]) > 120:
            out[i] = out[i] | {"content": out[i]["content"][:100] + " [... older output removed to save space]"}
    return out


def tool_loop(job, llm, steps: list[str]) -> tuple[list[dict], int | None]:
    from autose.patch.loop import code_block, record, check_line   # shared with the JSON loop

    prof = job.prof
    ws = Workspace(read=job.original)
    task = prompts.TASK.format(report=job.report, diagnosis=job.diagnosis, code=job.code({}))
    if steps:
        task += prompts.STEPS.format(steps="\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)))
    system = prompts.TOOL_SYSTEM + (prompts.FIX_RULES if prof.fix_rules else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task + prompts.TOOL_TASK}]
    attempts: list[dict] = []
    best, nudges, checked_diff, done = None, 0, None, False
    budget = int(prof.num_ctx * 2.6)  # characters, about 3.5 per token, minus room for the answer

    def find(name: str):
        return next((t for t in job.targets if t["name"] == name), None) or \
            next((m for ms in job.members.values() for m in ms if m["name"] == name), None)

    def run_check(step: int) -> str:
        nonlocal best, checked_diff, done
        diff = ws.diff()
        if not diff:
            return "No change has been made yet."
        if diff == checked_diff:
            return "Nothing changed since the last check."
        att = {"n": len(attempts), "step": step, "diff": diff, "edits": list(ws.log), "errors": []}
        attempts.append(att)
        c = job.check(dict(ws.files))
        record(att, c)
        checked_diff = diff
        job.log(f"  check {att['n']} (turn {step}): {check_line(c)}")
        if best is None or check.score(c) > check.score(attempts[best].get("check_full")):
            best = att["n"]
        if check.good_enough(c):
            done = True
            return "The bug is fixed: " + check.feedback(c, job.stmts, job.exp, ws.text, job.files) + "\nCall submit."
        new, note = job.page_faults(c, dict(ws.files))
        att["paged_in"] = [e["id"] for e in new]
        return (check.feedback(c, job.stmts, job.exp, ws.text, job.files) + note
                + ("\nUse view_code to read it." if new else "") + "\nFix what is still wrong, then run_check again.")

    def execute(call: dict, step: int) -> str:
        nonlocal done
        name = call.get("name")
        if name == "run_check":
            return run_check(step)
        if name == "submit":
            out = run_check(step) if ws.diff() and ws.diff() != checked_diff else ""
            done = True
            return (out + "\nSubmitted.").strip()
        fn = _arg(call, "function")
        if name == "view_code":
            t = find(fn)
            if t is None:
                return f"Unknown function {fn!r}; you may view: {', '.join(job.allowed[:-2])}"
            code, first, last = code_block(ws.text, t)
            return f"{t['id']} (lines {first}-{last}):\n{code}"
        if name in ("replace", "write_function"):
            edit = {"function": fn, "search": _arg(call, "old") if name == "replace" else "",
                    "code": _arg(call, "new") if name == "replace" else _arg(call, "code")}
            if name == "replace" and not edit["search"].strip():
                return "Error: old is empty; copy the exact lines to change, or use write_function."
            trial = Workspace(read=job.original, files=dict(ws.files))
            try:
                errors = apply_edits(trial, [edit], job.targets, job.classes, job.members, job.others)
            except EditError as e:
                errors = [str(e)]
            if errors:
                return "Error: " + "; ".join(errors) + "\nNothing was changed."
            if trial.files == ws.files:
                return "That edit changes nothing."
            ws.files, ws.log = trial.files, ws.log + trial.log
            t = find(fn) if fn not in ("(new method)", "(new function)") else None
            shown = code_block(ws.text, t)[0] if t else ""
            return f"Done: {trial.log[-1]}." + (f" The function now reads:\n{shown[:1200]}" if shown else "")
        return f"Unknown tool {name!r}."

    for step in range(prof.tool_steps):
        r = llm.chat(evict(messages, budget), tools=tool_specs(job.allowed), max_tokens=prof.answer_tokens)
        calls = r.get("tool_calls") or []
        msg = {"role": "assistant", "content": r.get("content", "")}
        if calls:
            msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("arguments") or {}}} for c in calls]
        messages.append(msg)
        if not calls:
            nudges += 1
            if nudges >= MAX_NUDGES:
                job.log(f"  turn {step}: no tool call {nudges} times in a row, stopping")
                break
            messages.append({"role": "user", "content": prompts.TOOL_NUDGE})
            continue
        nudges = 0
        for c in calls[:3]:
            out = execute(c, step)
            messages.append({"role": "tool", "content": out[:TOOL_OUT_CHARS], "tool_name": c.get("name", "")})
            if done:
                break
        if done:
            break
    if ws.diff() and ws.diff() != checked_diff:   # edits made but never checked: check them now
        run_check(prof.tool_steps)
    return attempts, best
