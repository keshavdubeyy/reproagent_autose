"""Code graph builder, ported from context-engineering-v2.

Source: FarahAlFahim/context-engineering-v2, scripts/build_code_and_call_graphs.py
(commit 1c65307), itself following LocAgent's dependency graph. The analysis logic
(entities, contains / imports / invokes / inherits edges, scoped + global name
matching) is kept as in the original. Our changes are marked "AUTOSE:":

  1. ids and paths use "/" on every OS (the original used os.sep: broken on Windows)
  2. class __init__ methods are kept as nodes (the original dropped them; many
     SWE-bench bugs live in __init__)
  3. skip-dirs match whole path parts (the original matched substrings)
  4. invokes / inherits edges record how they were found: "scoped" (via imports and
     containment) or "global" (same name anywhere in the repo, a weaker guess)
  5. duplicate edges and self-loops are dropped; files that fail to parse are reported,
     not hidden; same-named definitions in one file (e.g. property getter + setter)
     still share one id, as in the original, but are counted
  6. classes record their base-class names (needed to tag unittest test classes)
  7. git checkout / registry orchestration removed: we index exported snapshots
  8. imports also resolve from src/ and lib/ (flask, pytest and matplotlib keep their
     package there; the original only looked at the repo root, so their calls fell back
     to global name guesses)

Node code is used during analysis but not returned; the index stores locations and
readers take the code from the snapshot by line range.
"""
from __future__ import annotations

import ast
import os
import sys
import warnings
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

NODE_TYPE_DIRECTORY = "directory"
NODE_TYPE_FILE = "file"
NODE_TYPE_CLASS = "class"
NODE_TYPE_FUNCTION = "function"

EDGE_TYPE_CONTAINS = "contains"
EDGE_TYPE_IMPORTS = "imports"
EDGE_TYPE_INVOKES = "invokes"
EDGE_TYPE_INHERITS = "inherits"

# AUTOSE: whole path parts, plus common virtualenv / tool dirs
SKIP_DIRS = {".git", ".github", "__pycache__", ".tox", ".venv", "venv", ".eggs", "node_modules"}

sys.setrecursionlimit(30000)


def _posix(path: str) -> str:  # AUTOSE: one id format on every OS
    return path.replace(os.sep, "/")


def is_skip_dir(rel_dir: str) -> bool:
    return any(part in SKIP_DIRS for part in rel_dir.split("/"))


def _base_name(expr: ast.expr) -> Optional[str]:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


# ---------------- AST helpers ----------------
def find_imports(rel_file: str, tree: ast.AST, whole_file: bool) -> list:
    """Imports in a file (whole_file) or directly inside one class/function body."""
    candidates = ast.walk(tree) if whole_file else ast.iter_child_nodes(tree)
    imports = []
    for node in candidates:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({"type": "import", "module": alias.name, "alias": alias.asname})
        elif isinstance(node, ast.ImportFrom):
            entities = []
            for alias in node.names:
                if alias.name == "*":
                    entities = [{"name": "*", "alias": None}]
                    break
                entities.append({"name": alias.name, "alias": alias.asname})
            if node.level == 0:
                module_name = node.module
            else:
                package_parts = rel_file.split("/")
                package_parts = package_parts[:-node.level] if len(package_parts) >= node.level else []
                module_name = ".".join(package_parts + ([node.module] if node.module else []))
            imports.append({"type": "from", "module": module_name, "entities": entities})
    return imports


class CodeAnalyzer(ast.NodeVisitor):
    """Collects class / function entities with their source segment and lines."""

    def __init__(self, source_text: str):
        self._source_text = source_text
        self.nodes: list = []
        self.name_stack: list = []
        self.type_stack: list = []

    def _segment(self, node):
        try:
            return ast.get_source_segment(self._source_text, node)
        except Exception:
            return None

    def visit_ClassDef(self, node: ast.ClassDef):
        full = ".".join(self.name_stack + [node.name])
        self.nodes.append({
            "name": full, "type": NODE_TYPE_CLASS, "parent_type": self.type_stack[-1] if self.type_stack else None,
            "code": self._segment(node), "start_line": node.lineno, "end_line": node.end_lineno,
            "bases": [b for b in map(_base_name, node.bases) if b],  # AUTOSE: for test tagging
        })
        self.name_stack.append(node.name)
        self.type_stack.append(NODE_TYPE_CLASS)
        self.generic_visit(node)
        self.name_stack.pop()
        self.type_stack.pop()

    # AUTOSE: the original returned early here for a class's __init__, dropping it
    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_func(node)

    def _visit_func(self, node):
        full = ".".join(self.name_stack + [node.name])
        self.nodes.append({
            "name": full, "type": NODE_TYPE_FUNCTION, "parent_type": self.type_stack[-1] if self.type_stack else None,
            "code": self._segment(node), "start_line": node.lineno, "end_line": node.end_lineno,
        })
        self.name_stack.append(node.name)
        self.type_stack.append(NODE_TYPE_FUNCTION)
        self.generic_visit(node)
        self.name_stack.pop()
        self.type_stack.pop()


