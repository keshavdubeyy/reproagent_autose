"""Layer 1 tests on a small fixture repo: entities, lines, relationships, test tagging."""
import json
import subprocess
from textwrap import dedent

import pytest

from autose.repo_index import build_index, write_index

FILES = {
    "pkg/__init__.py": "from .core import Base\n",
    "pkg/core.py": dedent("""\
        def helper():
            return 1


        class Base:
            def __init__(self):
                self.x = helper()

            def run(self):
                return self.step()

            def step(self):
                return 2
        """),
    "pkg/child.py": dedent("""\
        from pkg.core import Base, helper


        class Child(Base):
            def run(self):
                return helper()
        """),
    "pkg/bad.py": "def (:\n",
    "tests/test_core.py": dedent("""\
        import unittest
        from pkg.core import Base


        def test_helper():
            assert True


        class TestBase:
            def setup_method(self):
                pass

            def test_run(self):
                assert Base().run() == 2


        class BaseChecks(unittest.TestCase):
            def test_x(self):
                pass


        class Helper:
            def test_not_collected(self):
                pass
        """),
}
COMMIT = "abc123def456"


@pytest.fixture()
def repo(tmp_path):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / ".autose_snapshot").write_text(json.dumps(
        {"instance_id": "demo__pkg-1", "repo": "demo/pkg", "base_commit": COMMIT}), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def index(repo):
    return build_index(repo)


def ents(index):
    return {e["id"]: e for e in index["entities"]}


def rels(index, rtype):
    return {(r["source"], r["target"]): r for r in index["relationships"] if r["type"] == rtype}


def test_metadata_from_snapshot_marker(index):
    r = index["repository"]
    assert (r["name"], r["instance_id"], r["commit"]) == ("demo/pkg", "demo__pkg-1", COMMIT)
    assert index["schema_version"] == 1


def test_entities_kinds_and_lines(index):
    e = ents(index)
    assert e["pkg/core.py:helper"]["kind"] == "function"
    base = e["pkg/core.py:Base"]
    assert (base["kind"], base["start_line"], base["end_line"]) == ("class", 5, 13)
    init = e["pkg/core.py:Base.__init__"]  # dropped by the original builder; kept by ours
    assert (init["kind"], init["start_line"], init["end_line"], init["parent"]) == ("method", 6, 7, "pkg/core.py:Base")
    assert e["pkg/core.py:Base.run"]["qualified_name"] == "pkg.core.Base.run"
    assert e["pkg/child.py:Child"]["bases"] == ["Base"]


def test_lines_match_source(index, repo):
    for ent in index["entities"]:
        line = (repo / ent["file"]).read_text(encoding="utf-8").splitlines()[ent["start_line"] - 1]
        assert f"{'class' if ent['kind'] == 'class' else 'def'} {ent['name']}" in line


def test_containment_imports_inheritance_calls(index):
    contains = rels(index, "contains")
    assert ("pkg", "pkg/core.py") in contains
    assert ("pkg/core.py", "pkg/core.py:Base") in contains
    assert ("pkg/core.py:Base", "pkg/core.py:Base.run") in contains
    imports = rels(index, "imports")
    assert ("pkg/child.py", "pkg/core.py:Base") in imports
    assert ("pkg/__init__.py", "pkg/core.py:Base") in imports  # relative import resolved
    inherits = rels(index, "inherits")
    assert inherits[("pkg/child.py:Child", "pkg/core.py:Base")]["resolution"] == "scoped"
    invokes = rels(index, "invokes")
    assert invokes[("pkg/child.py:Child.run", "pkg/core.py:helper")]["resolution"] == "scoped"
    assert ("pkg/core.py:Base.__init__", "pkg/core.py:helper") in invokes
    assert ("pkg/core.py:Base.run", "pkg/core.py:Base.step") in invokes
    assert all(r["source"] != r["target"] for r in index["relationships"])


def test_no_duplicate_relationships(index):
    keys = [(r["source"], r["target"], r["type"]) for r in index["relationships"]]
    assert len(keys) == len(set(keys))


def test_test_tagging(index):
    t = index["tests"]
    assert t["files"] == ["tests/test_core.py"]
    assert set(t["classes"]) == {"tests/test_core.py:TestBase", "tests/test_core.py:BaseChecks"}
    assert set(t["functions"]) == {"tests/test_core.py:test_helper", "tests/test_core.py:TestBase.test_run",
                                   "tests/test_core.py:BaseChecks.test_x"}
    assert not ents(index)["pkg/core.py:Base.run"]["is_test"]


def test_parse_errors_reported(index):
    assert [p["file"] for p in index["problems"]["parse_errors"]] == ["pkg/bad.py"]
    assert index["repository"]["stats"]["parse_errors"] == 1


def test_no_source_code_stored(index):
    blob = json.dumps(index)
    assert "self.x = helper()" not in blob


def test_write_index_roundtrip(index, tmp_path):
    out = write_index(index, tmp_path / "out" / "repository_index.json")
    assert json.loads(out.read_text(encoding="utf-8")) == index


def test_src_layout_imports_resolve(tmp_path):
    (tmp_path / "src" / "lib2").mkdir(parents=True)
    (tmp_path / "src" / "lib2" / "__init__.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from lib2 import f\n\n\ndef g():\n    return f()\n", encoding="utf-8")
    index = build_index(tmp_path, "deadbeef")
    assert ("app.py", "src/lib2/__init__.py:f") in rels(index, "imports")
    assert rels(index, "invokes")[("app.py:g", "src/lib2/__init__.py:f")]["resolution"] == "scoped"


def test_git_commit_must_match(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    head = git("rev-parse", "HEAD").stdout.strip()
    assert build_index(tmp_path)["repository"]["commit"] == head
    assert build_index(tmp_path, head[:7])["repository"]["commit"] == head[:7]
    with pytest.raises(ValueError):
        build_index(tmp_path, "0" * 40)
