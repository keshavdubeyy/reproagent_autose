"""Layer 3: explorer -> final report -> reviewer -> problem location, for one bug.

Port of context-engineering-v2@1c65307 src/agents/multi_agent.py (run_for_instance,
run_explorer_agent, run_reviewer_agent) and src/problem_location.py.

AUTOSE changes (for a 4B local model, 8K context):
  1. The harness drives the 5 checkpoints in order (1, 2-3, 4, 5), each a short tool loop with
     its own budget, instead of one 100-step agent that the original re-prompted when it
     stopped early (_detect_completed_checkpoints / _build_continuation_prompt).
  2. Checkpoints pass on their closing notes, not the whole trajectory. That replaces the
     original's separate compress_chat_history call; the notes go to the report and reviewer.
  3. The explorer starts from Layer 2's ranked candidates (instead of classify_report), and the
     harness hands it the primary file's outline and call graph from the index.
  4. If the model names no valid PRIMARY, the top Layer 2 candidate is used and recorded.
  5. Old tool outputs are shortened when a checkpoint's messages near the context limit.
Inputs are public only: the bug report, the snapshot, repository_index.json, retrieval.json.
"""
from __future__ import annotations

import builtins
import math
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from autose.agents import prompts
from autose.agents.json_parser import parse_json_best_effort
from autose.agents.llm import LLM
from autose.agents.tools import SPECS, Tools
from autose.retrieval.mentions import report_text
from autose.trace import rank as trace_rank

BUDGET = {"cp1": 4, "cp23": 3, "cp4": 3, "reviewer": 3}

# Ollama structured output: the model can only emit JSON of these shapes
_STR, _LIST = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
REPORT_SCHEMA = {"type": "object", "properties": {
    "Title": _STR, "Description": _STR, "RootCause": _STR, "StepsToReproduce": _LIST,
    "ExpectedBehavior": _STR, "ObservedBehavior": _STR},
    "required": ["Title", "Description", "RootCause", "StepsToReproduce", "ExpectedBehavior", "ObservedBehavior"]}
REVIEW_SCHEMA = {"type": "object", "properties": {"revised_report": REPORT_SCHEMA, "changes": _LIST, "evidence": _LIST},
                 "required": ["revised_report", "changes", "evidence"]}
PL_SCHEMA = {"type": "object", "properties": {"problem_location": {"type": "object", "properties": {
    "files": _LIST, "classes": _LIST, "methods": _LIST}, "required": ["files", "classes", "methods"]}},
    "required": ["problem_location"]}
N_CANDIDATES = 8
CONTEXT_CHARS = 22000  # ~6.5K tokens: leave room for the answer in an 8K context
OLD_TOOL_CHARS = 400


def _shrink(messages: list[dict]) -> None:
    """Shorten the oldest tool outputs until the messages fit."""
    for m in messages:
        if sum(len(x["content"]) for x in messages) <= CONTEXT_CHARS:
            return
        if m["role"] == "tool" and len(m["content"]) > OLD_TOOL_CHARS:
            m["content"] = m["content"][:OLD_TOOL_CHARS] + "\n[... shortened to save context]"