SOURCE_ROOTS = ("", "src/", "lib/")  # AUTOSE: src/ and lib/ layouts


def resolve_module(module_name: Optional[str], files: Dict[str, str]) -> Optional[str]:
    """'a.b' -> 'a/b.py' or 'a/b/__init__.py' (under the repo root, src/ or lib/)."""
    if not module_name:
        return None
    base = module_name.replace(".", "/")
    for root in SOURCE_ROOTS:
        for cand in (f"{root}{base}.py", f"{root}{base}/__init__.py"):
            if cand in files:
                return cand
    return None


def add_imports(src_id: str, imports: list, graph_nodes: Dict, add_edge, files: Dict[str, str]):
    for imp in imports:
        if imp["type"] == "import":
            rel = resolve_module(imp["module"], files)
            if rel and rel in graph_nodes:
                add_edge(src_id, rel, EDGE_TYPE_IMPORTS, alias=imp.get("alias"))
            continue
        module_name, entities = imp["module"], imp["entities"]
        if len(entities) == 1 and entities[0]["name"] == "*":
            rel = resolve_module(module_name, files)
            if rel and rel in graph_nodes:
                add_edge(src_id, rel, EDGE_TYPE_IMPORTS, alias=None)
            continue
        for ent in entities:
            name, alias = ent["name"], ent.get("alias")
            fullmod = f"{module_name}.{name}" if module_name else name
            rel = resolve_module(fullmod, files)
            if rel:
                if rel in graph_nodes:
                    add_edge(src_id, rel, EDGE_TYPE_IMPORTS, alias=alias)
                continue
            relm = resolve_module(module_name, files)
            if relm:
                node_entity = f"{relm}:{name}"
                if node_entity in graph_nodes:
                    add_edge(src_id, node_entity, EDGE_TYPE_IMPORTS, alias=alias)
                elif relm in graph_nodes:
                    add_edge(src_id, relm, EDGE_TYPE_IMPORTS, alias=alias)


# ---------------- graph builder ----------------
def build_code_graph(repo_dir: str, fuzzy_search: bool = True, global_import: bool = True):
    """Return (graph_nodes, graph_edges, problems).

    graph_nodes: id -> {id, type, start_line, end_line, parent_type, bases, code}
    graph_edges: [{source, target, type, [alias], [resolution]}]
    problems:    {"parse_errors": [{file, error}], "duplicate_ids": {id: extra_count}}
    """
    with warnings.catch_warnings():  # old repos are full of "invalid escape sequence" warnings
        warnings.simplefilter("ignore", SyntaxWarning)
        return _build_code_graph(repo_dir, fuzzy_search, global_import)


