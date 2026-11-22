"""Hybrid retrieval for Bug Understanding: which files / symbols does a bug report point at?

Modes (the retrieval part of the design's evaluation configurations):
  bm25    lexical only: BM25 over the knowledge package                       (lexical RAG)
  hybrid  bm25 + exact identifier matches + file paths + traceback frames      (config C)
  graph   hybrid + one hop of graph expansion (callees, callers, class)        (config D)

Every candidate keeps the evidence that put it there, so agents (and the paper) can cite why.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from autose.agent.models import BugReport
from .index import KnowledgeIndex, subtokens

STOP = set("""a an the and or but if then else when while for to of in on at by with from as is are was were be been
being this that these those it its i we you he she they them our your my me not no yes do does did done can could
should would will shall may might must have has had having get got so such than too very just also only into over
under out up down about after before again further here there all any both each few more most other some same own
what which who whom why how use used using work works working expected actual behavior behaviour error issue bug
example code file line return returns returned value values function method class python version please thanks
however following above below like see seems want need""".split())

_IDENT = re.compile(r"`([^`\n]{2,80})`|\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b|"
                    r"\b([A-Za-z_]*[a-z][A-Z][A-Za-z0-9_]*|[A-Za-z]+_[A-Za-z0-9_]+)\b|\b([A-Za-z_][A-Za-z0-9_]*)\(")
_FRAME = re.compile(r'File "([^"]+\.py)", line (\d+), in (\w+)')
_PATH = re.compile(r"\b((?:[\w-]+/)+[\w-]+\.py)\b")


@dataclass
class Candidate:
    sid: int
    score: float = 0.0
    why: list[str] = field(default_factory=list)


@dataclass
class Retrieval:
    symbols: list[Candidate]
    files: list[tuple[str, float]]       # ranked (path, score)


def report_text(r: BugReport) -> str:
    return f"{r.title}\n{r.body}"


def identifiers(text: str) -> list[str]:
    out = []
    for m in _IDENT.finditer(text):
        tok = next(g for g in m.groups() if g)
        for part in re.split(r"[^\w.]+", tok):
            out += [p for p in part.split(".") if len(p) > 2]
    return list(dict.fromkeys(out))


class HybridRetriever:
    def __init__(self, index: KnowledgeIndex, mode: str = "graph"):
        assert mode in ("bm25", "hybrid", "graph")
        self.ix, self.mode = index, mode

    def search(self, report: BugReport, k_symbols: int = 20, k_files: int = 10) -> Retrieval:
        text = report_text(report)
        idents = identifiers(text)
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]+", text)]
        terms = [t for w in words + idents for t in subtokens(w) if t not in STOP and len(t) > 2]
        cands: dict[int, Candidate] = defaultdict(lambda: Candidate(0))

        def add(sid: int, s: float, why: str):
            c = cands[sid]
            c.sid, c.score = sid, c.score + s
            if why not in c.why:
                c.why.append(why)

        hits = self.ix.bm25(terms)
        top = hits[0][1] if hits else 1.0
        for sid, s in hits:
            add(sid, s / top, "bm25")

        if self.mode in ("hybrid", "graph"):
            for name in idents:                         # exact definition names, rare names weigh more
                ids = self.ix.by_name(name)
                if ids and len(ids) <= 25:
                    w = 1.2 / math.log2(1 + len(ids))
                    for sid in ids:
                        add(sid, w, f"name:{name}")
            for path in _PATH.findall(text):            # file paths written in the report
                for sid in self.ix.by_path_suffix(path)[:200]:
                    add(sid, 0.6, f"path:{path}")
            frames = _FRAME.findall(text)               # traceback frames: the deepest ones matter most
            for depth, (path, _line, func) in enumerate(reversed(frames)):
                tail = "/".join(path.replace("\\", "/").split("/")[-2:])
                for sid in self.ix.by_path_suffix(tail):
                    row = self.ix.symbol(sid)
                    if row["name"] == func:
                        add(sid, 2.0 / (1 + depth), f"traceback:{tail}:{func}")

        # Graph evidence is kept apart from the report's own evidence. Measured on 118 SWE-bench bugs,
        # pulling neighbours straight into the symbol list finds the right FILE more often but pushes the
        # right FUNCTION down (callers/callees crowd it out). So: the graph decides file ranking in full,
        # while for symbols it only reinforces candidates the report already points at.
        graph: dict[int, float] = defaultdict(float)
        if self.mode == "graph":
            seeds = sorted(cands.values(), key=lambda c: -c.score)[:15]
            for c in seeds:
                for nid, rel in self.ix.neighbours(c.sid):
                    graph[nid] += self.GRAPH_W * c.score
                    if nid in cands and f"graph:{rel}" not in cands[nid].why:
                        cands[nid].why.append(f"graph:{rel}")

        def file_scores(items):
            files: dict[str, float] = {}
            for sid, sc in sorted(items, key=lambda t: -t[1])[:200]:
                path = self.ix.symbol(sid)["path"]
                files[path] = files.get(path, 0.0) + sc * (1.0 if path not in files else 0.3)
            return sorted(files.items(), key=lambda t: -t[1])[:k_files]

        base = {sid: c.score for sid, c in cands.items()}
        full = dict(base)
        for sid, g in graph.items():
            full[sid] = full.get(sid, 0.0) + g
        for sid, c in cands.items():                    # symbols: graph only corroborates
            c.score += self.CORROBORATE_W * graph.get(sid, 0.0)
        ranked = sorted(cands.values(), key=lambda c: -c.score)
        return Retrieval(ranked[:k_symbols], file_scores(full.items()))

    GRAPH_W = 0.25          # neighbour gets this share of its seed's score (file ranking)
    CORROBORATE_W = 0.2     # share of that graph score a report-backed symbol keeps
