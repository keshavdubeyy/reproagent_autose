"""Repository intelligence, lite: one knowledge package per (repo_id, commit), stored in SQLite.

What is indexed (static analysis only, Python `ast`):
* symbols  - modules, classes, functions and methods with line ranges, signature and docstring
* edges    - CONTAINS (file->symbol, class->method), CALLS, INHERITS, IMPORTS. A call is resolved to a
             symbol only when its name has few definitions; otherwise it is kept as `resolved = 0`
             (the design's rule: uncertain links are marked, not guessed)
* sym_fts  - SQLite FTS5 table for BM25 over name / path / docstring+strings / body identifiers

Tests are not candidates (agents must fix source code), so test files are skipped.
Built once and reused by every bug on the same commit: data/index/<repo_id>@<commit>.db
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import time
import warnings
from collections import defaultdict
from pathlib import Path

INDEX_DIR = Path(os.environ.get("AUTOSE_INDEX_DIR", "data/index"))
SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", "node_modules", ".tox", "docs", "doc",
             "examples", "benchmarks", "asv_bench"}
MAX_BODY_TOKENS = 300
MAX_STRINGS = 40
AMBIGUOUS = 3          # a call name with more definitions than this stays unresolved
SCHEMA_VERSION = 2   # 2: src/ and lib/ layouts dropped from module names

# Old projects have invalid escape sequences; parsing them is fine, the warnings are noise.
warnings.filterwarnings("ignore", category=SyntaxWarning)

_SPLIT = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in ("test", "tests", "testing") for p in parts[:-1]) or parts[-1].startswith(("test_", "conftest"))


def module_name(rel: str) -> str:
    """Import path of a file; `src/` and `lib/` layouts are dropped (src/flask/app.py -> flask.app)."""
    head, _, rest = rel.partition("/")
    if head in ("src", "lib") and "/" in rest:
        rel = rest
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def subtokens(ident: str) -> list[str]:
    """`get_FIELD_display` -> ['get_field_display', 'get', 'field', 'display']."""
    low = ident.lower()
    parts = [p.lower() for chunk in re.split(r"[_.\W]+", ident) for p in _SPLIT.findall(chunk)]
    return [low] + [p for p in parts if len(p) > 1 and p != low]


def tokens_text(idents) -> str:
    return " ".join(t for i in idents for t in subtokens(i))


class _Visitor(ast.NodeVisitor):
    """Collects symbols and raw edges of one file."""

    def __init__(self, rel: str, lines: list[str]):
        self.rel, self.mod, self.lines = rel, module_name(rel), lines
        self.symbols: list[dict] = []
        self.calls: list[tuple[int, str]] = []      # (symbol index, called name)
        self.bases: list[tuple[int, str]] = []
        self.stack: list[int] = []                   # indexes into self.symbols

    def _add(self, node, kind: str) -> int:
        parent = self.symbols[self.stack[-1]] if self.stack else None
        qual = f"{parent['qualname']}.{node.name}" if parent and parent["kind"] == "class" else node.name
        if parent and parent["kind"] in ("function", "method"):
            qual = f"{parent['qualname']}.<locals>.{node.name}"
        if kind == "function" and parent and parent["kind"] == "class":
            kind = "method"
        doc = ast.get_docstring(node) or ""
        idents, strings = set(), []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                idents.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                idents.add(sub.attr)
            elif isinstance(sub, ast.arg):
                idents.add(sub.arg)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and 3 < len(sub.value) < 200 \
                    and len(strings) < MAX_STRINGS and sub.value is not doc:
                strings.append(sub.value)
        sig = self.lines[node.lineno - 1].strip() if node.lineno <= len(self.lines) else node.name
        self.symbols.append(dict(
            path=self.rel, module=self.mod, qualname=qual, name=node.name, kind=kind,
            lineno=node.lineno, end_lineno=getattr(node, "end_lineno", node.lineno), signature=sig[:200],
            doc=doc.split("\n\n")[0][:500], parent=self.stack[-1] if self.stack else None,
            idents=sorted(idents)[:MAX_BODY_TOKENS], strings=strings))
        return len(self.symbols) - 1

    def visit_ClassDef(self, node):
        i = self._add(node, "class")
        for b in node.bases:
            name = b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", None)
            if name:
                self.bases.append((i, name))
        self.stack.append(i)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        i = self._add(node, "function")
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name:
                    self.calls.append((i, name))
        self.stack.append(i)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def db_path(repo_id: str, commit: str) -> Path:
    return INDEX_DIR / f"{repo_id.replace('/', '__')}@{commit[:12]}.db"


def build(repo: Path, repo_id: str, commit: str, force: bool = False) -> Path:
    """Build (or reuse) the knowledge package for this repository at this commit."""
    path = db_path(repo_id, commit)
    if path.exists() and not force:
        with sqlite3.connect(path) as c:
            row = c.execute("select value from meta where key='status'").fetchone()
            man = c.execute("select value from meta where key='manifest'").fetchone()
            if row and row[0] == "READY" and man and json.loads(man[0]).get("schema") == SCHEMA_VERSION:
                return path   # same schema; an older schema is rebuilt
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    t0 = time.time()
    repo = Path(repo)
    c = sqlite3.connect(path)
    c.executescript("""
      create table meta(key text primary key, value text);
      create table files(id integer primary key, path text unique, module text, sha1 text, parsed int);
      create table symbols(id integer primary key, file_id int, path text, module text, qualname text, name text,
                           kind text, lineno int, end_lineno int, signature text, doc text, parent_id int);
      create table edges(src int, dst int, dst_name text, kind text, resolved int, evidence text);
      create virtual table sym_fts using fts5(name, path, doc, body, tokenize='unicode61');
    """)
    n_files = n_fail = 0
    all_calls, all_bases, imports = [], [], []
    by_name: dict[str, list[int]] = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            rel = (Path(dirpath) / fn).relative_to(repo).as_posix()
            if is_test_path(rel):
                continue
            src = (repo / rel).read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
                parsed = 1
            except SyntaxError:
                tree, parsed = None, 0
                n_fail += 1
            n_files += 1
            fid = c.execute("insert into files(path, module, sha1, parsed) values (?,?,?,?)",
                            (rel, module_name(rel), hashlib.sha1(src.encode()).hexdigest(), parsed)).lastrowid
            # the module itself is a symbol: module-level constants and imports live here
            mod_doc = (ast.get_docstring(tree) or "").split("\n\n")[0][:500] if tree else ""
            mod_id = c.execute(
                "insert into symbols(file_id,path,module,qualname,name,kind,lineno,end_lineno,signature,doc,parent_id)"
                " values (?,?,?,?,?,?,?,?,?,?,?)",
                (fid, rel, module_name(rel), "<module>", module_name(rel).rsplit(".", 1)[-1], "module", 1,
                 len(src.splitlines()), "", mod_doc, None)).lastrowid
            top_names = []
            if tree:
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        top_names += [t.id for t in node.targets if isinstance(t, ast.Name)]
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        mod = getattr(node, "module", None)
                        for a in node.names:
                            imports.append((mod_id, (f"{mod}.{a.name}" if mod else a.name)))
            c.execute("insert into sym_fts(rowid,name,path,doc,body) values (?,?,?,?,?)",
                      (mod_id, tokens_text([module_name(rel)]), tokens_text([rel]), mod_doc, tokens_text(top_names)))
            if not tree:
                continue
            v = _Visitor(rel, src.splitlines())
            v.visit(tree)
            ids = []
            for s in v.symbols:
                parent = ids[s["parent"]] if s["parent"] is not None else mod_id
                sid = c.execute(
                    "insert into symbols(file_id,path,module,qualname,name,kind,lineno,end_lineno,signature,doc,"
                    "parent_id) values (?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, rel, s["module"], s["qualname"], s["name"], s["kind"], s["lineno"], s["end_lineno"],
                     s["signature"], s["doc"], parent)).lastrowid
                ids.append(sid)
                by_name[s["name"]].append(sid)
                c.execute("insert into edges values (?,?,?,?,?,?)", (parent, sid, s["name"], "CONTAINS", 1, "static"))
                c.execute("insert into sym_fts(rowid,name,path,doc,body) values (?,?,?,?,?)",
                          (sid, tokens_text([s["qualname"]]), tokens_text([rel]),
                           s["doc"] + " " + " ".join(s["strings"]), tokens_text(s["idents"])))
            all_calls += [(ids[i], name) for i, name in v.calls]
            all_bases += [(ids[i], name) for i, name in v.bases]
    n_res = n_unres = 0
    for kind, pairs in (("CALLS", set(all_calls)), ("INHERITS", set(all_bases))):
        for src_id, name in pairs:
            targets = by_name.get(name, [])
            if 0 < len(targets) <= AMBIGUOUS:
                c.executemany("insert into edges values (?,?,?,?,?,?)",
                              [(src_id, t, name, kind, 1, "static") for t in targets if t != src_id])
                n_res += 1
            elif targets:
                c.execute("insert into edges values (?,?,?,?,?,?)", (src_id, None, name, kind, 0, "static:ambiguous"))
                n_unres += 1
    c.executemany("insert into edges values (?,?,?,?,?,?)",
                  [(s, None, m, "IMPORTS", 0, "static") for s, m in imports])
    c.executescript("create index e_src on edges(src); create index e_dst on edges(dst);"
                    "create index s_name on symbols(name); create index s_path on symbols(path);")
    n_sym = c.execute("select count(*) from symbols").fetchone()[0]
    manifest = dict(repo_id=repo_id, commit=commit, schema=SCHEMA_VERSION, files=n_files, parse_failures=n_fail,
                    symbols=n_sym, calls_resolved=n_res, calls_unresolved=n_unres,
                    build_s=round(time.time() - t0, 1))
    status = "READY" if n_files and n_fail / n_files < 0.05 else "NEEDS_ATTENTION"
    c.executemany("insert into meta values (?,?)",
                  [("manifest", json.dumps(manifest)), ("status", status)])
    c.commit()
    c.close()
    return path


class KnowledgeIndex:
    """Read-only access to a knowledge package (the tools agents call)."""

    def __init__(self, path: Path):
        self.c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.c.row_factory = sqlite3.Row

    @classmethod
    def for_repo(cls, repo: Path, repo_id: str, commit: str) -> "KnowledgeIndex":
        return cls(build(repo, repo_id, commit))

    def manifest(self) -> dict:
        return json.loads(self.c.execute("select value from meta where key='manifest'").fetchone()[0])

    def bm25(self, terms: list[str], k: int = 200) -> list[tuple[int, float]]:
        """(symbol id, score >= 0) for an OR query over the terms; higher is better."""
        terms = sorted({t for t in terms if re.fullmatch(r"\w+", t)})
        if not terms:
            return []
        q = " OR ".join(f'"{t}"' for t in terms)
        rows = self.c.execute("select rowid, bm25(sym_fts, 6.0, 2.0, 2.0, 1.0) from sym_fts where sym_fts match ? "
                              "order by bm25(sym_fts, 6.0, 2.0, 2.0, 1.0) limit ?", (q, k)).fetchall()
        return [(r[0], -r[1]) for r in rows]

    def symbol(self, sid: int) -> sqlite3.Row:
        return self.c.execute("select * from symbols where id=?", (sid,)).fetchone()

    def by_name(self, name: str) -> list[int]:
        return [r[0] for r in self.c.execute("select id from symbols where name=?", (name,))]

    def by_path_suffix(self, suffix: str) -> list[int]:
        return [r[0] for r in self.c.execute("select id from symbols where path like ?", (f"%{suffix}",))]

    def neighbours(self, sid: int) -> list[tuple[int, str]]:
        """Resolved callees, callers, parent and children: (symbol id, relation)."""
        out = [(r[0], f"{r[1]}>") for r in self.c.execute(
            "select dst, kind from edges where src=? and dst is not null and kind != 'CONTAINS'", (sid,))]
        out += [(r[0], f"<{r[1]}") for r in self.c.execute(
            "select src, kind from edges where dst=? and kind != 'CONTAINS'", (sid,))]
        row = self.symbol(sid)
        if row and row["parent_id"]:
            out.append((row["parent_id"], "parent"))
        return out