def tool_loop(llm: LLM, tools: Tools, system: str, user: str, budget: int, log: list, stage: str,
              final_fmt: str | None = None) -> str:
    """Let the model call tools up to `budget` times, then make it answer without tools."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    used = 0
    for _ in range(budget + 3):
        _shrink(messages)
        allow = used < budget
        r = llm.chat(messages, SPECS if allow else None, fmt=None if allow else final_fmt)
        messages.append({"role": "assistant", "content": r["content"],
                         **({"tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for c in r["tool_calls"]]} if r["tool_calls"] else {})})
        log.append({"stage": stage, "role": "assistant", "content": r["content"],
                    "tool_calls": r["tool_calls"], "prompt_tokens": r["prompt_tokens"]})
        if not r["tool_calls"]:
            return r["content"]
        for c in r["tool_calls"][: max(budget - used, 0)]:
            out = tools.call(c["name"], c["arguments"])
            used += 1
            messages.append({"role": "tool", "content": out, "tool_name": c["name"]})
            log.append({"stage": stage, "role": "tool", "tool": c["name"], "args": c["arguments"], "chars": len(out)})
        if used >= budget:
            messages.append({"role": "user", "content": "Tool budget used. Finish this checkpoint now with its required lines."})
    return llm.chat(messages, fmt=final_fmt)["content"]


def outline(index: dict, file: str) -> str:
    rows = []
    for e in sorted((e for e in index["entities"] if e["file"] == file), key=lambda e: e["start_line"]):
        depth = e["qualified_name"].count(".") - e["file"].count("/") - 1
        bases = f"({', '.join(e.get('bases', []))})" if e["kind"] == "class" and e.get("bases") else ""
        rows.append(f"{'  ' * max(depth, 0)}{e['kind']} {e['name']}{bases}  [lines {e['start_line']}-{e['end_line']}]")
    text = "\n".join(rows)
    return text if len(text) < 3500 else text[:3500] + "\n[... outline truncated]"


ERROR_LINE = re.compile(r"\b\w*(?:Error|Exception|Warning)\b[^\n]*")
QUOTED = re.compile(r"['\"`]([A-Za-z_][\w.]{2,})['\"`]")
FRAME_FN = re.compile(r'File "[^"]+", line \d+, in (\w+)')
PY_PATH = re.compile(r"\b([\w./-]+\.py)\b")
BUILTIN_ERRORS = {n for n in dir(builtins) if n.endswith(("Error", "Exception", "Warning"))}
MAX_TERM_HITS = 25


def report_leads(report: str, tools: Tools, max_terms: int = 4) -> tuple[str, list[str]]:
    """Searches a developer would run first: names inside error messages and traceback frames,
    and the files the report names (with the closest real file when the name is stale).
    Returns (text for the prompt, entity ids that enclose the hits)."""
    terms = []
    for line in ERROR_LINE.findall(report):
        terms += [t for t in QUOTED.findall(line) if t not in BUILTIN_ERRORS]
        terms += [t for t in re.findall(r"\b([A-Z]\w+(?:Error|Exception|Warning))\b", line) if t not in BUILTIN_ERRORS]
    terms += FRAME_FN.findall(report)
    terms = list(dict.fromkeys(t.split(".")[-1] for t in terms if len(t.split(".")[-1]) > 2))[:max_terms]
    rows, ids = [], []
    for term in terms:
        before = len(tools.hit_entities)
        out = tools.search_codebase(rf"\b{re.escape(term)}\b")
        source = [l for l in out.splitlines() if not l.startswith("tests/") and "[in " in l]
        if not source or len(out.splitlines()) >= MAX_TERM_HITS:  # nothing, or too common to help
            del tools.hit_entities[before:]
            continue
        rows.append(f"search {term!r}:\n" + "\n".join(f"  {l}" for l in source[:6]))
        ids += tools.hit_entities[before:]
    for path in dict.fromkeys(PY_PATH.findall(report)):
        real = tools.resolver.file(path)
        if real:
            rows.append(f"file {path}: {real}")
        elif near := tools.suggest(path, 3):
            rows.append(f"file {path}: not in this version; closest: {', '.join(near)}")
    tools.calls.clear()  # harness searches are not the model's tool calls
    return ("\n".join(rows) or "(none)"), list(dict.fromkeys(ids))


MAX_CHOICES = 40
GENERIC = {"print", "init", "get", "set", "self", "eval", "call", "new", "str", "repr", "doprint", "the", "and", "for"}


def entry_classes(cands: list[str], tools: Tools, look: int = 5, per: int = 2, total: int = 4) -> list[str]:
    """Candidates with the classes their top functions instantiate inserted after them: reports
    name the user-facing function (ccode, pairplot), the bug lives in the class behind it."""
    out, added = [], 0
    for i, c in enumerate(cands):
        out.append(c)
        if i >= look or added >= total or tools.entities[c]["kind"] == "class":
            continue
        targets = [r["target"] for r in tools.out.get(c, []) if r["type"] == "invokes" and r.get("resolution") == "scoped"
                   and tools.entities.get(r["target"], {}).get("kind") == "class"
                   and not tools.entities[r["target"]]["is_test"]]
        for t in [t for t in dict.fromkeys(targets) if t not in cands and t not in out][:per]:
            if added < total:
                out.append(t)
                added += 1
    return out


def sibling_gaps(ids: list[str], tools: Tools, report: str, max_rows: int = 8) -> str:
    """CE-v2 checkpoint 3c done by the harness: in the class families around `ids` (a class, its
    parents' other subclasses), which report-related methods some siblings define and others lack."""
    from autose.retrieval.lexical import tokenize
    words = {w for w in tokenize(report) if len(w) >= 3 and w not in GENERIC}
    ent = tools.entities
    if not hasattr(tools, "_name_df"):  # how many entity names contain each word: rare words matter
        df = defaultdict(int)
        for e in ent.values():
            for p in set(tokenize(e["name"])):
                df[p] += 1
        tools._name_df = df
    rarity = lambda p: math.log(len(ent) / (1 + tools._name_df.get(p, 0)))
    rows, seen_families = [], set()
    for i in ids:
        cls = i if ent[i]["kind"] == "class" else ent[i]["parent"] if ent.get(ent[i]["parent"], {}).get("kind") == "class" else None
        if not cls:
            continue
        parents = [r["target"] for r in tools.out.get(cls, []) if r["type"] == "inherits"]
        family = {cls} | {r["source"] for p in parents for r in tools.inn.get(p, []) if r["type"] == "inherits"}
        family = sorted(c for c in family if c in ent and not ent[c]["is_test"])
        key = tuple(family)
        if len(family) < 2 or key in seen_families:
            continue
        seen_families.add(key)
        methods = {c: {ent[m]["name"] for m in tools.by_children.get(c, [])} for c in family}
        for name in sorted(set().union(*methods.values())):
            parts = {p for p in tokenize(name) if len(p) >= 3 and p not in GENERIC}
            have = [c for c in family if name in methods[c]]
            if not parts & words or len(have) == len(family):
                continue
            lack = [c for c in family if name not in methods[c]]
            short = lambda cs: ", ".join(c.split(":")[-1] for c in cs[:5]) + (" ..." if len(cs) > 5 else "")
            rows.append((max(rarity(p) for p in parts & words),
                         f"- {name}: defined in {short(have)}; missing in {short(lack)}"))
    rows.sort(key=lambda r: -r[0])
    return "\n".join(r[1] for r in rows[:max_rows])


def choose_primary(llm: LLM, tools: Tools, report: str, conclusion: str, pool: list[str],
                   stated: str | None, log: list) -> list[str]:
    """Constrained choice: the model can only answer with ids it was shown or read (an enum in
    the JSON schema), so it can neither skip the answer nor invent a location."""
    seen = [i for i in tools.read if i in tools.entities]
    options = list(dict.fromkeys(([stated] if stated else []) + seen + pool + tools.hit_entities))
    options = [o for o in options if not tools.entities[o]["is_test"]][:MAX_CHOICES]
    if not options:
        return []
    schema = {"type": "object", "properties": {"ranked": {"type": "array", "items": {"type": "string", "enum": options},
                                                          "minItems": 1, "maxItems": 3}},
              "required": ["ranked"]}
    listing = "\n".join(f"- {o}" for o in options)
    r = llm.chat([{"role": "user", "content": prompts.CHOOSE_PRIMARY.format(report=report[:3000], conclusion=conclusion[-1500:])
                   + f"\n\nAllowed ids:\n{listing}"}], max_tokens=200, fmt=schema)
    got = (parse_json_best_effort(r["content"], preferred_keys=["ranked"]) or {}).get("ranked", [])
    ranked = list(dict.fromkeys(x for x in got if x in options))[:3]
    log.append({"stage": "choose_primary", "role": "assistant", "content": r["content"], "stated": stated,
                "options": len(options)})
    return ranked


N_RIVALS = 3
RIVAL_CHARS = 1100


def rivals(primary: str, layer2_all: list[str], tools: Tools, report: str, n: int = N_RIVALS) -> list[str]:
    """CE-v2 checkpoint 4 asks the model to check nearby code; the 4B model rarely reads it, so
    the harness picks it: functions inside the primary (a class's methods, nested functions),
    then other functions in the same file; ranked by Layer 2 rank, then by words shared with
    the report (name words count 3x)."""
    from autose.retrieval.lexical import tokenize
    e = tools.entities[primary]
    words = {w for w in tokenize(report) if len(w) >= 3 and w not in GENERIC}
    rank = {x: i for i, x in enumerate(layer2_all)}

    def fns(ids):
        return [i for i in dict.fromkeys(ids) if i != primary and i in tools.entities
                and tools.entities[i]["kind"] != "class" and not tools.entities[i]["is_test"]]

    def key(i):
        x = tools.entities[i]
        try:
            body = set(tokenize("\n".join(tools.lines(x["file"])[x["start_line"] - 1:x["end_line"]])))
        except FileNotFoundError:
            body = set()
        return (rank.get(i, 10 ** 6), -(3 * len(set(tokenize(x["qualified_name"])) & words) + len(body & words)),
                x["start_line"])

    inside = sorted(fns(tools.by_children.get(primary, [])), key=key)
    near = sorted(fns(x["id"] for x in tools.by_file.get(e["file"], [])), key=key)
    first = inside[:n] if e["kind"] == "class" else inside[:1]
    return list(dict.fromkeys(first + near))[:n]


def harness_code(tools: Tools, eid: str, limit: int = RIVAL_CHARS) -> str:
    """Read code for the model without counting it as the model's read."""
    had = eid in tools.read
    out = tools.get_code(eid)
    if not had:
        tools.read.discard(eid)
    return out if len(out) <= limit else out[:limit] + "\n[... cut]"


def choose_edit(llm: LLM, tools: Tools, report: str, conclusion: str, pool: list[str], log: list) -> list[str]:
    """After checkpoint 4: which functions the fix edits, constrained to real non-test ids."""
    options = [o for o in dict.fromkeys(pool) if o in tools.entities and not tools.entities[o]["is_test"]][:MAX_CHOICES]
    if not options:
        return []
    schema = {"type": "object", "properties": {"edit": {"type": "array", "items": {"type": "string", "enum": options},
                                                        "minItems": 1, "maxItems": 3}},
              "required": ["edit"]}
    listing = "\n".join(f"- {o}" for o in options)
    r = llm.chat([{"role": "user", "content": prompts.CHOOSE_EDIT.format(report=report[:3000], conclusion=conclusion[-1500:])
                   + f"\n\nAllowed ids:\n{listing}"}], max_tokens=200, fmt=schema)
    got = (parse_json_best_effort(r["content"], preferred_keys=["edit"]) or {}).get("edit", [])
    edit = list(dict.fromkeys(x for x in got if x in options))[:3]
    log.append({"stage": "choose_edit", "role": "assistant", "content": r["content"], "options": len(options)})
    return edit


def keep_ids(primary: str | None, edit: list[str]) -> list[str]:
    """Locations the harness always reports: the top 2 edit choices, and the primary unless an
    edit choice lies inside it (a class whose method was chosen)."""
    keep = list(edit[:2])
    if primary and not any(k == primary or k.startswith(primary + ".") for k in keep):
        keep.append(primary)
    return keep


def snap_locations(pl: dict, tools: Tools, prefer: list[str], keep: list[str] | None = None) -> dict:
    """Map every named file / class / method to a real index entity (fixing stale or invented
    paths such as 'pkg/x/y.py: Cls.meth'); names that match nothing are dropped and recorded."""
    prefer_set = set(prefer)
    classes = [str(c) for c in pl.get("classes", []) if c]
    named_files = {f for f in (tools.resolver.file(str(x)) for x in pl.get("files", [])) if f}
    if not classes and named_files:  # no class named: use the preferred classes in the named files
        classes = [p for p in prefer if p in tools.entities and tools.entities[p]["kind"] == "class"
                   and tools.entities[p]["file"] in named_files][:1]
    ents, dropped, new_methods = [], [], []

    def resolve(name: str) -> str | None:
        name = re.sub(r"[`*]", "", name).strip().replace(": ", ":").replace("()", "")
        name = re.sub(r"(^|[.:])_{3,}(\w+?)_{3,}$", r"\1__\2__", name)  # markdown: ___init___
        tail = name.split(":")[-1]
        bare = "." not in tail and ":" not in name
        owners = []
        if bare:  # a bare method name: first look it up exactly on the classes the report named
            for cls in classes:
                cid = cls if cls in tools.entities else tools.find(cls.replace(" ", ""))[0]
                if cid and tools.entities[cid]["kind"] == "class":
                    owners.append(cid)
                    if f"{cid}.{tail}" in tools.entities:
                        return f"{cid}.{tail}"
        eid, cands = tools.find(name)
        if not eid and cands:
            hits = [c for c in cands if c in prefer_set]
            eid = hits[0] if len(hits) == 1 else None
        if eid and "." in tail:  # the name's own Class.method must match, not just its last part
            want = ".".join(tail.split(".")[-2:])
            if not tools.entities[eid]["qualified_name"].endswith(want):
                eid = None
        if eid and bare and named_files and tools.entities[eid]["file"] not in named_files:
            eid = None  # a bare name found only outside the files the report names
        if not eid and bare and len(owners) == 1:  # a bare new method on the one named class
            new_methods.append(f"{owners[0]}.{tail}")
            return owners[0]
        if not eid and "." in tail:  # Class.method where the class exists but the method does not:
            owner, _ = tools.find(name.rsplit(".", 1)[0])  # a method the fix must add (keep the class)
            if owner and tools.entities[owner]["kind"] == "class":
                new_methods.append(f"{owner}.{tail.rsplit('.', 1)[1]}")
                return owner
        if not eid and not bare:  # a stale or misspelt qualified name: closest real entity
            near = tools.suggest(name, 1)
            eid = near[0] if near and near[0] in tools.entities else None
        return eid

    for kind in ("methods", "classes"):
        for name in pl.get(kind, []):
            eid = resolve(str(name))
            (ents.append(eid) if eid else dropped.append(str(name)))
    ents = [e for e in dict.fromkeys(ents + [k for k in keep or [] if k in tools.entities])
            if not tools.entities[e]["is_test"]]
    files = [f for f in (tools.resolver.file(str(x)) for x in pl.get("files", [])) if f]
    files = list(dict.fromkeys(files + [tools.entities[e]["file"] for e in ents]))
    kind = lambda e: tools.entities[e]["kind"]
    return {"files": files,
            "classes": list(dict.fromkeys(
                [tools.entities[e]["qualified_name"] for e in ents if kind(e) == "class"]
                + [tools.entities[tools.entities[e]["parent"]]["qualified_name"] for e in ents
                   if kind(e) == "method" and tools.entities[e]["parent"] in tools.entities])),
            "methods": [tools.entities[e]["qualified_name"] for e in ents if kind(e) != "class"]
                       + [f"{tools.entities[m.rsplit('.', 1)[0]]['qualified_name']}.{m.rsplit('.', 1)[1]}"
                          for m in new_methods],
            "entities": ents, "new_methods": new_methods, "dropped": dropped}


def finalise_locations(raw_pl: dict, tools: Tools, primary: str | None, ranked: list[str], read: list[str],
                       cands: list[str], edit: list[str] | None = None) -> dict:
    """Snap the extracted locations and add the harness's kept ids (primary, edit choices), so a
    right pick is never lost to the extraction step; if extraction found nothing real, fall back
    to the agent's own ranked choice from checkpoint 1 (recorded in 'source')."""
    edit = edit or []
    keep = keep_ids(primary, edit)
    snapped = snap_locations(raw_pl, tools, ([primary] if primary else []) + edit + ranked + read + cands)
    if snapped["entities"] or snapped["new_methods"]:
        return snap_locations(raw_pl, tools, ([primary] if primary else []) + edit + ranked + read + cands,
                              keep) | {"source": "report", "kept": keep}
    fallback = list(dict.fromkeys(keep + ranked))[:3]
    names = {"methods": [tools.entities[e]["qualified_name"] for e in fallback if tools.entities[e]["kind"] != "class"],
             "classes": [tools.entities[e]["qualified_name"] for e in fallback if tools.entities[e]["kind"] == "class"],
             "files": []}
    return snap_locations(names, tools, fallback, fallback) | {"source": "ranked_fallback", "kept": keep}


def _after(marker: str, text: str) -> str:
    m = re.search(rf"{marker}\s*(.+)", text)
    return m.group(1).strip() if m else ""


def run_instance(bug_report, index: dict, code_dir: str | Path, retrieval: dict, llm: LLM,
                 trace: dict | None = None) -> dict:
    """trace: workspaces/<id>/trace.json from autose.trace (optional execution evidence)."""
    t0 = time.time()
    tools = Tools(index, code_dir)
    report = report_text(bug_report)
    log: list = []

    layer2 = [e["id"] for e in retrieval["entities"] if e["id"] in tools.entities]
    ran: set[str] = set()
    if trace and trace.get("status") == "ran":  # execution evidence: fuse the trace into the ranking
        ran = set(trace.get("executed", {}))
        frames = [f for f in trace.get("frames", []) if f in tools.entities and not tools.entities[f]["is_test"]]
        layer2 = trace_rank.fuse(layer2, trace_rank.ranking(trace, report, index, code_dir), frames)
    leads_text, lead_ids = report_leads(report, tools)
    if trace and trace.get("frames"):
        lead_ids = list(dict.fromkeys(lead_ids + [f for f in reversed(trace["frames"]) if f in tools.entities]))
    cands = entry_classes(list(dict.fromkeys(layer2[:N_CANDIDATES])), tools)
    mark = lambda c: " [ran]" if c in ran else ""
    cand_text = "\n".join(f"{i + 1}. {c}{mark(c)}  |  {tools.signature(c)[:120]}" for i, c in enumerate(cands))
    trace_text = trace_rank.summary(trace) if trace else "(the report's code was not run)"

    cp1 = tool_loop(llm, tools, prompts.SYSTEM, prompts.CP1.format(report=report, candidates=cand_text,
                                                                    leads=leads_text, trace=trace_text),
                    BUDGET["cp1"], log, "cp1")
    stated, _ = tools.find(_after("PRIMARY:", cp1).split()[0] if _after("PRIMARY:", cp1) else "")
    ranked = choose_primary(llm, tools, report, cp1, cands + lead_ids, stated, log)
    primary, primary_source = (ranked[0], "model") if ranked else ((cands[0], "layer2_fallback") if cands else (None, "none"))
    notes = [f"Checkpoint 1: {cp1.split('CHECKPOINT 1 DONE')[-1].strip() or cp1[-600:]}"
             + (f"\nRanked locations: {ranked}" if ranked else "")]

    siblings, near, edit = "", [], []
    if primary:
        file = tools.entities[primary]["file"]
        siblings = sibling_gaps(list(dict.fromkeys([primary] + ranked + cands[:6])), tools, report)
        cp23 = tool_loop(llm, tools, prompts.SYSTEM, prompts.CP23.format(
            report=report, notes="\n".join(notes), file=file, outline=outline(index, file),
            primary=primary, subgraph=tools.get_subgraph(primary), siblings=siblings or "(none found)"),
            BUDGET["cp23"], log, "cp23")
        notes.append(f"Checkpoints 2-3: {cp23.split('CHECKPOINT 3 DONE')[-1].strip() or cp23[-1200:]}")
        code = tools.get_code(primary)
        near = rivals(primary, layer2, tools, report)
        near_text = "\n\n".join(harness_code(tools, r) for r in near) or "(none)"
        others = sorted((c for c in cands if c != primary and c not in near),
                        key=lambda c: tools.entities[c]["file"] != file)
        others_text = "\n".join(f"- {c}  |  {tools.signature(c)[:120]}" for c in others[:6]) or "(none)"
        cp4 = tool_loop(llm, tools, prompts.SYSTEM, prompts.CP4.format(
            report=report, notes="\n".join(notes), primary=primary, code=code[:3000], rivals=near_text,
            others=others_text, siblings=siblings or "(none found)"), BUDGET["cp4"], log, "cp4")
        notes.append(f"Checkpoint 4: {cp4[-2500:]}")
        edit = choose_edit(llm, tools, report, cp4, [primary] + near + tools.by_children.get(primary, [])
                           + ranked + [i for i in tools.read if i in tools.entities] + others, log)
    analysis = llm.chat([{"role": "system", "content": prompts.SYSTEM},
                         {"role": "user", "content": prompts.CP5.format(report=report, notes="\n\n".join(notes))}],
                        max_tokens=900)["content"]
    log.append({"stage": "cp5", "role": "assistant", "content": analysis})

    draft_raw = llm.chat([{"role": "user", "content": prompts.REPORT.format(report=report, analysis=analysis)}],
                         max_tokens=900, fmt=REPORT_SCHEMA)["content"]
    draft = parse_json_best_effort(draft_raw, preferred_keys=["RootCause", "Title"]) or {"_raw_unparsed_report": draft_raw}

    draft_text = json.dumps(draft, ensure_ascii=False)[:5000]
    checks = tool_loop(llm, tools, prompts.REVIEWER_SYSTEM, prompts.REVIEWER.format(
        report=report, draft=draft_text, analysis=analysis[:4000], budget=BUDGET["reviewer"]),
        BUDGET["reviewer"], log, "reviewer")
    rev_raw = llm.chat([{"role": "system", "content": prompts.REVIEWER_SYSTEM},
                        {"role": "user", "content": prompts.REVIEWER_FINAL.format(draft=draft_text, checks=checks[-4000:])}],
                       max_tokens=1200, fmt=REVIEW_SCHEMA)["content"]
    rev = parse_json_best_effort(rev_raw, preferred_keys=["revised_report"]) or {}
    final = rev.get("revised_report") if isinstance(rev.get("revised_report"), dict) else draft

    pl_raw = llm.chat([{"role": "user", "content": prompts.PROBLEM_LOCATION.format(
        report=json.dumps(final, ensure_ascii=False)[:6000])}], max_tokens=400, fmt=PL_SCHEMA)["content"]
    pl = (parse_json_best_effort(pl_raw, preferred_keys=["problem_location"]) or {}).get("problem_location", {})
    if not isinstance(pl, dict):
        pl = {}
    raw_pl = {k: [str(x) for x in pl.get(k, []) if x] for k in ("files", "classes", "methods")}
    snapped = finalise_locations(raw_pl, tools, primary, ranked, sorted(tools.read), cands, edit)

    return {
        "primary": primary, "primary_source": primary_source, "primary_ranked": ranked, "primary_stated": stated,
        "harness_read": near, "edit_locations": edit,
        "trace_used": bool(ran), "ran_candidates": [c for c in cands if c in ran],
        "candidates": cands, "leads": lead_ids, "notes": notes, "analysis": analysis,
        "draft_report": draft, "bug_report": final,
        "reviewer_changes": rev.get("changes", []), "reviewer_evidence": rev.get("evidence", []),
        "problem_location": snapped, "problem_location_raw": raw_pl,
        "tool_calls": tools.calls, "read": sorted(tools.read), "trajectory": log,
        "usage": dict(llm.usage), "seconds": round(time.time() - t0, 1),
    }
