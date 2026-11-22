"""Function-level program analysis: control flow, data flow, control dependence and backward slicing.

Together with the knowledge package (AST symbols + CALLS / INHERITS / IMPORTS edges) this forms a
lightweight code property graph (CPG):

  AST   statements of one function, with the names each one defines and uses
  CFG   statement-level control flow graph (ENTRY, EXIT, branches, loops, try/except, return/raise)
  DDG   data dependence: reaching definitions, def -> use for every variable / attribute path
  CDG   control dependence: a statement depends on the if / loop / except header it is nested in
  slice backward program slice: every statement that can affect a chosen line or the return value

Graphs are computed on demand for one function (milliseconds) instead of for the whole repository.

Python is dynamic, so this is an approximation: no aliasing, `self.x` is treated as one variable,
and calls are not followed here (the caller does that with the call graph, see `calls_in`).
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field

ENTRY, EXIT = 0, -1          # EXIT is renumbered to the last index after building


def dotted(node: ast.AST) -> str | None:
    """`a.b.c` for Name / Attribute chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _prefixes(path: str) -> list[str]:
    bits = path.split(".")
    return [".".join(bits[:i]) for i in range(1, len(bits) + 1)]


def uses_of(node: ast.AST | None) -> set[str]:
    """Variables and attribute paths read by an expression (`self.x.y` -> self, self.x, self.x.y)."""
    out: set[str] = set()
    if node is None:
        return out
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Name, ast.Attribute)) and isinstance(getattr(sub, "ctx", None), ast.Load):
            d = dotted(sub)
            if d:
                out.update(_prefixes(d))
    return out


def targets_of(t: ast.AST) -> tuple[set[str], set[str]]:
    """(defined, read) for an assignment target. `x[i] = v` defines x and reads x and i."""
    defs, uses = set(), set()
    if isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            d, u = targets_of(e)
            defs |= d
            uses |= u
    elif isinstance(t, ast.Starred):
        return targets_of(t.value)
    elif isinstance(t, (ast.Name, ast.Attribute)):
        d = dotted(t)
        if d:
            defs.add(d)
            if "." in d:
                uses.update(_prefixes(d)[:-1])
        if isinstance(t, ast.Attribute):
            uses |= uses_of(t.value)
    elif isinstance(t, ast.Subscript):
        base = dotted(t.value)
        if base:
            defs.add(base)
        uses |= uses_of(t.value) | uses_of(t.slice)
    return defs, uses


def calls_of(node: ast.AST | None) -> list[str]:
    """Dotted names of the functions called in an expression (`self.helper`, `parse`, ...)."""
    if node is None:
        return []
    return [d for sub in ast.walk(node) if isinstance(sub, ast.Call) and (d := dotted(sub.func))]


@dataclass
class Stmt:
    idx: int
    kind: str
    line: int
    end_line: int
    defs: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)


@dataclass
class Slice:
    stmts: list[Stmt]
    params: set[str]                         # parameters the criterion depends on

    @property
    def lines(self) -> list[int]:
        return sorted({s.line for s in self.stmts if s.kind not in ("entry", "exit")})

    @property
    def calls(self) -> list[str]:
        return list(dict.fromkeys(c for s in self.stmts for c in s.calls))


