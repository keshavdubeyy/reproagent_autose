"""Bounded context 2 (continued): Bug Resolution - fixing.

Added after the mid-term review (the proposal had fixing out of scope). Fixing starts only
after a reproduction test has been accepted: the test localises the fault and validates the patch.

* FaultLocalizer  - Function/Tool : ranks suspicious definitions from the failing reproduction test
                                    (project frames in the traceback, the API the test calls, one
                                    level of callees, the module constants they read) + the report.
* PatchGenerator  - AI capability : the SLM rewrites whole definitions (function / method / constant).
                                    Whole-definition replacement is far more robust for a 3B model
                                    than asking it for a unified diff with exact line numbers.
* PatchApplier    - Function/Tool : splices the returned definitions into the project at their AST
                                    positions, checks syntax and produces a unified diff.
"""
from __future__ import annotations

import ast
import difflib
import re
import textwrap
from collections import defaultdict
from pathlib import Path

from .llm import LLM, extract_code
from .models import BugReport, BugSpec, FaultCandidate, Patch, PatchAttempt
from .reproduction import RepoExplorer, _is_test_path
from .verification import TEST_FILE

MAX_DEF_LINES = 120
MAX_CONTEXT_CHARS = 9000


# ------------------------------------------------------------------ AST helpers (tool)
def _span(node: ast.AST) -> tuple[int, int, int]:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno, node.col_offset


def definitions(src: str) -> dict[str, tuple[int, int, int]]:
    """qualname -> (start line, end line, column) for top-level functions, classes, methods and
    module-level constants."""
    out: dict[str, tuple[int, int, int]] = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = _span(node)
        elif isinstance(node, ast.ClassDef):
            out[node.name] = _span(node)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = _span(sub)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    out[t.id] = _span(node)
    return out


def _enclosing(defs: dict, line: int) -> str | None:
    """Innermost function/method whose span contains `line`."""
    best = None
    for q, (s, e, _) in defs.items():
        if s <= line <= e and (best is None or (e - s) < (defs[best][1] - defs[best][0])):
            best = q
    return best


