"""Bounded context 2: Bug Resolution - phase 1, reproduction (phase 2, fixing, is in resolution.py).

* RepoExplorer   - Function/Tool : AST index of the project, ranks code relevant to the BugSpec,
                                   mines usage examples / imports from existing tests.
* TestGenerator  - AI capability : writes a pytest reproduction test, refined with feedback.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from .llm import LLM, extract_code, is_cut_off
from .models import AttemptRecord, BugReport, BugSpec, CodeContext, CodeSnippet, ReproTest

SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", "node_modules", ".tox",
             "docs", "doc", "examples"}
MAX_SNIPPET_LINES = 45


def _is_test_path(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return any(p in ("test", "tests", "testing") for p in parts[:-1]) or parts[-1].startswith("test")


def _module_name(rel: str) -> str:
    """Import path of a file. `src/` and `lib/` layouts are not part of it: src/flask/app.py -> flask.app."""
    rel = rel.replace("\\", "/")
    head, _, rest = rel.partition("/")
    if head in ("src", "lib") and "/" in rest:
        rel = rest
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _words(s: str) -> set[str]:
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+|_", s) if len(w) > 2}


class RepoExplorer:
    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self._defs: list[CodeSnippet] | None = None
        self._tests: list[tuple[str, str]] | None = None
        self._helpers: list[str] = []   # names defined in test helper modules
        self._globals: list[str] = []   # module-level assigned names
        self._modules: set[str] = set()

    # -------------------------------------------------- indexing (tool)
    def _index(self) -> None:
        defs, tests = [], []
        for dirpath, dirnames, filenames in os.walk(self.repo):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if not fn.endswith(".py") or fn.startswith("test_reproagent"):
                    continue
                path = Path(dirpath) / fn
                rel = str(path.relative_to(self.repo)).replace("\\", "/")
                try:
                    src = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                is_test = _is_test_path(rel)
                if is_test:
                    tests.append((rel, src))
                    if Path(rel).name.startswith("test"):
                        continue
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                if is_test:   # test helper module (tests/utils.py, conftest.py): only for symbol lookup
                    for node in tree.body:
                        nm = getattr(node, "name", None) or (
                            node.targets[0].id if isinstance(node, ast.Assign)
                            and isinstance(node.targets[0], ast.Name) else None)
                        if nm:
                            self._helpers.append(f"{_module_name(rel)}.{nm}")
                    continue
                self._modules.add(_module_name(rel))
                for node in tree.body:   # module-level names such as Command = namedtuple(...)
                    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                        self._globals.append(f"{_module_name(rel)}.{node.targets[0].id}")
                lines = src.splitlines()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        end = getattr(node, "end_lineno", node.lineno)
                        body = lines[node.lineno - 1:end]
                        if len(body) > MAX_SNIPPET_LINES:
                            body = body[:MAX_SNIPPET_LINES] + ["    # ... (truncated)"]
                        defs.append(CodeSnippet(file=rel, module=_module_name(rel), name=node.name,
                                                lineno=node.lineno, source="\n".join(body)))
        self._defs, self._tests = defs, tests

    # -------------------------------------------------- retrieval (tool)
    def search(self, spec: BugSpec, report: BugReport, k: int = 4) -> CodeContext:
        if self._defs is None:
            self._index()
        symbols = {s.lower() for s in spec.symbols}
        files = [f.replace("\\", "/") for f in spec.files]
        text_words = _words(f"{report.title} {report.body} {spec.summary}")
        scored = []
        for d in self._defs:
            score = 0.0
            if d.name.lower() in symbols:
                score += 10
            if any(d.file.endswith(f) or f.endswith(d.file) for f in files):
                score += 4
            if any(_module_name(f) in d.module for f in files if f.endswith(".py")):
                score += 1
            score += 1.5 * len(_words(d.name) & text_words)
            score += 0.5 * len(_words(d.file) & text_words)
            if d.name.startswith("_"):
                score -= 0.5
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda t: (-t[0], len(t[1].source)))
        top = []
        for s, d in scored:
            if len(top) >= k:
                break
            if any(t.name == d.name and t.file == d.file for t in top):
                continue
            d.score = s
            top.append(d)
        return CodeContext(snippets=top, usage_examples=self._usage(top[:2]))

    def _usage(self, snippets: list[CodeSnippet], max_lines: int = 14) -> list[str]:
        """Import lines + call sites of the top symbols in the project's existing tests."""
        out: list[str] = []
        for sn in snippets:
            pat = re.compile(rf"\b{re.escape(sn.name)}\b")
            for rel, src in self._tests or []:
                if not pat.search(src):
                    continue
                lines = src.splitlines()
                imports = [l for l in lines if l.startswith(("import ", "from ")) and
                           ("tests" in l or sn.name in l or "Command" in l or sn.module.split(".")[0] in l)]
                calls = [l.strip() for l in lines if pat.search(l) and not l.startswith(("from", "import"))]
                block = [f"# from {rel}"] + imports[:6] + calls[:6]
                out.extend(block)
                if len(out) >= max_lines:
                    return out[:max_lines]
                break
        return out

    # -------------------------------------------------- symbol lookup (tool, used for feedback)
    def locate(self, name: str) -> list[str]:
        """Dotted import paths where `name` is defined, e.g. ['shopcart.cart.Product']."""
        if self._defs is None:
            self._index()
        found = {f"{d.module}.{d.name}" for d in self._defs if d.name == name}
        found |= {g for g in self._globals + self._helpers if g.rsplit(".", 1)[-1] == name}
        return sorted(found, key=lambda s: (s.startswith("tests"), len(s)))[:3]

    def module_named(self, name: str) -> list[str]:
        return sorted(m for m in self._modules if m.rsplit(".", 1)[-1] == name)[:3]

    def class_attributes(self, cls: str) -> list[str]:
        """Methods and self.<attr> fields of a class (from its source)."""
        if self._defs is None:
            self._index()
        attrs: set[str] = set()
        for d in self._defs:
            if d.name == cls and d.source.lstrip().startswith("class"):
                attrs |= set(re.findall(r"^\s+def (\w+)", d.source, re.M))
                attrs |= set(re.findall(r"self\.(\w+)\s*=", d.source))
        return sorted(a for a in attrs if not a.startswith("__") or a == "__init__")

    def symbol_hint(self, error: str, test_code: str) -> str:
        """Turn a NameError / ImportError / AttributeError into a concrete correction."""
        names: list[str] = []
        m = re.search(r"name '(\w+)' is not defined|cannot import name '(\w+)'", error)
        if m:
            names.append(m.group(1) or m.group(2))
        m = re.search(r"No module named '([\w.]+)'", error)
        if m:
            for imp in re.findall(rf"from {re.escape(m.group(1))} import ([\w, ]+)", test_code):
                names += [n.strip() for n in imp.split(",")]
        hints = []
        for n in names:
            where = self.locate(n)
            mods = self.module_named(n)
            if where:
                hints.append(f"`{n}` is defined in: {', '.join(where)} -> import it from there "
                             f"(e.g. `from {where[0].rsplit('.', 1)[0]} import {n}`).")
            elif mods:
                hints.append(f"`{n}` is a module: use `from {mods[0].rsplit('.', 1)[0]} import {n}` "
                             f"or `import {mods[0]}`.")
            else:
                hints.append(f"`{n}` does not exist in this project; do not use it.")
        m = re.search(r"'(\w+)' object has no attribute '(\w+)'", error)
        if m:
            attrs = self.class_attributes(m.group(1))
            if attrs:
                hints.append(f"`{m.group(1)}` has no `{m.group(2)}`. Its real attributes/methods are: "
                             f"{', '.join(attrs)}.")
        return " ".join(hints)

    def repo_layout(self, max_items: int = 25) -> str:
        items = []
        for p in sorted(self.repo.iterdir()):
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            items.append(p.name + ("/" if p.is_dir() else ""))
        return " ".join(items[:max_items])


