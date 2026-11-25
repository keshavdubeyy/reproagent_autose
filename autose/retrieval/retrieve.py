"""Layer 2 retrieval: bug report + repository_index -> ranked candidate entities and files.

Sources (each a ranked list of non-test entities):
  mentions   names the report uses, resolved in the index          (autose.retrieval.mentions)
  bm25       lexical match of the report against entity code       (autose.retrieval.lexical)
  graph      neighbours of the top mention and bm25 hits            (autose.retrieval.graph)
  embedding  similarity to the report, over a pool of the hits above (autose.retrieval.semantic)
             plus every entity of the top files (optional: needs an Embedder)
They are fused by reciprocal rank fusion. Files are ranked by the fused scores of their entities.

Reads only the public bug report, the public code snapshot and the index.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from autose.retrieval import mentions
from autose.retrieval.graph import Graph
from autose.retrieval.lexical import BM25, entity_texts

RRF_K = 60
SOURCE_WEIGHT = {"mentions": 1.0, "bm25": 1.0, "graph": 0.5, "embedding": 1.0}
SEEDS_PER_SOURCE = 5
POOL_FILES = 5
POOL_MAX = 300


def fuse(sources: dict[str, list[str]], file_of: dict[str, str]) -> tuple[list[str], dict, dict, dict]:
    fused = defaultdict(float)
    ranks = defaultdict(dict)
    for name, ids in sources.items():
        for r, eid in enumerate(ids):
            fused[eid] += SOURCE_WEIGHT[name] / (RRF_K + r + 1)
            ranks[eid][name] = r + 1
    order = sorted(fused, key=lambda e: (-fused[e], e))
    per_file = defaultdict(list)
    for eid in order:
        per_file[file_of[eid]].append(fused[eid])
    file_score = {f: sum(sorted(s, reverse=True)[:3]) for f, s in per_file.items()}
    return order, fused, ranks, file_score


class Retriever:
    """Per-snapshot indexes, built once and reused for every query on that snapshot."""

    def __init__(self, index: dict, code_dir: str | Path, embedder=None):
        self.index = index
        self.resolver = mentions.Resolver(index)
        ids, docs = entity_texts(index, Path(code_dir))
        self.doc = dict(zip(ids, docs))
        self.bm25 = BM25(ids, docs)
        self.graph = Graph(index)
        self.embedder = embedder
        self.file_of = {e["id"]: e["file"] for e in index["entities"]}
        self.by_file = defaultdict(list)
        for i in ids:
            self.by_file[self.file_of[i]].append(i)

    def pool(self, sources: dict[str, list[str]], file_order: list[str]) -> list[str]:
        """Candidates for the embedding ranker: every hit so far, then the top files' entities."""
        seen = dict.fromkeys(i for ids in sources.values() for i in ids)
        for f in file_order[:POOL_FILES]:
            seen.update(dict.fromkeys(self.by_file.get(f, [])))
        return list(seen)[:POOL_MAX]

    def retrieve(self, bug_report, top_k: int = 50) -> dict:
        text = mentions.report_text(bug_report)
        ment = mentions.find(text, self.index, self.resolver)
        lex = self.bm25.rank(text, top_k=100)
        seeds = {}
        for rank, (eid, *_) in enumerate(ment[:SEEDS_PER_SOURCE]):
            seeds[eid] = max(seeds.get(eid, 0), 1.0 / (rank + 1))
        for rank, (eid, _) in enumerate(lex[:SEEDS_PER_SOURCE]):
            seeds[eid] = max(seeds.get(eid, 0), 1.0 / (rank + 1))
        walk = self.graph.expand(seeds)
        sources = {"mentions": [m[0] for m in ment], "bm25": [x[0] for x in lex], "graph": [w[0] for w in walk]}
        order, fused, ranks, file_score = fuse(sources, self.file_of)

        if self.embedder is not None:
            files_now = sorted(file_score, key=lambda f: (-file_score[f], f))
            pool = self.pool(sources, files_now)
            ranked = self.embedder.rank(text, pool, [self.doc[i] for i in pool])
            sources["embedding"] = [i for i, _ in ranked]
            order, fused, ranks, file_score = fuse(sources, self.file_of)

        return {
            "entities": [{"id": e, "score": round(fused[e], 5), "ranks": ranks[e]} for e in order[:top_k]],
            "files": [{"id": f, "score": round(s, 5)} for f, s in
                      sorted(file_score.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]],
            "sources": {k: v[:top_k] for k, v in sources.items()},
            "mention_reasons": {m[0]: m[2] for m in ment[:top_k]},
        }