def _build_code_graph(repo_dir: str, fuzzy_search: bool, global_import: bool):
    repo_path = os.path.abspath(repo_dir)
    graph_nodes: Dict[str, Dict] = {"/": {"id": "/", "type": NODE_TYPE_DIRECTORY}}
    graph_edges: List[Dict] = []
    seen_edges = set()
    parse_errors: List[Dict] = []

    def add_edge(src, dst, etype, **extra):  # AUTOSE: dedupe, source/target keys
        key = (src, dst, etype)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edge = {"source": src, "target": dst, "type": etype}
        edge.update({k: v for k, v in extra.items() if v is not None})
        graph_edges.append(edge)

    files: Dict[str, str] = {}  # rel_file -> source text
    for root, dirs, filenames in os.walk(repo_path):
        rel_dir = _posix(os.path.relpath(root, repo_path))
        if rel_dir == ".":
            rel_dir = "/"
        elif is_skip_dir(rel_dir):
            dirs[:] = []  # AUTOSE: don't descend either
            continue
        else:
            graph_nodes.setdefault(rel_dir, {"id": rel_dir, "type": NODE_TYPE_DIRECTORY})
            parent = os.path.dirname(rel_dir) or "/"
            add_edge(parent, rel_dir, EDGE_TYPE_CONTAINS)
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            file_abs = os.path.join(root, fname)
            if os.path.islink(file_abs):
                continue
            rel_file = _posix(os.path.relpath(file_abs, repo_path))
            try:
                with open(file_abs, "r", encoding="utf-8") as f:
                    files[rel_file] = f.read()
            except Exception as exc:
                parse_errors.append({"file": rel_file, "error": f"read: {type(exc).__name__}"})
                continue
            graph_nodes[rel_file] = {"id": rel_file, "type": NODE_TYPE_FILE}

    trees: Dict[str, ast.AST] = {}
    duplicate_ids: Dict[str, int] = defaultdict(int)
    for rel_file, text in files.items():
        try:
            trees[rel_file] = ast.parse(text, filename=rel_file)
        except (SyntaxError, ValueError, RecursionError) as exc:
            parse_errors.append({"file": rel_file, "error": f"parse: {type(exc).__name__}: {exc}"[:200]})
            entities = []
        else:
            analyzer = CodeAnalyzer(text)
            try:
                analyzer.visit(trees[rel_file])
            except RecursionError:
                parse_errors.append({"file": rel_file, "error": "analyze: RecursionError"})
            entities = analyzer.nodes
        add_edge(os.path.dirname(rel_file) or "/", rel_file, EDGE_TYPE_CONTAINS)
        for ent in entities:
            node_id = f"{rel_file}:{ent['name']}"
            if node_id in graph_nodes:
                duplicate_ids[node_id] += 1  # the later definition wins, as in the original
            graph_nodes[node_id] = {"id": node_id, **{k: v for k, v in ent.items() if k != "name"}}
            parts = ent["name"].split(".")
            parent = rel_file if len(parts) == 1 else f"{rel_file}:{'.'.join(parts[:-1])}"
            add_edge(parent, node_id, EDGE_TYPE_CONTAINS)

    for rel_file, tree in trees.items():
        add_imports(rel_file, find_imports(rel_file, tree, whole_file=True), graph_nodes, add_edge, files)

    global_name_map = defaultdict(list)
    if global_import:
        for nid in graph_nodes:
            if nid.endswith(".py"):
                fname = os.path.basename(nid)
                global_name_map[fname].append(nid)
                global_name_map[os.path.splitext(fname)[0]].append(nid)
            elif ":" in nid:
                global_name_map[nid.split(":")[-1].split(".")[-1]].append(nid)

    outgoing = defaultdict(list)
    incoming = defaultdict(list)
    for e in graph_edges:
        outgoing[e["source"]].append(e)
        incoming[e["target"]].append(e)

    entity_nodes = [nid for nid, nd in graph_nodes.items() if nd["type"] in (NODE_TYPE_CLASS, NODE_TYPE_FUNCTION)]

    def get_inner_nodes(query_node, src_node):
        res = []
        for e in outgoing.get(src_node, []):
            if e["type"] == EDGE_TYPE_CONTAINS and e["target"] != query_node:
                res.append(e["target"])
                if graph_nodes.get(e["target"], {}).get("type") == NODE_TYPE_CLASS:
                    res.extend(get_inner_nodes(query_node, e["target"]))
        return res

    def find_parent(node_name):
        for e in incoming.get(node_name, []):
            if e["type"] == EDGE_TYPE_CONTAINS:
                return e["source"]
        return None

    file_scope_cache: Dict[str, Tuple[List[str], Dict[str, str]]] = {}

    def file_scope(cur) -> Tuple[List[str], Dict[str, str]]:
        """What a file's imports make callable. The same for every entity in the file, so
        computed once per file (AUTOSE: the original redid it per entity; same result)."""
        if cur not in file_scope_cache:
            callee_nodes, callee_alias = [], {}
            file_list, file_stack, seen = [], [cur], {cur}
            while file_stack:
                f0 = file_stack.pop()
                for e in outgoing.get(f0, []):
                    if e["type"] == EDGE_TYPE_IMPORTS and e["target"] not in seen:
                        seen.add(e["target"])
                        if (graph_nodes.get(e["target"], {}).get("type") == NODE_TYPE_FILE
                                and e["target"].endswith("__init__.py")):
                            file_list.append(e["target"])
                            file_stack.append(e["target"])
            for f in file_list + [cur]:  # imported package __init__ files, then cur itself
                if f != cur:
                    callee_nodes.extend(get_inner_nodes(cur, f))
                for e in outgoing.get(f, []):
                    if e["type"] == EDGE_TYPE_IMPORTS:
                        if e.get("alias"):
                            callee_alias[e["alias"]] = e["target"]
                        ttype = graph_nodes.get(e["target"], {}).get("type")
                        if ttype in (NODE_TYPE_FILE, NODE_TYPE_CLASS):
                            callee_nodes.extend(get_inner_nodes(f, e["target"]))
                        if ttype in (NODE_TYPE_FUNCTION, NODE_TYPE_CLASS):
                            callee_nodes.append(e["target"])
            file_scope_cache[cur] = (callee_nodes, callee_alias)
        return file_scope_cache[cur]

    def find_all_possible_callee(node_id) -> Tuple[List[str], Dict[str, str]]:
        callee_nodes, callee_alias = [], {}
        cur = prev = node_id
        while True:
            callee_nodes.extend(get_inner_nodes(prev, cur))
            if graph_nodes[cur]["type"] == NODE_TYPE_FILE:
                nodes, alias = file_scope(cur)
                callee_nodes.extend(nodes)
                callee_alias.update(alias)
                break
            prev, cur = cur, find_parent(cur)
            if cur is None:
                break
        return callee_nodes, callee_alias

    def link(node_id, names, etype, callee_name_dict):
        for name in set(names):
            cand = callee_name_dict.get(name)
            if cand:
                for c in (cand if isinstance(cand, list) else [cand]):
                    if c != node_id:
                        add_edge(node_id, c, etype, resolution="scoped")
            elif global_import:
                for c in global_name_map.get(name, []):
                    if c != node_id:
                        add_edge(node_id, c, etype, resolution="global")

    for node_id in entity_nodes:
        meta = graph_nodes[node_id]
        code = meta.get("code")
        if not code:
            continue
        try:
            caller_tree = ast.parse(code)
        except Exception:
            continue
        callee_nodes, callee_alias = find_all_possible_callee(node_id)
        if fuzzy_search:
            callee_name_dict = defaultdict(list)
            for c in set(callee_nodes):
                callee_name_dict[c.split(":")[-1].split(".")[-1]].append(c)
            for a, c in callee_alias.items():
                callee_name_dict[a].append(c)
        else:
            callee_name_dict = {c.split(":")[-1].split(".")[-1]: c for c in callee_nodes[::-1]}
            callee_name_dict.update(callee_alias)

        rel_file = node_id.split(":")[0]
        caller_name = node_id.split(":")[-1].split(".")[-1]
        invocations, inheritances = [], []
        if meta["type"] == NODE_TYPE_CLASS:
            for ast_node in ast.walk(caller_tree):
                if isinstance(ast_node, ast.ClassDef) and ast_node.name == caller_name:
                    add_imports(node_id, find_imports(rel_file, ast_node, whole_file=False), graph_nodes, add_edge, files)
                    inheritances += [b for b in map(_base_name, ast_node.bases) if b]
                    invocations += [d for d in map(_base_name, ast_node.decorator_list) if d]
                    for item in ast_node.body:  # calls in __init__ also count for the class (original)
                        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                            add_imports(node_id, find_imports(rel_file, item, whole_file=False), graph_nodes, add_edge, files)
                            for sub in ast.walk(item):
                                if isinstance(sub, ast.Call) and _base_name(sub.func):
                                    invocations.append(_base_name(sub.func))
                            break
                    break
        else:
            for ast_node in ast.walk(caller_tree):
                if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast_node.name == caller_name:
                    add_imports(node_id, find_imports(rel_file, ast_node, whole_file=False), graph_nodes, add_edge, files)
                    invocations += [d for d in map(_base_name, ast_node.decorator_list) if d]

                    def traverse_call(n):  # calls in this body, not in nested defs
                        for child in ast.iter_child_nodes(n):
                            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                continue
                            if isinstance(child, ast.Call) and _base_name(child.func):
                                invocations.append(_base_name(child.func))
                            traverse_call(child)
                    traverse_call(ast_node)
                    break

        link(node_id, invocations, EDGE_TYPE_INVOKES, callee_name_dict)
        link(node_id, inheritances, EDGE_TYPE_INHERITS, callee_name_dict)

    return graph_nodes, graph_edges, {"parse_errors": parse_errors, "duplicate_ids": dict(duplicate_ids)}