# ------------------------------------------------------------------ Fault Localizer (tool)
class FaultLocalizer:
    def __init__(self, repo: Path, explorer: RepoExplorer | None = None):
        self.repo = Path(repo)
        self.explorer = explorer or RepoExplorer(repo)
        if self.explorer._defs is None:
            self.explorer._index()
        self.mod2file: dict[str, str] = {}
        for d in self.explorer._defs:
            self.mod2file.setdefault(d.module, d.file)
        for m in self.explorer._modules:
            if m not in self.mod2file:
                for cand in (m.replace(".", "/") + ".py", m.replace(".", "/") + "/__init__.py"):
                    if (self.repo / cand).exists():
                        self.mod2file[m] = cand
        self.file2mod = {f: m for m, f in self.mod2file.items()}
        self._src: dict[str, str] = {}
        self._defs: dict[str, dict] = {}

    def src(self, rel: str) -> str:
        if rel not in self._src:
            self._src[rel] = (self.repo / rel).read_text(encoding="utf-8", errors="replace")
        return self._src[rel]

    def defs(self, rel: str) -> dict:
        if rel not in self._defs:
            self._defs[rel] = definitions(self.src(rel))
        return self._defs[rel]

    def _resolve(self, module: str, name: str) -> list[tuple[str, str]]:
        """(file, qualname) where `name` imported from `module` is really defined."""
        rel = self.mod2file.get(module)
        if rel and name in self.defs(rel):
            return [(rel, name)]
        top = module.split(".")[0]
        if module and not any(m == top or m.startswith(top + ".") for m in self.mod2file):
            return []    # not a project module (pytest, os, ...)
        out = []
        for dotted in self.explorer.locate(name):
            mod = dotted.rsplit(".", 1)[0]
            f = self.mod2file.get(mod)
            if f and not _is_test_path(f) and name in self.defs(f):
                out.append((f, name))
        return out[:2]

    def localize(self, report: BugReport, spec: BugSpec, test_code: str | None,
                 test_output: str | None, k: int = 6) -> list[FaultCandidate]:
        score: dict[tuple[str, str], float] = defaultdict(float)
        why: dict[tuple[str, str], str] = {}

        def add(key, s, reason):
            if key[1] is None:
                return
            score[key] += s
            why.setdefault(key, reason)

        # 1. project frames in the failing traceback (deepest frame = strongest signal)
        if test_output:
            frames = re.findall(r"^([^\s:][^:\n]*\.py):(\d+): in (\w+)", test_output, re.M)
            for depth, (path, line, _func) in enumerate(frames):
                rel = path.replace("\\", "/").lstrip("./")
                if rel.endswith(TEST_FILE) or rel not in self.file2mod or _is_test_path(rel):
                    continue
                q = _enclosing({q: v for q, v in self.defs(rel).items()
                                if not self._is_const(rel, q)}, int(line))
                add((rel, q), 10 + depth, f"in the traceback (line {line})")
        # 2. project API the reproduction test calls
        if test_code:
            for key, reason, role in self._called_by_test(test_code):
                if key[1].endswith("__init__"):
                    s = 2          # constructors are usually setup
                else:
                    s = {"asserted": 8, "last_action": 7}.get(role, 4)
                add(key, s, reason + {"asserted": " (its result is asserted)",
                                      "last_action": " (last action before the assert)"}.get(role, ""))
        # 3. symbols named in the report (functions, Class.method or bare method names)
        for sym in spec.symbols:
            leaf = sym.rsplit(".", 1)[-1]
            hits = self._resolve("", leaf)
            if not hits:
                hits = [(f, q) for f in self.file2mod for q in self.defs(f)
                        if q.endswith("." + leaf) and not _is_test_path(f)][:2]
            for rel, q in hits:
                add((rel, q), 4, "named in the bug report")
        # without a test (fix_no_repro): fall back to the explorer's report-based ranking
        if not test_code:
            for sn in self.explorer.search(spec, report, k=k).snippets:
                q = _enclosing(self.defs(sn.file), sn.lineno) if sn.file in self.file2mod else None
                add((sn.file, q or sn.name), sn.score, "matches the bug report")
        # 4. one level of callees of the best candidates (the bug is often one call deeper)
        words = _words(f"{report.title} {report.body} {spec.summary} {spec.expected}")
        for (rel, q), s in sorted(score.items(), key=lambda t: -t[1])[:5]:
            for key in self._callees(rel, q):
                if key not in score:
                    add(key, max(s / 2, 2.5), f"called by {q}")
        # report vocabulary in the definition name (e.g. 'discount' -> bulk_discount)
        for key in list(score):
            score[key] += 1.5 * len(_words(key[1]) & words)
        funcs = [(key, s) for key, s in score.items()
                 if key[1] in self.defs(key[0]) and not self._is_const(*key) and not self._is_class(*key)]
        funcs.sort(key=lambda t: -t[1])
        chosen, total = [], 0
        for (rel, q), s in funcs:
            c = self._candidate(rel, q, s, why.get((rel, q), ""))
            if total + len(c.source) > MAX_CONTEXT_CHARS and chosen:
                continue
            chosen.append(c)
            total += len(c.source)
            if len(chosen) >= k:
                break
        # 5. module-level constants read by the chosen functions (e.g. DISCOUNT_THRESHOLD)
        consts = []
        for c in list(chosen):
            for rel, q in self._constants_used(c):
                if not any(x.file == rel and x.name == q for x in chosen + consts):
                    consts.append(self._candidate(rel, q, c.score - 1, f"constant read by {c.name}"))
        return chosen + consts[:6]

    # -- helpers
    def _is_const(self, rel: str, q: str) -> bool:
        """Module-level assignment of a value (not an alias such as `tqdm = tqdm_gui`)."""
        try:
            s, e, _ = self.defs(rel)[q]
            first = self.src(rel).splitlines()[s - 1].lstrip()
        except (KeyError, IndexError):
            return False
        if first.startswith(("def ", "async def ", "class ", "@")):
            return False
        value = first.split("=", 1)[-1].strip()
        return (e - s) < 15 and not re.fullmatch(r"[A-Za-z_][\w.]*", value)

    def _is_class(self, rel: str, q: str) -> bool:
        s, _, _ = self.defs(rel)[q]
        return self.src(rel).splitlines()[s - 1].lstrip().startswith("class ")

    def _candidate(self, rel, q, s, reason) -> FaultCandidate:
        start, end, _ = self.defs(rel)[q]
        lines = self.src(rel).splitlines()[start - 1:end]
        if len(lines) > MAX_DEF_LINES:
            lines = lines[:MAX_DEF_LINES] + ["    # ... (truncated)"]
        return FaultCandidate(file=rel, module=self.file2mod.get(rel, ""), name=q, lineno=start,
                              end_lineno=end, source="\n".join(lines), score=round(s, 1), why=reason)

    def _called_by_test(self, code: str):
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return
        asserted_vars: set[str] = set()     # variables whose VALUE is compared (x == .., x[k] == ..)
        asserted_calls: set[int] = set()    # calls whose result is asserted / expected to raise
        last_action: set[int] = set()       # last statement-level project call before each assert

        def compared(e):
            while isinstance(e, ast.Subscript):
                e = e.value
            if isinstance(e, ast.Name):
                asserted_vars.add(e.id)

        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list):
                prev = None
                for st in body:
                    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                        prev = st.value
                    elif isinstance(st, ast.Assign) and isinstance(st.value, ast.Call):
                        prev = None    # `bill = checkout(cart)` is the action, judged by its own role
                    elif isinstance(st, ast.Assert) and prev is not None:
                        last_action.add(id(prev))
            if isinstance(node, ast.Assert):
                t = node.test
                if isinstance(t, ast.UnaryOp):
                    t = t.operand
                for e in ([t.left] + t.comparators) if isinstance(t, ast.Compare) else [t]:
                    compared(e)
                    if isinstance(e, ast.Call):
                        asserted_calls.add(id(e))
                for sub in ast.walk(node.test):
                    if isinstance(sub, ast.Call):
                        asserted_calls.add(id(sub))
            elif isinstance(node, ast.With) and any("raises" in ast.unparse(i.context_expr) for i in node.items):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and "raises" not in ast.unparse(sub.func):
                        asserted_calls.add(id(sub))
        for node in ast.walk(tree):   # x = f(...) ; assert x == ...
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in asserted_vars
                                                    for t in node.targets):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Call):
                        asserted_calls.add(id(sub))

        def role(call):
            return "asserted" if id(call) in asserted_calls else (
                "last_action" if id(call) in last_action else "setup")

        names: dict[str, tuple[str, str]] = {}    # local name -> (module, name)
        modules: dict[str, str] = {}               # alias -> module
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    if f"{node.module}.{a.name}" in self.mod2file:
                        modules[a.asname or a.name] = f"{node.module}.{a.name}"
                    else:
                        names[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    modules[a.asname or a.name] = a.name
        classes = {local: tgt for local, tgt in names.items() if tgt[1][:1].isupper()}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in names:
                mod, nm = names[f.id]
                for rel, q in self._resolve(mod, nm):
                    if self._is_class(rel, q) and f"{q}.__init__" in self.defs(rel):
                        q = f"{q}.__init__"
                    yield (rel, q), "called by the reproduction test", role(node)
            elif isinstance(f, ast.Attribute):
                base = f.value
                dotted = []
                while isinstance(base, ast.Attribute):
                    dotted.insert(0, base.attr)
                    base = base.value
                if isinstance(base, ast.Name) and base.id in modules:
                    mod = ".".join([modules[base.id]] + dotted)
                    for rel, q in self._resolve(mod, f.attr):
                        yield (rel, q), "called by the reproduction test", role(node)
                else:   # method call on an instance of an imported class, e.g. cart.set_quantity(...)
                    for mod, cls in classes.values():
                        for rel, q in self._resolve(mod, cls):
                            if f"{q}.{f.attr}" in self.defs(rel):
                                yield ((rel, f"{q}.{f.attr}"), "method called by the reproduction test",
                                       role(node))

    def _function_node(self, rel: str, q: str):
        start = self.defs(rel)[q][0]
        for node in ast.walk(ast.parse(self.src(rel))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _span(node)[0] == start:
                return node
        return None

    def _imports_of(self, rel: str) -> dict[str, tuple[str, str]]:
        """Names a module imports from other project modules: local -> (module, name)."""
        out = {}
        mod = self.file2mod.get(rel, "")
        pkg = mod if rel.endswith("__init__.py") else mod.rpartition(".")[0]
        for node in ast.parse(self.src(rel)).body:
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join(parts + ([node.module] if node.module else []))
                for a in node.names:
                    out[a.asname or a.name] = (base, a.name)
        return out

    def _callees(self, rel: str, q: str):
        node = self._function_node(rel, q) if q in self.defs(rel) else None
        if node is None:
            return
        imported = self._imports_of(rel)
        cls = q.split(".")[0] if "." in q else None
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Name):
                if f.id in self.defs(rel) and not self._is_class(rel, f.id):
                    yield (rel, f.id)
                elif f.id in imported:
                    for key in self._resolve(*imported[f.id]):
                        if not self._is_class(*key):
                            yield key
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self" \
                    and cls and f"{cls}.{f.attr}" in self.defs(rel):
                yield (rel, f"{cls}.{f.attr}")

    def _constants_used(self, c: FaultCandidate):
        node = self._function_node(c.file, c.name)
        if node is None:
            return
        imported = self._imports_of(c.file)
        seen = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id not in seen:
                seen.add(sub.id)
                if sub.id in self.defs(c.file) and self._is_const(c.file, sub.id):
                    yield (c.file, sub.id)
                elif sub.id in imported:
                    for key in self._resolve(*imported[sub.id]):
                        if self._is_const(*key):
                            yield key


# ------------------------------------------------------------------ Patch Generator (AI capability)
FIX_SYSTEM = """You are a careful Python developer fixing a reported bug in a project.
You get the bug report, a failing test that reproduces it, its failure output, and the suspicious project code.
Find the root cause and fix it in the PROJECT code. Never change or special-case the test.
Rules:
- Make the smallest change that fixes the root cause for ALL inputs, not only the test's values.
- Keep names and signatures unchanged. Do not delete behaviour that the report does not mention.
- Return ONLY the complete new version of each definition you change (a whole function, a whole
  method, or a module-level constant assignment), in ONE ```python block. Do not return unchanged code.
- For a method, write it inside its class header, e.g. `class Cart:` followed by the full method.
- If you need a new import, put the import line at the top of the block.
Start with one line `Root cause: ...`, then the code block."""

MULTI_RULE = """- The bug may need changes in MORE THAN ONE place: the same mistake in several functions, a caller AND
  the function it calls, or code AND a constant, possibly in different files. Check every symptom in the
  report and every failing test function, and return ALL definitions you change in the one block."""

FIX_SYSTEM_MULTI = FIX_SYSTEM.replace("- If you need a new import", MULTI_RULE + "\n- If you need a new import")

SWEEP_SYSTEM = """You review a bug fix in a Python project. A fix was applied in one place. The SAME mistake
may also exist in other definitions. You get the fix (a diff) and similar code from the project.
For each definition below that contains the SAME mistake, return its complete corrected version (methods
inside their `class X:` header), all in ONE ```python block. Change nothing else.
If none of them has the same mistake, answer exactly: NONE"""

FIX_SYSTEM_NO_TEST = FIX_SYSTEM.replace(
    "You get the bug report, a failing test that reproduces it, its failure output, and the suspicious "
    "project code.", "You get the bug report and the suspicious project code.").replace(
    " Never change or special-case the test.", "").replace(
    "not only the test's values", "not only the report's example")


class PatchGenerator:
    def __init__(self, llm: LLM):
        self.llm = llm

    def generate(self, report: BugReport, spec: BugSpec, candidates: list[FaultCandidate],
                 test_code: str | None, test_output: str | None,
                 history: list[PatchAttempt], attempt: int, multi: bool = False,
                 kept_diff: str = "") -> Patch:
        parts = [f"## Bug report\nTitle: {report.title}\n{report.body}",
                 f"## Expected behaviour\n{spec.expected}"]
        if test_code:
            parts.append(f"## Failing reproduction test (do NOT change it)\n```python\n{test_code.rstrip()}\n```")
            parts.append(f"## Its failure output on the current code\n```\n{_tail(test_output or '', 30)}\n```")
        if kept_diff:   # names only: showing a diff makes small models answer with a diff
            parts.append("## Already fixed and KEPT\nYour earlier change to " + kept_diff + " fixed part of the "
                         "bug and is already in the code below. Do not undo it. The REMAINING failure is in the "
                         "output above: it needs a change in another definition.")
        code = "\n\n".join(_render(c) for c in candidates)
        parts.append(f"## Suspicious project code (most suspicious first)\n```python\n{code}\n```")
        if history and history[-1].check.category != "partial":
            last = history[-1]
            parts.append(f"## Your previous patch (#{last.patch.attempt}) was REJECTED\n```python\n"
                         f"{last.patch.code.rstrip()}\n```\nProblem: {last.check.feedback}\n"
                         "Write a DIFFERENT patch that fixes this problem.")
        elif history:
            parts.append(f"## Progress\n{history[-1].check.feedback}\nNow fix the REMAINING failure.")
        parts.append("Fix the bug now.")
        system = (FIX_SYSTEM_MULTI if multi else FIX_SYSTEM) if test_code else FIX_SYSTEM_NO_TEST
        prompt = "\n\n".join(parts)
        seen = {re.sub(r"\s+", "", h.patch.code) for h in history}
        temp = 0.2 if attempt == 1 else 0.6
        for _ in range(2):   # regenerate once if the SLM repeats a rejected patch verbatim
            raw = self.llm.chat(system, prompt, temperature=temp)
            code = extract_code(raw)
            if re.sub(r"\s+", "", code) not in seen:
                break
            prompt += "\n\nYou returned exactly the same code as your rejected patch. Change the code that causes the bug."
            temp = 0.9
        return Patch(attempt=attempt, code=code, raw_llm_output=raw)

    def sweep(self, diff: str, siblings: list[FaultCandidate], attempt: int) -> Patch | None:
        """Consistency sweep: ask whether the same mistake exists in similar code. None = 'NONE'."""
        code = "\n\n".join(_render(c) for c in siblings)
        prompt = (f"## Fix that was applied\n```diff\n{_tail(diff, 40)}\n```\n\n"
                  f"## Similar code elsewhere in the project\n```python\n{code}\n```\n\n"
                  "Does any of these definitions contain the SAME mistake? Return the corrected definitions, or NONE.")
        raw = self.llm.chat(SWEEP_SYSTEM, prompt, temperature=0.2)
        code = extract_code(raw)
        if not code.strip() or code.strip().upper().startswith("NONE") or (
                "```" not in raw and "NONE" in raw.upper()):
            return None
        return Patch(attempt=attempt, code=code, raw_llm_output=raw, kind="sweep")


# ------------------------------------------------------------------ multi-location helpers (tools)
_TOK = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|==|!=|<=|>=|\*\*|//|->|[^\s\w]")
_KEEP = {"if", "elif", "else", "return", "not", "in", "is", "and", "or", "None", "True", "False", "for",
         "while", "raise", "lambda", "def", "class", "with", "try", "except", "yield", "await", "del"}


def _norm_tokens(line: str) -> list[str]:
    out = []
    for t in _TOK.findall(line.split("#", 1)[0]):
        if t in _KEEP:
            out.append(t)
        elif t[0].isalpha() or t[0] == "_":
            out.append("CONST" if t.isupper() and len(t) > 1 else "ID")
        elif t[0].isdigit():
            out.append("N")
        elif t[0] in "'\"":
            out.append("S")
        else:
            out.append(t)
    return out


def _contains(seq: list[str], sub: list[str]) -> bool:
    return any(seq[i:i + len(sub)] == sub for i in range(len(seq) - len(sub) + 1)) if sub else False


def changed_definitions(old_src: str, new_src: str) -> list[str]:
    """Qualnames whose source text differs between two versions of a file."""
    od, nd = definitions(old_src), definitions(new_src)
    ol, nl = old_src.splitlines(), new_src.splitlines()
    out = []
    for q, (s, e, _) in nd.items():
        if q in od:
            os_, oe, _ = od[q]
            if ol[os_ - 1:oe] != nl[s - 1:e]:
                out.append(q)
        else:
            out.append(q)
    # a changed method also changes its class: keep only the innermost names
    return [q for q in out if not any(o != q and o.startswith(q + ".") for o in out)]


def find_siblings(loc: FaultLocalizer, original: dict[str, str], patched: dict[str, str],
                  search_files: list[str], limit: int = 3) -> list[FaultCandidate]:
    """Definitions NOT changed by the patch that still contain the code pattern the patch replaced
    (e.g. the same `>` vs `>=` mistake in a sibling function). `original`/`patched`: rel -> source."""
    changes = []   # (old line tokens, removed token run, added token run)
    for rel, new in patched.items():
        old = original[rel]
        sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines())
        ol, nl = old.splitlines(), new.splitlines()
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            for r, a in zip(ol[i1:i2], nl[j1:j2]):
                rt, at = _norm_tokens(r), _norm_tokens(a)
                if len(rt) < 4 or rt == at or rt[0] in ("def", "class"):
                    continue
                m = difflib.SequenceMatcher(None, rt, at)
                ops = [o for o in m.get_opcodes() if o[0] != "equal"]
                rem = [t for _, x1, x2, _, _ in ops for t in rt[x1:x2]]
                add = [t for _, _, _, y1, y2 in ops for t in at[y1:y2]]
                x1, x2 = ops[0][1], ops[-1][2]
                ctx = (rt[x1 - 1] if x1 > 0 else None, rt[x2] if x2 < len(rt) else None)
                changes.append((rel, rt, rem, add, ctx))
    if not changes:
        return []
    done = {(rel, q) for rel, new in patched.items() for q in changed_definitions(original[rel], new)}
    hits: list[tuple[float, str, str]] = []
    for rel in dict.fromkeys(search_files):
        if _is_test_path(rel):
            continue
        src = loc.src(rel)
        lines = src.splitlines()
        for q, (s, e, _) in loc.defs(rel).items():
            if (rel, q) in done or loc._is_const(rel, q) or loc._is_class(rel, q):
                continue
            best = 0.0
            for line in lines[s - 1:e]:
                lt = _norm_tokens(line)
                if len(lt) < 3:
                    continue
                for prel, rt, rem, add, (before, after) in changes:
                    ratio = difflib.SequenceMatcher(None, rt, lt).ratio()
                    bonus = 0.2 if rel == prel else 0.0          # copy-paste mistakes are usually nearby
                    if rem and _contains(lt, rem) and not _contains(lt, add) and ratio >= 0.5:
                        ctx = max((0.2 * (i > 0 and lt[i - 1] == before) +
                                   0.2 * (i + len(rem) < len(lt) and lt[i + len(rem)] == after))
                                  for i in range(len(lt) - len(rem) + 1) if lt[i:i + len(rem)] == rem)
                        best = max(best, ratio + ctx + bonus)
                    elif not rem and add and not _contains(lt, add) and ratio >= 0.8:
                        best = max(best, ratio + bonus)
            if best:
                hits.append((best, rel, q))
    hits.sort(key=lambda h: -h[0])
    return [loc._candidate(rel, q, round(b, 2), "similar code to the fixed line (same mistake?)")
            for b, rel, q in hits[:limit]]


