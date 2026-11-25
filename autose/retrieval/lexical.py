"""BM25 over the index's non-test classes, methods and functions.

Port of context-engineering-v2@1c65307 src/utils/bm25.py (bm25_prepare_candidates, bm25_rank_query):
same document (node id + first 1000 characters of its code), same k1=1.5, b=0.75 and idf.

AUTOSE changes:
  1. Code comes from the snapshot by file and line range (the index stores no code).
  2. Tokens are also split on '_' and camelCase ('ang_vel_in' -> ang_vel_in, ang, vel, in),
     so a report saying "velocity of a point" can meet `Point.vel`.
  3. An inverted index replaces the per-query loop over every document (same scores, faster).
  4. The query is only the public bug report: the original appended the FAIL_TO_PASS test
     names, which are evaluation data an agent must not see.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z0-9_]+")
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
DOC_CHARS = 1000


def tokenize(text: str) -> list[str]:
    out = []
    for tok in TOKEN.findall(text):
        out.append(tok.lower())
        subs = [s.lower() for part in tok.split("_") for s in CAMEL.findall(part)]
        if len(subs) > 1:
            out.extend(subs)
    return out


def entity_texts(index: dict, code_dir: Path) -> tuple[list[str], list[str]]:
    """(ids, documents) for every non-test class / method / function."""
    by_file = defaultdict(list)
    for e in index["entities"]:
        if not e["is_test"]:
            by_file[e["file"]].append(e)
    ids, docs = [], []
    for f, ents in by_file.items():
        try:
            lines = (code_dir / f).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        for e in ents:
            code = "\n".join(lines[e["start_line"] - 1:e["end_line"]])
            ids.append(e["id"])
            docs.append(f"{e['id']} {code[:DOC_CHARS]}")
    return ids, docs


class BM25:
    def __init__(self, ids: list[str], docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.ids, self.k1, self.b = ids, k1, b
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths = []
        for i, doc in enumerate(docs):
            toks = tokenize(doc)
            self.lengths.append(len(toks))
            for t, tf in Counter(toks).items():
                self.postings[t].append((i, tf))
        n = len(docs)
        self.avgdl = sum(self.lengths) / max(n, 1)
        self.idf = {t: math.log((n - len(p) + 0.5) / (len(p) + 0.5) + 1) for t, p in self.postings.items()}

    @classmethod
    def from_index(cls, index: dict, code_dir: Path) -> "BM25":
        return cls(*entity_texts(index, code_dir))

    def rank(self, query: str, top_k: int = 100) -> list[tuple[str, float]]:
        scores = defaultdict(float)
        for qt, qtf in Counter(tokenize(query)).items():  # repeated terms count qtf times, as in the original
            idf = self.idf.get(qt)
            if idf is None:
                continue
            for i, tf in self.postings[qt]:
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / max(self.avgdl, 1))
                scores[i] += qtf * idf * tf * (self.k1 + 1) / max(norm, 1e-9)
        best = sorted(scores.items(), key=lambda kv: (-kv[1], self.ids[kv[0]]))[:top_k]
        return [(self.ids[i], round(s, 4)) for i, s in best]