class FunctionFlow:
    """CFG + DDG + CDG of one function or method, built from its AST node."""

    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef):
        self.name = fn.name
        a = fn.args
        self.params = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs] + \
                      [p.arg for p in (a.vararg, a.kwarg) if p]
        self.stmts: list[Stmt] = []
        self.succ: dict[int, set[int]] = defaultdict(set)
        self.ctrl: dict[int, int | None] = {}            # CDG parent
        entry = self._node(fn, "entry", None, defs=set(self.params))
        exits = self._block(fn.body, {entry}, None, None)
        self.exit = self._node(fn, "exit", None)
        for p in exits:
            self.succ[p].add(self.exit)
        for i in list(self.succ):                          # renumber placeholder EXIT
            if EXIT in self.succ[i]:
                self.succ[i].discard(EXIT)
                self.succ[i].add(self.exit)
        self.pred: dict[int, set[int]] = defaultdict(set)
        for a_, bs in self.succ.items():
            for b in bs:
                self.pred[b].add(a_)
        self.ddg = self._reaching_definitions()

    # ---------------------------------------------------------------- CFG construction
    def _node(self, n, kind, ctrl, defs=None, uses=None, calls=None) -> int:
        i = len(self.stmts)
        self.stmts.append(Stmt(i, kind, getattr(n, "lineno", 0), getattr(n, "end_lineno", 0) or 0,
                               defs or set(), uses or set(), calls or []))
        self.ctrl[i] = ctrl
        return i

    def _link(self, preds, i):
        for p in preds:
            self.succ[p].add(i)

    def _block(self, body, preds, ctrl, loop) -> set[int]:
        for s in body:
            preds = self._stmt(s, preds, ctrl, loop)
        return preds

    def _stmt(self, s, preds, ctrl, loop) -> set[int]:
        if isinstance(s, ast.If):
            h = self._node(s, "if", ctrl, uses=uses_of(s.test), calls=calls_of(s.test))
            self._link(preds, h)
            return self._block(s.body, {h}, h, loop) | (self._block(s.orelse, {h}, h, loop) if s.orelse else {h})
        if isinstance(s, (ast.While, ast.For, ast.AsyncFor)):
            if isinstance(s, ast.While):
                h = self._node(s, "while", ctrl, uses=uses_of(s.test), calls=calls_of(s.test))
            else:
                d, u = targets_of(s.target)
                h = self._node(s, "for", ctrl, defs=d, uses=u | uses_of(s.iter), calls=calls_of(s.iter))
            self._link(preds, h)
            breaks: list[int] = []
            self._link(self._block(s.body, {h}, h, (h, breaks)), h)
            out = self._block(s.orelse, {h}, h, loop) if s.orelse else {h}
            return out | set(breaks)
        if isinstance(s, (ast.With, ast.AsyncWith)):
            defs, uses, calls = set(), set(), []
            for it in s.items:
                uses |= uses_of(it.context_expr)
                calls += calls_of(it.context_expr)
                if it.optional_vars is not None:
                    d, u = targets_of(it.optional_vars)
                    defs |= d
                    uses |= u
            h = self._node(s, "with", ctrl, defs, uses, calls)
            self._link(preds, h)
            return self._block(s.body, {h}, ctrl, loop)
        if isinstance(s, ast.Try) or type(s).__name__ == "TryStar":
            first = len(self.stmts)
            body_out = self._block(s.body, preds, ctrl, loop)
            in_body = set(range(first, len(self.stmts)))
            out = set()
            for hd in s.handlers:
                h = self._node(hd, "except", ctrl, defs={hd.name} if hd.name else set(), uses=uses_of(hd.type))
                self._link(set(preds) | in_body, h)         # any statement of the body may raise
                out |= self._block(hd.body, {h}, h, loop)
            out |= self._block(s.orelse, body_out, ctrl, loop) if s.orelse else body_out
            if s.finalbody:
                out = self._block(s.finalbody, out, ctrl, loop)
            return out
        if type(s).__name__ == "Match":
            h = self._node(s, "match", ctrl, uses=uses_of(s.subject), calls=calls_of(s.subject))
            self._link(preds, h)
            out = {h}
            for case in s.cases:
                out |= self._block(case.body, {h}, h, loop)
            return out
        if isinstance(s, (ast.Return, ast.Raise)):
            val = s.value if isinstance(s, ast.Return) else s.exc
            n = self._node(s, "return" if isinstance(s, ast.Return) else "raise", ctrl,
                           uses=uses_of(val), calls=calls_of(val))
            self._link(preds, n)
            self.succ[n].add(EXIT)
            return set()
        if isinstance(s, ast.Break):
            n = self._node(s, "break", ctrl)
            self._link(preds, n)
            if loop:
                loop[1].append(n)
            return set()
        if isinstance(s, ast.Continue):
            n = self._node(s, "continue", ctrl)
            self._link(preds, n)
            if loop:
                self.succ[n].add(loop[0])
            return set()
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            deco = set().union(*[uses_of(d) for d in s.decorator_list]) if s.decorator_list else set()
            n = self._node(s, "def", ctrl, defs={s.name}, uses=deco)
            self._link(preds, n)
            return {n}
        defs, uses = set(), set()
        if isinstance(s, ast.Assign):
            for t in s.targets:
                d, u = targets_of(t)
                defs |= d
                uses |= u
            uses |= uses_of(s.value)
        elif isinstance(s, (ast.AugAssign, ast.AnnAssign)):
            d, u = targets_of(s.target)
            defs |= d
            uses |= u | uses_of(s.value)
            if isinstance(s, ast.AugAssign):
                uses |= d
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            defs = {(a.asname or a.name).split(".")[0] for a in s.names}
        elif isinstance(s, (ast.Global, ast.Nonlocal, ast.Pass)):
            pass
        elif isinstance(s, ast.Delete):
            for t in s.targets:
                d, u = targets_of(t)
                defs |= d
                uses |= u
        else:                                               # Expr, Assert, ...
            uses = uses_of(s)
        n = self._node(s, type(s).__name__.lower(), ctrl, defs, uses, calls_of(s))
        self._link(preds, n)
        return {n}

    # ---------------------------------------------------------------- data flow
    def _reaching_definitions(self) -> dict[int, set[tuple[int, str]]]:
        """use statement -> {(defining statement, variable)} (classic iterative reaching definitions)."""
        n = len(self.stmts)
        gen = [{(i, v) for v in s.defs} for i, s in enumerate(self.stmts)]
        rd_in: list[set] = [set() for _ in range(n)]
        rd_out: list[set] = [set(g) for g in gen]
        work = list(range(n))
        while work:
            i = work.pop()
            new_in = set().union(*[rd_out[p] for p in self.pred[i]]) if self.pred[i] else set()
            killed = self.stmts[i].defs
            new_out = gen[i] | {(d, v) for d, v in new_in if v not in killed}
            rd_in[i] = new_in
            if new_out != rd_out[i]:
                rd_out[i] = new_out
                work.extend(self.succ[i])
        return {i: {(d, v) for d, v in rd_in[i] if v in self.stmts[i].uses} for i in range(n)}

    # ---------------------------------------------------------------- slicing
    def stmts_at(self, line: int) -> list[int]:
        """Innermost statement(s) covering a source line."""
        cover = [s for s in self.stmts if s.kind not in ("entry", "exit") and s.line <= line <= s.end_line]
        if not cover:
            return []
        best = min(s.end_line - s.line for s in cover)
        return [s.idx for s in cover if s.end_line - s.line == best]

    def returns(self) -> list[int]:
        return [s.idx for s in self.stmts if s.kind in ("return", "raise")] or \
               [p for p in self.pred[self.exit]]

    def backward_slice(self, criteria: list[int], variables: set[str] | None = None) -> Slice:
        """Statements that can affect the criteria (data + control dependence, transitively).
        `variables` restricts the first step to the dependences of those names."""
        seen: set[int] = set()
        work: list[tuple[int, set[str] | None]] = [(c, variables) for c in criteria]
        while work:
            i, only = work.pop()
            if i in seen and only is None:
                continue
            seen.add(i)
            for d, v in self.ddg.get(i, ()):
                if only is None or v in only or any(v.startswith(o + ".") or o.startswith(v + ".") for o in only):
                    if d not in seen:
                        work.append((d, None))
            c = self.ctrl.get(i)
            if c is not None and c not in seen:
                work.append((c, None))
        params = {v for i in seen for d, v in self.ddg.get(i, ()) if d == ENTRY} & set(self.params)
        return Slice([self.stmts[i] for i in sorted(seen)], params)

    def slice_from_line(self, line: int, variables: set[str] | None = None) -> Slice:
        return self.backward_slice(self.stmts_at(line), variables)

    def slice_from_returns(self) -> Slice:
        return self.backward_slice(self.returns())

    # ---------------------------------------------------------------- reporting
    def stats(self) -> dict:
        return {"statements": len(self.stmts) - 2,
                "cfg_edges": sum(len(v) for v in self.succ.values()),
                "data_edges": sum(len(v) for v in self.ddg.values()),
                "control_edges": sum(1 for c in self.ctrl.values() if c is not None)}


def function_node(tree: ast.Module, qualname: str):
    """`f` or `Class.method` (or nested `Class.Inner.method`) inside a parsed module."""
    body = tree.body
    node = None
    for part in qualname.split("."):
        node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            return None
        body = node.body
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def flow_of(src: str, qualname: str) -> FunctionFlow | None:
    try:
        node = function_node(ast.parse(src), qualname)
        return FunctionFlow(node) if node else None
    except (SyntaxError, RecursionError, ValueError):
        return None
