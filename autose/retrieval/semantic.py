"""Embedding similarity between a bug report and code entities, with a local model.

Port of context-engineering-v2@1c65307 src/utils/embeddings.py (build_embedding_index,
semantic_rank_by_embedding): document = node id + its code, L2-normalised vectors,
inner-product ranking.

AUTOSE changes:
  1. A local model through Ollama (nomic-embed-text, 768-d) replaces OpenAI
     text-embedding-3-large; nomic's "search_document:" / "search_query:" prefixes are used.
  2. Ranks a candidate pool (the other sources' hits and the entities of the top files)
     instead of every node: on this machine the model embeds about 6 entities per second,
     so a whole sympy snapshot (30K entities) would take 1.5 hours.
  3. Vectors are cached by the SHA-1 of the document text in one .npz file, not per
     repo+commit, so the 77 sympy snapshots share every unchanged function.
  4. numpy dot products replace FAISS (pools are a few hundred vectors).
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

import numpy as np

OLLAMA = "http://127.0.0.1:11434"  # not "localhost": on Windows that tries IPv6 first and waits 2 s
MODEL = "nomic-embed-text"
BATCH = 32


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Embedder:
    def __init__(self, cache_path: str | Path, model: str = MODEL, url: str = OLLAMA):
        self.model, self.url, self.cache_path = model, url, Path(cache_path)
        self.cache: dict[str, np.ndarray] = {}
        self.dirty = False
        self.calls = self.embedded = 0
        if self.cache_path.exists():
            with np.load(self.cache_path) as z:
                self.cache = dict(zip(z["keys"].tolist(), z["vectors"]))

    def _embed(self, texts: list[str]) -> np.ndarray:
        body = json.dumps({"model": self.model, "input": texts, "truncate": True}).encode()
        req = urllib.request.Request(f"{self.url}/api/embed", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            vecs = np.asarray(json.load(r)["embeddings"], dtype="float32")
        self.calls += 1
        return vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)

    def documents(self, docs: list[str]) -> np.ndarray:
        texts = [f"search_document: {d}" for d in docs]
        keys = [_key(t) for t in texts]
        todo = [i for i, k in enumerate(keys) if k not in self.cache]
        for s in range(0, len(todo), BATCH):
            part = todo[s:s + BATCH]
            for i, v in zip(part, self._embed([texts[i] for i in part])):
                self.cache[keys[i]] = v
            self.embedded += len(part)
            self.dirty = True
        return np.stack([self.cache[k] for k in keys]) if keys else np.zeros((0, 768), "float32")

    def query(self, text: str) -> np.ndarray:
        return self._embed([f"search_query: {text}"])[0]

    def rank(self, query: str, ids: list[str], docs: list[str]) -> list[tuple[str, float]]:
        if not ids:
            return []
        sims = self.documents(docs) @ self.query(query)
        order = np.argsort(-sims, kind="stable")
        return [(ids[i], round(float(sims[i]), 4)) for i in order]

    def save(self) -> None:
        if not self.dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_name(self.cache_path.stem + ".tmp.npz")
        keys = list(self.cache)
        np.savez(tmp, keys=np.array(keys), vectors=np.stack([self.cache[k] for k in keys]))
        tmp.replace(self.cache_path)
        self.dirty = False