# ------------------------------------------------------------------ AI capability
GEN_SYSTEM = """You are a test engineer. Write ONE pytest test file that REPRODUCES a reported bug in a Python project.
Rules:
- The test must encode the EXPECTED (correct) behaviour from the bug report, so it FAILS on the current buggy code and would PASS after a fix.
- Recreate the report's scenario EXACTLY: same inputs, amounts and steps. Do not add extra items or setup
  that changes the inputs. If the report gives an amount (e.g. Rs 800), build data that totals exactly that.
- Assert ONLY the property the report complains about (ideally a single assert). Do not assert other
  fields (totals, taxes, counts...) that the report does not give exact values for.
- Use the exact expected values from the report when they are given.
- Import the code under test using the module paths shown (the project root is on sys.path).
- Do NOT mock or re-implement the function under test. Do NOT wrap the call in try/except.
- For a crash bug, call the code normally and assert the expected result; the crash itself makes the test fail.
- If the report describes SEVERAL separate wrong behaviours, write one test function for EACH of them
  (each with its own scenario), so that every symptom is checked.
- Keep it short: one test function per symptom, plain asserts.
- Do not reason or explain in comments: at most one short comment line per test function.
Answer with a single ```python code block and nothing else."""


class TestGenerator:
    __test__ = False  # not a pytest test class
    def __init__(self, llm: LLM):
        self.llm = llm

    def generate(self, report: BugReport, spec: BugSpec, ctx: CodeContext | None, layout: str,
                 history: list[AttemptRecord], attempt: int) -> ReproTest:
        parts = [f"## Bug report\nTitle: {report.title}\n{report.body}",
                 f"## Interpretation\nExpected: {spec.expected}\nObserved: {spec.observed}\n"
                 f"Failure kind: {spec.failure_kind}"]
        if ctx is not None:
            parts.append(f"## Project top-level layout\n{layout}")
            if ctx.snippets:
                code = "\n\n".join(f"# module: {s.module}  (file {s.file}, line {s.lineno})\n{s.source}"
                                   for s in ctx.snippets)
                parts.append(f"## Relevant project code\n```python\n{code}\n```")
            if ctx.usage_examples:
                parts.append("## How existing tests import/call this code (API usage ONLY - do NOT copy "
                             "their input values; use the values from the bug report)\n```python\n"
                             + "\n".join(ctx.usage_examples) + "\n```")
        if ctx is not None and re.search(r"(^|\s)django/", layout or ""):
            parts.append("## Test environment\nDjango settings are already configured when the test runs "
                         "(in-memory SQLite with tables created; INSTALLED_APPS: contenttypes, auth, sessions, "
                         "messages, admin, sites, staticfiles). Do NOT call settings.configure() or "
                         "django.setup(). If you need a model, use an existing one such as "
                         "django.contrib.auth.models.User, Group or Permission instead of defining a new one.")
        if spec.example_inputs:
            parts.append("## Scenario values from the report (use these)\n- " + "\n- ".join(spec.example_inputs))
        if history:
            last = history[-1]
            parts.append(f"## Your previous attempt (#{last.test.attempt}) did NOT work\n```python\n"
                         f"{last.test.code}```\nExecution result: {last.execution.status}\n"
                         f"```\n{_tail(last.execution.output, 25)}\n```\nProblem: {last.analysis.feedback}\n"
                         "Write a DIFFERENT, improved test that fixes this problem.")
        parts.append("Write the reproduction test now.")
        prompt = "\n\n".join(parts)
        seen = {_norm(h.test.code) for h in history}
        temp = 0.2 if attempt == 1 else 0.6
        for _ in range(2):   # regenerate once if the SLM repeats an earlier test verbatim
            raw = self.llm.chat(GEN_SYSTEM, prompt, temperature=temp)
            code = extract_code(raw)
            if is_cut_off(raw):     # rambled in comments until the length limit: ask once for code only
                prompt += ("\n\nYour answer was cut off because it was too long. Write the test code "
                           "directly, with no explanatory comments.")
                temp = 0.6
                continue
            if _norm(code) not in seen:
                break
            prompt += ("\n\nYou produced exactly the same test as before. It is wrong. Change the inputs "
                       "and assertions so they follow the bug report's scenario.")
            temp = 0.9
        return ReproTest(attempt=attempt, code=code, raw_llm_output=raw)


