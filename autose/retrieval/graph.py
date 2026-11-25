"""Graph walk from seed entities over the repository_index relationships.

Port of context-engineering-v2@1c65307:
  src/graph/loader.py           build_edge_adjacency (undirected: both edge directions)
  src/tools/code_navigation.py  get_subgraph_internal (BFS, hops=2, max_nodes=40)

AUTOSE changes (the original walk treats every edge alike, which floods the result on our
graph, where one sympy method can have 600 name-only call links):
  1. Only entity-to-entity edges: invokes, inherits, and contains between a class and its
     methods. Files, directories and imports are left out.
  2. Each step multiplies the score by an edge weight: scoped call / inheritance / containment
     1.0, a name-only ("global") call 0.3, and by DECAY per hop.
  3. A node's global call edges are skipped when it has more than MAX_GLOBAL of them.
  4. Several seeds walk at once; a node keeps the best score any seed gives it.
"""
from __future__ import annotations

import heapq
from collections import defaultdict

WEIGHT = {"scoped": 1.0, "global": 0.3}
DECAY = 0.5
MAX_GLOBAL = 15


class Graph:
    def __init__(self, index: dict):
        entity = {e["id"]: e for e in index["entities"]}
        self.is_test = {i: e["is_test"] for i, e in entity.items()}
        self.adj: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
        global_out = defaultdict(list)
        for r in index["relationships"]:
            s, t, kind = r["source"], r["target"], r["type"]
            if s not in entity or t not in entity:
                continue  # file / directory ends
            if kind == "invokes" and r.get("resolution") == "global":
                global_out[s].append(t)
                continue
            if kind in ("invokes", "inherits", "contains"):
                self.adj[s].append((t, 1.0, kind))
                self.adj[t].append((s, 1.0, kind))
        for s, targets in global_out.items():
            if len(targets) > MAX_GLOBAL:
                continue
            for t in targets:
                self.adj[s].append((t, WEIGHT["global"], "invokes_global"))
                self.adj[t].append((s, WEIGHT["global"], "invokes_global"))

    def expand(self, seeds: dict[str, float], hops: int = 2, max_nodes: int = 40) -> list[tuple[str, float, str]]:
        """Best-first walk: [(entity_id, score, 'seed' | edge kind it was reached by)], seeds excluded."""
        best = dict(seeds)
        via = {s: "seed" for s in seeds}
        heap = [(-sc, 0, s) for s, sc in seeds.items()]
        heapq.heapify(heap)
        done = set()
        while heap and len(done) < max_nodes + len(seeds):
            neg, depth, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if depth >= hops:
                continue
            for nb, w, kind in self.adj.get(node, ()):
                if self.is_test.get(nb, True):
                    continue
                sc = -neg * w * DECAY
                if sc > best.get(nb, 0):
                    best[nb], via[nb] = sc, kind
                    heapq.heappush(heap, (-sc, depth + 1, nb))
        out = [(n, round(best[n], 4), via[n]) for n in done if n not in seeds]
        return sorted(out, key=lambda x: (-x[1], x[0]))[:max_nodes]