def cumulative_diff(repo: Path, files: dict[str, str]) -> str:
    out = []
    for rel, new in files.items():
        old = (Path(repo) / rel).read_text(encoding="utf-8", errors="replace")
        if old != new:
            out.append("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                    f"a/{rel}", f"b/{rel}")))
    return "\n".join(out)


# ------------------------------------------------------------------ Patch Applier (tool)
class PatchApplier:
    def __init__(self, localizer: FaultLocalizer):
        self.loc = localizer

    def _target(self, name: str, candidates: list[FaultCandidate], method_of: str | None = None):
        files = list(dict.fromkeys([c.file for c in candidates] + list(self.loc.mod2file.values())))
        files = [f for f in files if not _is_test_path(f)]
        wanted = [f"{method_of}.{name}"] if method_of else [name]
        if not method_of:   # a bare `def set_quantity` may be a method of a candidate class
            wanted += [c.name for c in candidates if c.name.endswith("." + name)]
        for q in wanted:
            for f in files:
                if q in self.loc.defs(f):
                    return f, q
        if not method_of:   # bare method name that is unique in the project
            hits = [(f, q) for f in files for q in self.loc.defs(f) if q.endswith("." + name)]
            if len(hits) == 1:
                return hits[0]
        return None

    def apply(self, patch: Patch, candidates: list[FaultCandidate]) -> str | None:
        """Fill patch.files and patch.diff. Returns an error message for the generator, or None."""
        code = _dedent_code(patch.code)
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            if re.search(r"^(---|\+\+\+|@@) ", code, re.M):
                return ("You answered with a diff. Answer with the COMPLETE new Python definition(s) instead "
                        "(whole functions/methods), not a diff.")
            return f"Your code block is not valid Python ({e.msg}, line {e.lineno})."
        lines = code.splitlines()
        edits: dict[str, list] = defaultdict(list)
        imports: list[str] = []
        missing: list[str] = []

        def seg(node):
            s, e, _ = _span(node)
            return textwrap.dedent("\n".join(lines[s - 1:e]))

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(seg(node))
            elif isinstance(node, ast.ClassDef):
                tgt_cls = self._target(node.name, candidates)
                if tgt_cls is None:
                    missing.append(node.name)
                    continue
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        t = self._target(sub.name, candidates, method_of=node.name)
                        if t:
                            edits[t[0]].append((t[1], seg(sub)))
                        else:
                            missing.append(f"{node.name}.{sub.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign)):
                name = node.name if hasattr(node, "name") else next(
                    (t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
                     if isinstance(t, ast.Name)), None)
                t = self._target(name, candidates) if name else None
                if t:
                    edits[t[0]].append((t[1], seg(node)))
                elif name:
                    missing.append(name)
        if not edits:
            what = f" ({', '.join(missing)} not found in the project)" if missing else ""
            return ("Your answer did not replace any existing definition" + what +
                    ". Return the complete new version of an existing function, method or constant.")
        diffs = []
        for rel, items in edits.items():
            old = self.loc.src(rel)
            new_lines = old.splitlines()
            defs = self.loc.defs(rel)
            for q, text in sorted(items, key=lambda it: -defs[it[0]][0]):   # bottom-up
                s, e, col = defs[q]
                new_lines[s - 1:e] = textwrap.indent(text, " " * col).splitlines()
            new_lines = self._add_imports(new_lines, imports)
            imports = []    # imports go to the first patched file only
            new = "\n".join(new_lines) + ("\n" if old.endswith("\n") else "")
            try:
                ast.parse(new)
            except SyntaxError as e:
                return f"After applying your code, {rel} is not valid Python ({e.msg}, line {e.lineno})."
            if new != old:
                patch.files[rel] = new
                diffs.append("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                          f"a/{rel}", f"b/{rel}")))
        if not patch.files:
            return self._identical_feedback([(rel, q) for rel, items in edits.items() for q, _ in items])
        patch.diff = "\n".join(diffs)
        return None

    def _identical_feedback(self, targets: list[tuple[str, str]]) -> str:
        """An unchanged rewrite means the defect is likely in something the definition depends on."""
        consts, callees = [], []
        for rel, q in targets:
            if self.loc._is_const(rel, q) or self.loc._is_class(rel, q):
                continue
            c = self.loc._candidate(rel, q, 0, "")
            for crel, cq in self.loc._constants_used(c):
                line = self.loc._candidate(crel, cq, 0, "").source.strip()
                if line not in consts:
                    consts.append(line)
            callees = list(dict.fromkeys(callees + [cq for _, cq in self.loc._callees(rel, q)]))
        msg = [f"Your code is identical to the current {', '.join(q for _, q in targets)}, so it changes nothing "
               "and the bug is still there."]
        if consts or callees:
            msg.append("Its body may be correct; the defect may be in what it uses:")
            if consts:
                msg.append("constants " + "; ".join(f"`{c}`" for c in consts[:5]) + ".")
            if callees:
                msg.append("Functions it calls: " + ", ".join(callees[:5]) + ".")
            msg.append("Compare these values and functions with the expected behaviour in the report, and return "
                       "the corrected definition (a constant can be returned as one assignment line).")
        return " ".join(msg)

    @staticmethod
    def _add_imports(lines: list[str], imports: list[str]) -> list[str]:
        todo = [i for i in imports if i.strip() and i.strip() not in (l.strip() for l in lines)]
        if not todo:
            return lines
        try:   # insert after the END of the last top-level import (it may span several lines)
            tree = ast.parse("\n".join(lines))
            last = max((n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))),
                       default=0) - 1
        except SyntaxError:
            last = -1
        return lines[:last + 1] + todo + lines[last + 1:]


def _render(c: FaultCandidate) -> str:
    """Candidate as shown to the SLM; methods appear inside their class header (as they must be returned)."""
    head = f"# {c.file} (module {c.module}) - {c.why}"
    if "." in c.name:
        cls = c.name.split(".")[0]
        return f"{head}\nclass {cls}:  # other members omitted\n{c.source}"
    return f"{head}\n{c.source}"


def _dedent_code(code: str) -> str:
    """Dedent by the smallest indentation of real code lines; drop leading comment-only lines
    (the SLM often copies our '# file ...' header at column 0 above an indented method)."""
    lines = code.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    ind = [len(l) - len(l.lstrip()) for l in lines if l.strip() and not l.lstrip().startswith("#")]
    cut = min(ind) if ind else 0
    out = [l[cut:] if l[:cut].strip() == "" else l.lstrip() for l in lines]
    return "\n".join(out) + "\n"


def _words(s: str) -> set[str]:
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+|_", s) if len(w) > 2}


def _tail(s: str, n: int) -> str:
    return "\n".join(s.strip().splitlines()[-n:])