def _norm(code: str) -> str:
    return re.sub(r"\s+", "", code)


def _tail(s: str, n: int) -> str:
    return "\n".join(s.strip().splitlines()[-n:])


# ------------------------------------------------------------------ explorer backed by the knowledge package
RANKED_K = 10


class IndexedExplorer(RepoExplorer):
    """Same tool interface, but `search` ranks code with the knowledge package (autose/intel) instead of
    keyword overlap: `bm25`, `hybrid` (configuration C) or `graph` (configuration D)."""

    def __init__(self, repo: Path, mode: str = "hybrid", repo_id: str | None = None, commit: str | None = None):
        super().__init__(repo)
        from autose.intel.index import KnowledgeIndex, build
        from autose.intel.retrieve import HybridRetriever
        rebuild = commit is None            # a local project without a known commit: always re-index
        self.index = KnowledgeIndex(build(self.repo, repo_id or f"local/{self.repo.name}",
                                          commit or "worktree", force=rebuild))
        self.retriever = HybridRetriever(self.index, mode)
        self.mode = mode
        self.last = None

    def search(self, spec: BugSpec, report: BugReport, k: int = 4) -> CodeContext:
        if self._defs is None:
            self._index()                   # still needed for symbol hints and usage examples
        r = self.retriever.search(report, k_symbols=40)
        self.last = r
        top: list[CodeSnippet] = []
        ranked = []                         # the longer ranked list, handed to the Fault Localiser
        for c in r.symbols:
            row = self.index.symbol(c.sid)
            if row["kind"] == "module":
                continue
            if len(ranked) < RANKED_K:
                ranked.append((row["path"], row["lineno"], row["name"]))
            if len(top) >= k:
                if len(ranked) >= RANKED_K:
                    break
                continue
            try:
                lines = (self.repo / row["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            body = lines[row["lineno"] - 1:row["end_lineno"]]
            if len(body) > MAX_SNIPPET_LINES:
                body = body[:MAX_SNIPPET_LINES] + ["    # ... (truncated)"]
            top.append(CodeSnippet(file=row["path"], module=row["module"], name=row["name"], lineno=row["lineno"],
                                   source="\n".join(body), score=round(c.score, 2)))
        return CodeContext(snippets=top, usage_examples=self._usage(top[:2]), ranked=ranked)
