"""Layer 2 tests: mention extraction and resolution, BM25, graph walk, fusion, no leaks."""
import json
import math
from pathlib import Path
from textwrap import dedent

import pytest

from autose.repo_index import build_index
from autose.retrieval import Retriever, mentions
from autose.retrieval import graph as graph_mod
from autose.retrieval.lexical import BM25, tokenize

FILES = {
    "pkg/__init__.py": "",
    "pkg/geometry.py": dedent("""\
        class Point:
            def vel(self, frame):
                return self._vel_dict[frame]

            def pos_from(self, other):
                return 0


        def distance(a, b):
            return a.pos_from(b)
        """),
    "pkg/frames.py": dedent("""\
        from pkg.geometry import Point


        class ReferenceFrame:
            def ang_vel_in(self, other):
                return Point().vel(other)
        """),
    "tests/test_geometry.py": dedent("""\
        from pkg.geometry import Point


        def test_vel():
            Point().vel(None)
        """),
}


@pytest.fixture()
def repo(tmp_path):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def index(repo):
    return build_index(repo, "deadbeef")


def test_tokenize_splits_snake_and_camel():
    assert tokenize("ang_vel_in ReferenceFrame") == [
        "ang_vel_in", "ang", "vel", "in", "referenceframe", "reference", "frame"]


def test_extract_reads_frames_calls_and_code():
    text = ('Calling `Point.vel` fails:\n```\nP.vel(A)\n```\n'
            'File "/site-packages/pkg/geometry.py", line 3, in vel\nKeyError')
    found = mentions.extract(text)
    assert found["frames"] == [("/site-packages/pkg/geometry.py", 3, "vel")]
    assert "P.vel" in found["calls"] and "Point.vel" in found["code"]


def test_resolver_narrows_by_dotted_prefix_and_skips_tests(index):
    r = mentions.Resolver(index)
    assert r.name("Point.vel") == ["pkg/geometry.py:Point.vel"]
    assert r.name("geometry.distance") == ["pkg/geometry.py:distance"]
    assert all("tests/" not in i for i in r.name("test_vel") + r.name("vel"))
    assert r.frame("/abs/path/pkg/geometry.py", 3) == "pkg/geometry.py:Point.vel"


def test_find_ranks_traceback_and_named_entities(index):
    report = {"Description": "ReferenceFrame.ang_vel_in() works but P.vel(A) raises",
              "ObservedBehavior": ['File "pkg/geometry.py", line 3, in vel']}
    ranked = mentions.find(report, index)
    assert ranked[0][0] == "pkg/geometry.py:Point.vel" and "traceback" in ranked[0][2]
    assert "pkg/frames.py:ReferenceFrame.ang_vel_in" in [r[0] for r in ranked]


def test_names_with_too_many_matches_are_dropped(index, monkeypatch):
    monkeypatch.setattr(mentions, "MAX_MATCHES", 0)
    assert mentions.find("Point.vel() fails", index) == []


def test_bm25_matches_original_formula():
    bm = BM25(["a", "b"], ["alpha beta", "gamma"])
    (top, s), = bm.rank("alpha", top_k=1)
    idf = math.log((2 - 1 + 0.5) / (1 + 0.5) + 1)
    tf, dl, avgdl = 1, 2, 1.5
    expected = idf * tf * 2.5 / (tf + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl))
    assert top == "a" and s == pytest.approx(expected, abs=1e-4)


def test_graph_walk_skips_tests_and_noisy_global_edges(index, monkeypatch):
    g = graph_mod.Graph(index)
    near = dict((n, sc) for n, sc, _ in g.expand({"pkg/geometry.py:Point.vel": 1.0}))
    assert "pkg/geometry.py:Point" in near                      # contains, 1 hop
    assert "pkg/frames.py:ReferenceFrame.ang_vel_in" in near   # its caller
    assert not any(n.startswith("tests/") for n in near)
    monkeypatch.setattr(graph_mod, "MAX_GLOBAL", -1)
    g2 = graph_mod.Graph(index)
    assert not any(k == "invokes_global" for edges in g2.adj.values() for _, _, k in edges)


def test_retriever_fuses_sources(index, repo):
    out = Retriever(index, repo).retrieve({"Description": "Point.vel() should compute the velocity"})
    ids = [e["id"] for e in out["entities"]]
    assert ids[0] == "pkg/geometry.py:Point.vel"
    assert out["files"][0]["id"] == "pkg/geometry.py"
    assert set(out["sources"]) == {"mentions", "bm25", "graph"}
    assert not any(i.startswith("tests/") for i in ids)
    json.dumps(out)


def test_retrieval_code_never_reads_evaluation_data():
    src = "\n".join(p.read_text(encoding="utf-8") for p in Path("autose/retrieval").glob("*.py"))
    for banned in ("private", "test_specs", "gold_patch", "autose.evaluation", "external/"):
        assert banned not in src
    assert "FAIL_TO_PASS" not in src.replace("appended the FAIL_TO_PASS test", "")


class FakeEmbedder:
    """Prefers documents that mention 'pos_from'; records the pool it was given."""
    def rank(self, query, ids, docs):
        self.pool = ids
        return sorted(zip(ids, [float("pos_from" in d) for d in docs]), key=lambda x: -x[1])


def test_retriever_adds_embedding_ranker_over_pool(index, repo):
    fake = FakeEmbedder()
    out = Retriever(index, repo, fake).retrieve({"Description": "Point.vel() should compute the velocity"})
    assert "embedding" in out["sources"]
    assert "pkg/geometry.py:Point.pos_from" in fake.pool          # pulled in from a top file
    top = out["sources"]["embedding"][0]  # a document containing 'pos_from' (the class body does too)
    assert top in ("pkg/geometry.py:Point", "pkg/geometry.py:Point.pos_from", "pkg/geometry.py:distance")
    assert not any(i.startswith("tests/") for i in fake.pool)


def test_embedder_caches_by_text(tmp_path, monkeypatch):
    import numpy as np
    from autose.retrieval.semantic import Embedder
    calls = []

    def fake_embed(self, texts):
        calls.append(len(texts))
        v = np.array([[len(t), 1.0] for t in texts], dtype="float32")
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    monkeypatch.setattr(Embedder, "_embed", fake_embed)
    cache = tmp_path / "c.npz"
    e = Embedder(cache)
    e.documents(["a", "bb"])
    e.save()
    e2 = Embedder(cache)
    e2.documents(["a", "bb", "ccc"])
    assert calls == [2, 1] and len(e2.cache) == 3
