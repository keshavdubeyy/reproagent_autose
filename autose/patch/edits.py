"""Layer 4 edit tools: turn the model's answer into a checked change to one or more files.

Two edit forms, both limited to the code Layer 3 located:
  REWRITE  the model writes the whole new version of one function or method (a 4B model copes with
           this far better than with line numbers or exact-match diffs); the harness puts it back at
           the original indentation in place of the old one.
  SEARCH/REPLACE  for long functions: an exact block of existing lines and its replacement.
           Whitespace at line ends is ignored; a block that indents differently is re-indented.
Every edit is validated before it is accepted (AUTOSE, after the validated single-file edits of
AutoSE-Labs/autose DR-007, Apache-2.0): the target exists, the block matches exactly once, the
function keeps its name, and the edited file still parses as Python 3.9.
"""
from __future__ import annotations

import ast
import difflib
import re
import textwrap
from dataclasses import dataclass, field

FENCE = re.compile(r"```(?:python|py)?[^\n]*\n(.*?)```", re.S)
SR = re.compile(r"<{5,9} ?SEARCH[^\n]*\n(.*?)\n?={5,9}[^\n]*\n(.*?)\n?>{5,9} ?REPLACE", re.S)
PY = (3, 9)


class EditError(ValueError):
    """An edit the harness refused; the message goes back to the model."""


@dataclass
class Workspace:
    """Edited copies of files, in memory, over an untouched snapshot."""
    read: callable  # file -> text of the snapshot's file
    files: dict[str, str] = field(default_factory=dict)  # file -> edited text
    log: list[str] = field(default_factory=list)

    def text(self, file: str) -> str:
        return self.files[file] if file in self.files else self.read(file)

    def diff(self) -> str:
        return "".join(unified_diff(f, self.read(f), t) for f, t in sorted(self.files.items())
                       if t != self.read(f))


def parses(text: str, file: str = "<file>") -> None:
    try:
        ast.parse(text, file, feature_version=PY)
    except SyntaxError as e:
        raise EditError(f"the edited {file} no longer parses: {e.msg} (line {e.lineno})") from None


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _span(text: str, ent: dict, decorators: bool = True) -> tuple[int, int]:
    """0-based [start, end) of the entity's lines (with its decorators unless told not to), re-found
    by name so that an earlier edit to the same file that moved lines does not break it."""
    lines = text.splitlines(keepends=True)
    name = ent["name"].split(".")[-1]
    want = re.compile(rf"\s*(async\s+def|def|class)\s+{re.escape(name)}\b")
    starts = [i for i, l in enumerate(lines) if want.match(l)]
    if not starts:
        raise EditError(f"{ent['id']} is no longer in {ent['file']}")
    start = min(starts, key=lambda i: abs(i - (ent["start_line"] - 1)))
    tree = ast.parse("".join(lines), feature_version=PY)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.lineno - 1 == start:
            first = min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1
            return (first if decorators else node.lineno - 1), node.end_lineno
    raise EditError(f"could not find where {ent['id']} ends")


def rewrite(ws: Workspace, ent: dict, new_code: str) -> None:
    """Replace a whole function, method or class with the model's new version of it."""
    file = ent["file"]
    text = ws.text(file)
    lines = text.splitlines(keepends=True)
    code = textwrap.dedent(new_code.replace("\t", "    ")).strip("\n")
    try:
        body = ast.parse(code, feature_version=PY).body
    except SyntaxError as e:
        raise EditError(f"your code for {ent['name']} does not parse: {e.msg} (line {e.lineno})") from None
    name = ent["name"].split(".")[-1]
    defs = [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name]
    if not defs:
        raise EditError(f"write the complete new `def {name}` (or `class {name}`), starting with its def line")
    d = defs[0]
    # keep only that definition (no stray imports or examples around it); one written without
    # decorators keeps the original decorators
    first = min([x.lineno for x in d.decorator_list] + [d.lineno])
    code = "\n".join(code.splitlines()[first - 1:d.end_lineno])
    start, end = _span(text, ent, decorators=bool(d.decorator_list))
    pad = _indent(lines[start])
    new = "".join(pad + l if l.strip() else "\n" for l in (code + "\n").splitlines(keepends=True))
    edited = "".join(lines[:start]) + new + "".join(lines[end:])
    parses(edited, file)
    ws.files[file] = edited
    ws.log.append(f"rewrote {ent['id']}")


