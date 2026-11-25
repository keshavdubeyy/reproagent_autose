"""Build repository_index.json: Layer 1 (knowledge building) for one repo at one commit.

    build_index(repo_path, commit=None) -> dict

Input: a Python repository path and its commit. A git checkout's HEAD must match the
commit; our exported snapshots have no .git, so the commit comes from the argument or
from the snapshot's .autose_snapshot marker.

Output schema (one JSON file):
  schema_version, repository{name, instance_id, commit, path, built_at, builder, stats}
  directories  [id]
  files        [{id, module, lines, is_test}]
  entities     [{id, kind: class|method|function, name, qualified_name, file,
                 start_line, end_line, parent, is_test, [bases]}]
  relationships[{source, target, type: contains|imports|invokes|inherits,
                 [resolution: scoped|global], [alias]}]
  tests        {files, classes, functions}   (ids)
  problems     {parse_errors, duplicate_ids}

Ids follow context-engineering-v2: "path/to/file.py" and "path/to/file.py:Class.method".
No source code is stored: readers take it from the snapshot by file and line range.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from pathlib import Path

from autose.repo_index import cev2_graph as cg
from autose.repo_index.tests_detect import is_test_class, is_test_file, is_test_function

SCHEMA_VERSION = 1
BUILDER = {"source": "context-engineering-v2@1c65307 scripts/build_code_and_call_graphs.py",
           "port": "autose/repo_index/cev2_graph.py", "fuzzy_search": True, "global_import": True}


def _git_head(repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() or None


def _resolve_commit(repo: Path, commit: str | None) -> tuple[str, dict]:
    marker_path = repo / ".autose_snapshot"
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else {}
    head = _git_head(repo) or marker.get("base_commit")
    if commit and head and not head.startswith(commit) and not commit.startswith(head):
        raise ValueError(f"{repo} is at {head}, not {commit}")
    found = commit or head
    if not found:
        raise ValueError(f"{repo} has no .git or .autose_snapshot: pass the commit")
    return found, marker


def _module_name(path: str) -> str:
    mod = path[:-3] if path.endswith(".py") else path
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    return mod.replace("/", ".")


def build_index(repo_path: str | Path, commit: str | None = None) -> dict:
    repo = Path(repo_path)
    commit, marker = _resolve_commit(repo, commit)
    t0 = time.time()
    nodes, edges, problems = cg.build_code_graph(str(repo))
    build_s = time.time() - t0

    files, entities, directories = [], [], []
    for nid, nd in nodes.items():
        if nd["type"] == cg.NODE_TYPE_DIRECTORY:
            directories.append(nid)
        elif nd["type"] == cg.NODE_TYPE_FILE:
            text = (repo / nid).read_text(encoding="utf-8")
            files.append({"id": nid, "module": _module_name(nid),
                          "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
                          "is_test": is_test_file(nid)})
    test_file = {f["id"]: f["is_test"] for f in files}

    def parent_of(nid: str) -> str:
        file, qual = nid.split(":", 1)
        return file if "." not in qual else f"{file}:{qual.rsplit('.', 1)[0]}"

    test_classes = set()
    for nid, nd in nodes.items():  # classes first: methods need to know their class
        if nd["type"] == cg.NODE_TYPE_CLASS and test_file.get(nid.split(":", 1)[0]):
            if is_test_class(nid.rsplit(".", 1)[-1].split(":")[-1], nd.get("bases", [])):
                test_classes.add(nid)

    for nid, nd in nodes.items():
        if nd["type"] not in (cg.NODE_TYPE_CLASS, cg.NODE_TYPE_FUNCTION):
            continue
        file, qual = nid.split(":", 1)
        name = qual.rsplit(".", 1)[-1]
        parent = parent_of(nid)
        parent_kind = "file" if parent == file else nodes[parent]["type"] if parent in nodes else "unknown"
        if nd["type"] == cg.NODE_TYPE_CLASS:
            kind, is_test = "class", nid in test_classes
        else:
            kind = "method" if nd.get("parent_type") == cg.NODE_TYPE_CLASS else "function"
            is_test = test_file.get(file, False) and is_test_function(name, parent_kind, parent in test_classes)
        ent = {"id": nid, "kind": kind, "name": name, "qualified_name": f"{_module_name(file)}.{qual}",
               "file": file, "start_line": nd["start_line"], "end_line": nd["end_line"],
               "parent": parent, "is_test": bool(is_test)}
        if kind == "class":
            ent["bases"] = nd.get("bases", [])
        entities.append(ent)

    kinds = Counter(e["kind"] for e in entities)
    stats = {
        "build_seconds": round(build_s, 1),
        "directories": len(directories), "files": len(files),
        "classes": kinds["class"], "methods": kinds["method"], "functions": kinds["function"],
        "relationships": dict(sorted(Counter(e["type"] for e in edges).items())),
        "call_resolution": dict(sorted(Counter(e.get("resolution") for e in edges
                                               if e["type"] == cg.EDGE_TYPE_INVOKES).items())),
        "test_files": sum(f["is_test"] for f in files),
        "test_functions": sum(e["is_test"] and e["kind"] != "class" for e in entities),
        "parse_errors": len(problems["parse_errors"]),
        "duplicate_ids": len(problems["duplicate_ids"]),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": {"name": marker.get("repo"), "instance_id": marker.get("instance_id"), "commit": commit,
                       "path": repo.as_posix(), "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "builder": BUILDER, "stats": stats},
        "directories": directories,
        "files": files,
        "entities": entities,
        "relationships": edges,
        "tests": {"files": [f["id"] for f in files if f["is_test"]],
                  "classes": sorted(test_classes),
                  "functions": [e["id"] for e in entities if e["is_test"] and e["kind"] != "class"]},
        "problems": problems,
    }


def write_index(index: dict, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return out
