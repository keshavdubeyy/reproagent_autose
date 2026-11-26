"""Code navigation tools for the agents, over one public snapshot and its repository_index.

Port of context-engineering-v2@1c65307 src/tools/code_navigation.py and src/tools/registry.py:
get_code, get_file_context, search_codebase, get_subgraph.

AUTOSE changes:
  1. get_code reads the entity from the snapshot by its index line range (the original read a
     `code` field the graph builder never writes, so it always returned empty code).
  2. Every output is capped (MAX_LINES / MAX_CHARS) so a call fits an 8K-token context.
  3. search_codebase scans the snapshot in Python (no git, no .git in our snapshots), max 30 hits.
  4. get_subgraph lists one hop with direction (calls / called by / contains / inherits) and
     marks name-only ("global") calls; the original printed an undirected 2-hop BFS.
  5. Paths are confined to the snapshot directory.
  6. Tool schemas are Ollama/OpenAI function specs instead of LangChain StructuredTools.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from pathlib import Path

from autose.retrieval.mentions import Resolver

MAX_LINES = 120
MAX_CHARS = 5000
MAX_HITS = 30
MAX_NEIGHBOURS = 12

SPECS = [
    {"type": "function", "function": {
        "name": "get_code", "description": "Source of a class, method or function. Give its id "
        "(path/file.py:Class.method) or its name (Class.method).",
        "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}}},
    {"type": "function", "function": {
        "name": "get_file_context", "description": f"Read lines of a file (at most {MAX_LINES} per call).",
        "parameters": {"type": "object", "properties": {
            "file": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            "required": ["file"]}}},
    {"type": "function", "function": {
        "name": "search_codebase", "description": f"Regex search over the repository's .py files (first {MAX_HITS} hits).",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "get_subgraph", "description": "What a class/method calls, what calls it, what it contains and inherits.",
        "parameters": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}}},
]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n[... truncated]"


class Tools:
    def __init__(self, index: dict, code_dir: str | Path):
        self.root = Path(code_dir).resolve()
        self.entities = {e["id"]: e for e in index["entities"]}
        self.resolver = Resolver(index)
        self.files = [f["id"] for f in index["files"]]
        self._text: dict[str, list[str]] = {}
        self.out = defaultdict(list)
        self.inn = defaultdict(list)
        for r in index["relationships"]:
            if r["type"] in ("invokes", "inherits", "contains"):
                self.out[r["source"]].append(r)
                self.inn[r["target"]].append(r)
        self.calls: list[dict] = []
        self.read: set[str] = set()  # entity ids / file windows whose code the agent has seen
        self.hit_entities: list[str] = []  # entities enclosing search hits, in order found
        self.by_file = defaultdict(list)
        self.by_children = defaultdict(list)  # class / function id -> ids defined directly inside it
        for e in index["entities"]:
            self.by_file[e["file"]].append(e)
            self.by_children[e["parent"]].append(e["id"])
        self.seen_calls: dict[str, str] = {}
        self.is_test_file = {f["id"]: f["is_test"] for f in index["files"]}

    # ---------- helpers ----------
    def lines(self, file: str) -> list[str]:
        if file not in self._text:
            p = (self.root / file).resolve()
            if self.root not in p.parents or not p.is_file():
                raise FileNotFoundError(file)
            self._text[file] = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return self._text[file]

    def find(self, node: str) -> tuple[str | None, list[str]]:
        """Port of _fuzzy_find_node / _find_ambiguous_matches: (id, candidates if ambiguous)."""
        q = node.strip().strip("'\"`").replace("::", ":")
        if q in self.entities:
            return q, []
        if ":" in q:
            f, qual = q.split(":", 1)
            hits = [i for i in self.entities if i.endswith(f"{f}:{qual}") or i == f"{f}:{qual}"]
            if len(hits) == 1:
                return hits[0], []
            q = qual if not hits else q
        ids = self.resolver.name(q.replace("/", ".").removesuffix(".py"))
        if len(ids) == 1:
            return ids[0], []
        return None, ids[:15]

    def enclosing(self, file: str, line: int) -> str | None:
        inside = [e for e in self.by_file.get(file, []) if e["start_line"] <= line <= e["end_line"]]
        return min(inside, key=lambda e: e["end_line"] - e["start_line"])["id"] if inside else None

    def suggest(self, name: str, n: int = 5) -> list[str]:
        """Closest real files / entity ids: bug reports often quote renamed or misspelt names."""
        name = name.strip().strip("'\"`").replace("\\", "/")
        if name.endswith(".py") or "/" in name and ":" not in name:
            def score(f):  # whole path and file name both count; source files before tests
                ratio = difflib.SequenceMatcher(None, name, f).ratio()
                base = difflib.SequenceMatcher(None, name.rsplit("/", 1)[-1], f.rsplit("/", 1)[-1]).ratio()
                return ratio + base - 0.5 * self.is_test_file.get(f, False)
            ranked = sorted(self.files, key=score, reverse=True)
            out = [f for f in ranked[:n] if score(f) > 1.0]
        else:
            tail = name.split(":")[-1]
            quals = {i.split(":", 1)[1]: i for i in self.entities if not self.entities[i]["is_test"]}
            out = [quals[q] for q in difflib.get_close_matches(tail, list(quals), n, 0.7)]
        return list(dict.fromkeys(out))[:n]

    def file_outline(self, file: str, limit: int = 40) -> str:
        ents = sorted(self.by_file.get(file, []), key=lambda e: e["start_line"])
        rows = [f"  {e['id']}  [{e['kind']}, lines {e['start_line']}-{e['end_line']}]" for e in ents[:limit]]
        more = f"\n  ... {len(ents) - limit} more" if len(ents) > limit else ""
        return f"{file} is a file. Its classes and functions:\n" + "\n".join(rows) + more

    def signature(self, eid: str) -> str:
        e = self.entities[eid]
        try:
            return self.lines(e["file"])[e["start_line"] - 1].strip()
        except (FileNotFoundError, IndexError):
            return ""

    # ---------- tools ----------
    def get_code(self, node: str) -> str:
        eid, cands = self.find(node)
        if not eid:
            if cands:
                return f"ambiguous, use one of: {cands}"
            f = self.resolver.file(node.strip()) if node.strip().endswith(".py") else None
            if f:
                return self.file_outline(f)
            near = self.suggest(node)
            return f"not found: {node}." + (f" Closest matches: {near}" if near else " Try search_codebase.")
        e = self.entities[eid]
        start, end = e["start_line"], e["end_line"]
        body = self.lines(e["file"])[start - 1:min(end, start - 1 + MAX_LINES)]
        more = f"\n[... {end - start + 1 - MAX_LINES} more lines: use get_file_context]" if end - start + 1 > MAX_LINES else ""
        self.read.add(eid)
        return _cap(f"{eid} ({e['kind']}, lines {start}-{end})\n" +
                    "\n".join(f"{start + i}: {l}" for i, l in enumerate(body)) + more)

    def get_file_context(self, file: str, start_line: int = 1, end_line: int | None = None) -> str:
        file = file.strip().lstrip("./").replace("\\", "/")
        if file not in self.files:
            match = self.resolver.file(file)
            if not match:
                near = self.suggest(file if file.endswith(".py") else file + ".py")
                return f"not found: {file}." + (f" Closest files: {near}" if near else "")
            file = match
        lines = self.lines(file)
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or start + MAX_LINES - 1), start + MAX_LINES - 1)
        self.read.add(f"{file}#{start}-{end}")
        return _cap(f"{file} lines {start}-{end} of {len(lines)}\n" +
                    "\n".join(f"{start + i}: {l}" for i, l in enumerate(lines[start - 1:end])))

    def search_codebase(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for f in sorted(self.files, key=lambda f: self.is_test_file.get(f, False)):  # source before tests
            for n, line in enumerate(self.lines(f), 1):
                if rx.search(line):
                    eid = self.enclosing(f, n)
                    if eid and not self.entities[eid]["is_test"] and eid not in self.hit_entities:
                        self.hit_entities.append(eid)
                    where = f" [in {eid}]" if eid else ""
                    hits.append(f"{f}:{n}{where}: {line.strip()[:140]}")
                    if len(hits) >= MAX_HITS:
                        return _cap("\n".join(hits) + f"\n[first {MAX_HITS} hits]")
        return _cap("\n".join(hits)) if hits else f"no matches for {pattern!r}"

    def get_subgraph(self, node: str) -> str:
        eid, cands = self.find(node)
        if not eid:
            return f"ambiguous: {cands}" if cands else f"not found: {node}"
        rows = []

        def add(label, rels, end):
            ordered = sorted(rels, key=lambda r: r.get("resolution") == "global")
            for r in ordered[:MAX_NEIGHBOURS]:
                tag = " (name-only guess)" if r.get("resolution") == "global" else ""
                rows.append(f"  {label} {r[end]}{tag}")
            if len(rels) > MAX_NEIGHBOURS:
                rows.append(f"  {label} ... {len(rels) - MAX_NEIGHBOURS} more")

        out, inn = self.out.get(eid, []), self.inn.get(eid, [])
        add("contains", [r for r in out if r["type"] == "contains"], "target")
        add("inherits from", [r for r in out if r["type"] == "inherits"], "target")
        add("subclassed by", [r for r in inn if r["type"] == "inherits"], "source")
        add("calls", [r for r in out if r["type"] == "invokes"], "target")
        add("called by", [r for r in inn if r["type"] == "invokes"], "source")
        return _cap(f"{eid} ({self.entities[eid]['kind']})\n" + ("\n".join(rows) or "  (no links)"))

    def call(self, name: str, args: dict) -> str:
        fn = {"get_code": self.get_code, "get_file_context": self.get_file_context,
              "search_codebase": self.search_codebase, "get_subgraph": self.get_subgraph}.get(name)
        if not fn:
            result = f"unknown tool {name}"
        else:
            norm = self._normalise(fn, args or {})
            key = name + repr(sorted((k, str(v).strip().strip("'\"`")) for k, v in norm.items()))
            if key in self.seen_calls:  # repeated call: remind instead of re-sending the same text
                first = self.seen_calls[key].splitlines()[0][:200] if self.seen_calls[key] else ""
                result = (f"You already called {name} with these arguments (it returned: {first} ...). "
                          "Use that result, or look at something else.")
                self.calls.append({"tool": name, "args": args, "chars": len(result), "repeat": True})
                return result
            try:
                result = fn(**norm)
            except Exception as e:  # a bad argument must not end the run
                result = f"error: {type(e).__name__}: {e}"
            self.seen_calls[key] = result
        self.calls.append({"tool": name, "args": args, "chars": len(result)})
        return result

    # names small models use instead of the declared ones (seen: get_code(id=...))
    ALIASES = {"node": ("id", "node_id", "name", "entity", "entity_id", "target", "qualified_name", "symbol"),
               "file": ("path", "file_path", "filename", "filepath"),
               "start_line": ("start", "line_start", "from_line"), "end_line": ("end", "line_end", "to_line"),
               "pattern": ("query", "regex", "text", "search", "term")}

    def _normalise(self, fn, args: dict) -> dict:
        params = fn.__code__.co_varnames[1:fn.__code__.co_argcount]
        out = {k: v for k, v in args.items() if k in params}
        for p in params:
            if p not in out:
                alias = next((a for a in self.ALIASES.get(p, ()) if a in args), None)
                if alias:
                    out[p] = args[alias]
        if params and params[0] not in out and len(args) == 1:  # a single unnamed-looking argument
            out[params[0]] = next(iter(args.values()))
        return out