def search_replace(ws: Workspace, file: str, search: str, replace: str, within: tuple[int, int] | None = None) -> None:
    """Replace one exact block of lines (trailing whitespace ignored; re-indented if the block's
    indentation differs only by a constant). `within` limits the match to a 0-based line range."""
    text = ws.text(file)
    lines = text.splitlines(keepends=True)
    want = [l.rstrip() for l in search.strip("\n").splitlines()]
    if not any(want):
        raise EditError("the SEARCH block is empty")
    lo, hi = within or (0, len(lines))

    def matches(shift: str) -> list[int]:
        n = len(want)
        return [i for i in range(lo, max(lo, hi - n + 1))
                if all((shift + w if w else "") == lines[i + k].rstrip() for k, w in enumerate(want))]
    hits, shift = matches(""), ""
    if not hits:
        first = next(w for w in want if w)
        for i in range(lo, hi):
            if lines[i].strip() == first.strip():
                cand = _indent(lines[i])[:max(0, len(_indent(lines[i])) - len(_indent(first)))]
                if _indent(lines[i]) == cand + _indent(first) and matches(cand):
                    hits, shift = matches(cand), cand
                    break
    if not hits:
        first = next(w for w in want if w).strip()
        near = difflib.get_close_matches(first, [l.strip() for l in lines[lo:hi]], n=1, cutoff=0.5)
        hint = ""
        if near:
            j = lo + [l.strip() for l in lines[lo:hi]].index(near[0])
            hint = " The closest lines are:\n" + "".join(lines[j:j + 3]).rstrip("\n")
        raise EditError(f"the search text does not match any lines of {file} exactly; copy them from the "
                        f"code shown (a class shown only as an outline cannot be searched: rewrite or add a "
                        f"method instead).{hint}")
    if len(hits) > 1:
        raise EditError(f"the SEARCH block matches {len(hits)} places in {file}; include more lines to make it unique")
    i = hits[0]
    # the replacement's first line goes where the matched first line was; the rest keep their
    # indentation relative to it (models indent replacements inconsistently with the search)
    base = _indent(lines[i])
    new = replace.strip("\n").splitlines()
    r0 = next((_indent(l) for l in new if l.strip()), "")
    rep = "".join(("\n" if not l.strip() else base + (l[len(r0):] if l.startswith(r0) else l.lstrip()) + "\n")
                  for l in new) if replace.strip() else ""
    # a replacement that also repeats the lines right after the match (the search was only the
    # `if` line, the replacement the whole `if` block) replaces those lines too, not duplicates them
    end = i + len(want)
    def code_only(l: str) -> str:  # compare code, not spacing or comments
        return re.sub(r"\s+", "", l.split("#")[0])
    rl = [code_only(l) for l in new if l.strip()]
    for k in range(min(len(rl) - 1, len(lines) - end), 0, -1):
        if rl[-k:] == [code_only(l) for l in lines[end:end + k]] and all(rl[-k:]) and rl[:len(want)] != rl[-k:]:
            end += k
            break
    edited = "".join(lines[:i]) + rep + "".join(lines[end:])
    parses(edited, file)
    ws.files[file] = edited
    ws.log.append(f"replaced {len(want)} lines at {file}:{i + 1}")


def unified_diff(file: str, old: str, new: str) -> str:
    """A git-style diff that `git apply` and `patch -p1` accept."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    for x in (a, b):
        if x and not x[-1].endswith("\n"):
            x[-1] += "\n\\ No newline at end of file\n"
    body = "".join(difflib.unified_diff(a, b, f"a/{file}", f"b/{file}", n=3))
    return f"diff --git a/{file} b/{file}\n{body}" if body else ""


def add_member(ws: Workspace, cls: dict, code: str) -> None:
    """Add a new method (or nested class) at the end of a class body."""
    text = ws.text(cls["file"])
    start, end = _span(text, cls, decorators=False)
    lines = text.splitlines(keepends=True)
    body = [l for l in lines[start + 1:end] if l.strip()]
    pad = _indent(body[0]) if body else _indent(lines[start]) + "    "
    src = (textwrap.dedent(code).strip("\n") + "\n").splitlines(keepends=True)
    block = "".join(pad + l if l.strip() else "\n" for l in src)
    edited = "".join(lines[:end]) + "\n" + block + "".join(lines[end:])
    parses(edited, cls["file"])
    ws.files[cls["file"]] = edited
    ws.log.append(f"added to {cls['id']}")


def add_function(ws: Workspace, before: dict, code: str) -> None:
    """Add a new module-level function just above a located top-level function."""
    text = ws.text(before["file"])
    start, _ = _span(text, before)
    lines = text.splitlines(keepends=True)
    edited = "".join(lines[:start]) + textwrap.dedent(code).strip("\n") + "\n\n\n" + "".join(lines[start:])
    parses(edited, before["file"])
    ws.files[before["file"]] = edited
    ws.log.append(f"added a function above {before['id']}")


def add_imports(ws: Workspace, file: str, imports: list[str]) -> None:
    """Add module-level imports the new code needs, after the file's last top-level import."""
    text = ws.text(file)
    new = [i for i in imports if i not in text]
    if not new:
        return
    tree = ast.parse(text, feature_version=PY)
    last = max([n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))], default=0)
    lines = text.splitlines(keepends=True)
    edited = "".join(lines[:last]) + "".join(i + "\n" for i in new) + "".join(lines[last:])
    parses(edited, file)
    ws.files[file] = edited
    ws.log.append(f"imports added to {file}")


