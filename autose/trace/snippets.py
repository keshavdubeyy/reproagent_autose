"""Pull runnable statements out of a bug report: fenced code blocks, doctest (>>>) and IPython
(In [n]:) sessions, and inline `code` in the steps to reproduce. Each returned string parses
as Python; output lines and prose are dropped."""
from __future__ import annotations

import ast
import re
import textwrap

FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
INLINE = re.compile(r"`([^`\n]{3,})`")
PROMPT = re.compile(r"^\s*(>>>|\.\.\.(?=\s|$)|In \[\d+\]:|\.\.\.:) ?")
OUT_PROMPT = re.compile(r"^\s*Out\[\d+\]:")
STEP_START = re.compile(r"(?:^|:\s+|\b(?:Execute|Run|Call|Evaluate|Compute|Type|Use)\s+)(?=(?:from|import|print)\b|\w[\w.]*\s*=[^=]|\w[\w.]*\()")


def _step_code(line: str) -> str | None:
    """Unquoted code in a step ('Run print(f(x)) and observe ...'): the longest parsing prefix
    after the verb or colon, ending at a ')' or at the end of the line."""
    for m in STEP_START.finditer(line):
        rest = line[m.end():].rstrip(" .")
        ends = [len(rest)] + [i + 1 for i in range(len(rest) - 1, 0, -1) if rest[i] == ")"]
        for end in ends:
            code = rest[:end].strip()
            if _parses(code) and _looks_like_code(code) and not re.search(r"\b(and|the|then|observe)\b", code):
                return code
    return None


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except (SyntaxError, ValueError):
        return False


def _looks_like_code(code: str) -> bool:
    return bool(re.search(r"[(=\[]|^\s*(from|import)\s", code))


def _from_session(block: str) -> str | None:
    lines = block.splitlines()
    if not any(PROMPT.match(l) for l in lines):
        return None
    keep = []
    for l in lines:
        m = PROMPT.match(l)
        if m:
            keep.append(l[m.end():])
        elif OUT_PROMPT.match(l):
            continue
    return "\n".join(keep)


def _split(code: str) -> list[str]:
    """Whole block if it parses; otherwise the longest runs of lines that do."""
    code = textwrap.dedent(code).strip("\n")
    if not code.strip():
        return []
    if _parses(code):
        return [code]
    out, buf = [], []
    for line in code.splitlines():
        buf.append(line)
        chunk = textwrap.dedent("\n".join(buf))
        if _parses(chunk):
            continue
        buf.pop()
        if buf:
            out.append(textwrap.dedent("\n".join(buf)))
        buf = [line] if _parses(textwrap.dedent(line)) else []
    if buf:
        out.append(textwrap.dedent("\n".join(buf)))
    return [c for c in out if c.strip() and _looks_like_code(c)]


def statements(bug_report) -> list[str]:
    fields = bug_report if isinstance(bug_report, dict) else {"Description": str(bug_report)}
    out: list[str] = []
    for name in ("Description", "StepsToReproduce", "ObservedBehavior", "ExpectedBehavior"):
        value = fields.get(name) or ""
        text = "\n".join(map(str, value)) if isinstance(value, list) else str(value)
        for block in FENCE.findall(text):
            session = _from_session(block)
            out += _split(session if session is not None else block)
        rest = FENCE.sub("\n", text)
        session = _from_session(rest)
        if session:
            out += _split(session)
        if name == "StepsToReproduce":
            for line in rest.splitlines():
                spans = [s for s in INLINE.findall(line) if _parses(s.strip()) and _looks_like_code(s)]
                if spans:
                    out += [s.strip() for s in spans]
                    continue
                code = _step_code(line.strip())
                if code:
                    out.append(code)
    out = list(dict.fromkeys(c for c in out if c.strip()))
    # imports first: the steps often import what the description's examples already use
    is_import = lambda c: all(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.parse(c).body)
    return [c for c in out if is_import(c)] + [c for c in out if not is_import(c)]