def _pieces(code: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(name, source) of each top-level def/class in a code block, and its top-level imports."""
    code = textwrap.dedent(code.replace("\t", "    ")).strip("\n")
    tree = ast.parse(code, feature_version=PY)
    lines = code.splitlines()
    defs, imports = [], []
    for n in tree.body:
        first = min([d.lineno for d in getattr(n, "decorator_list", [])] + [n.lineno])
        src = "\n".join(lines[first - 1:n.end_lineno])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append((n.name, src))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            imports.append(src)
    return defs, imports


def _top_level_function(targets: list[dict]) -> dict | None:
    return next((t for t in targets if t.get("kind") == "function" and not t.get("parent", "").count(":")), None)


def answer_edits(answer: str) -> list[dict]:
    """Edits written as text: SEARCH/REPLACE blocks if there are any, else the ```python blocks."""
    blocks = SR.findall(answer)
    if blocks:
        return [{"function": "", "search": s, "code": r} for s, r in blocks]
    return [{"function": "", "search": "", "code": c} for c in FENCE.findall(answer) if c.strip()]


def apply_edits(ws: Workspace, edits: list[dict], targets: list[dict], classes: list[dict] | None = None,
                members: dict[str, list[dict]] | None = None, others: set[str] | None = None) -> list[str]:
    """Apply edits {function, search, code}. With `search`, the exact lines are replaced by `code`
    inside the named function (else inside any target). Without, each def in `code` is matched by
    name: a target, else an existing method of a located class (classes / members), else it is
    added as a new method of the first located class, else as a new function above the first
    located top-level function. A name in `others` (code that exists but was not located) is
    refused rather than added; a def given twice is applied once. Returns the refusal messages
    (empty if all applied); edits that applied stay applied."""
    errors: list[str] = []
    classes, members = classes or [], members or {}
    if not edits:
        return ["no edit found"]
    names = ", ".join(t["name"] for t in targets)

    def find(name: str) -> dict | None:
        return next((t for t in targets if t["name"].split(".")[-1] == name), None) or \
            next((m for c in classes for m in members.get(c["id"], []) if m["name"] == name), None)
    done: set[str] = set()
    for ed in edits:
        code, search = ed.get("code") or "", ed.get("search") or ""
        if search.strip() and search.strip().startswith(("def ", "async def ", "@", "class ")):
            # the def line as `search` with the whole new function as `code` is a rewrite
            try:
                defs, _ = _pieces(code)
            except SyntaxError:
                defs = []
            if defs and find(defs[0][0]) is not None:
                search = ""
        if search.strip():
            named = find((ed.get("function") or "").split(".")[-1].split(":")[-1])
            last = "no location to edit"
            for t in ([named] if named else []) + [t for t in targets if t is not named]:
                try:
                    start, end = _span(ws.text(t["file"]), t)
                    search_replace(ws, t["file"], search, code, (start, end))
                    break
                except EditError as e:
                    last = str(e)
            else:
                errors.append(last)
            continue
        try:
            defs, imports = _pieces(code)
        except SyntaxError as e:
            errors.append(f"the code for {ed.get('function') or 'an edit'} does not parse: {e.msg} (line {e.lineno})")
            continue
        if not defs:
            errors.append("an edit has no def or class: write the complete function")
            continue
        files = []
        for name, src in defs:
            if name in done:
                continue
            t = find(name)
            try:
                if t is None and name in (others or set()):
                    raise EditError(f"`{name}` is not one of the functions to edit: {names}")
                if t is not None:
                    rewrite(ws, t, src)
                elif classes:
                    t = classes[0]
                    add_member(ws, t, src)
                else:
                    t = _top_level_function(targets)
                    if t is None:
                        raise EditError(f"`{name}` is not one of the functions to edit: {names}")
                    add_function(ws, t, src)
                done.add(name)
                files.append(t["file"])
            except EditError as e:
                errors.append(str(e))
        if imports and files:
            try:
                add_imports(ws, files[0], imports)
            except EditError as e:
                errors.append(str(e))
    return errors


def apply_answer(ws: Workspace, answer: str, targets: list[dict], classes: list[dict] | None = None,
                 members: dict[str, list[dict]] | None = None, others: set[str] | None = None) -> list[str]:
    """apply_edits for edits written as text (SEARCH/REPLACE or ```python blocks)."""
    edits = answer_edits(answer)
    if not edits:
        return ["no edit found: give the new code in a ```python block"]
    return apply_edits(ws, edits, targets, classes, members, others)
