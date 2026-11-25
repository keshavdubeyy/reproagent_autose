"""Code entities named in a bug report, resolved to repository_index ids.

Port of context-engineering-v2@1c65307:
  src/tools/classify.py         _regex_extract_programming_entities (methods, classes, files)
  src/tools/code_navigation.py  _fuzzy_find_node / _find_ambiguous_matches (name -> node id)

AUTOSE changes:
  1. Regex only (no LLM classifier), so this step is deterministic and costs no model call.
  2. Also reads traceback frames (File "x.py", line N, in f), `backticked` names, names inside
     code blocks, dotted paths, and capitalised words that are class names in the index.
  3. A name resolves to every matching entity with weight 1/len(matches) instead of the first
     match; names matching more than MAX_MATCHES entities (e.g. __init__) are dropped.
  4. Test entities are never returned (the original skipped them with a weaker path check).
"""
from __future__ import annotations

import re
from collections import defaultdict

MAX_MATCHES = 25

CALL = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")          # original: methods
CLASS_DEF = re.compile(r"\bclass\s+([A-Z]\w*)")                         # original: classes
PY_FILE = re.compile(r"\b([\w./-]+\.py)\b")                              # original: other mentions
FRAME = re.compile(r'File "([^"]+\.py)", line (\d+)(?:, in (\w+))?')
BACKTICK = re.compile(r"`([^`\n]+)`")
FENCE = re.compile(r"```.*?```", re.S)
IDENT = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\b")
CODEISH = re.compile(r"_|\.|[a-z][A-Z]")  # snake_case, dotted, camelCase


def report_text(bug_report) -> str:
    """Flatten a bug report (str or CE-v2 style dict of str / list) into one text."""
    if isinstance(bug_report, str):
        return bug_report
    parts = []
    for v in bug_report.values():
        parts.extend(map(str, v) if isinstance(v, list) else [str(v)])
    return "\n".join(parts)


def extract(text: str) -> dict:
    """Names the report mentions, by how they appear."""
    code_blocks = " ".join(FENCE.findall(text)) + " " + " ".join(BACKTICK.findall(text))
    words = IDENT.findall(text)
    return {
        "frames": [(f, int(n), fn) for f, n, fn in FRAME.findall(text)],
        "files": sorted(set(PY_FILE.findall(text))),
        "calls": sorted(set(CALL.findall(text))),
        "classes": sorted(set(CLASS_DEF.findall(text))),
        "code": sorted(set(IDENT.findall(code_blocks))),
        "codeish": sorted({w for w in words if CODEISH.search(w)}),
        "capitalised": sorted({w for w in words if w[:1].isupper() and "." not in w}),
    }


class Resolver:
    """Name -> non-test entity ids, over one repository_index."""

    def __init__(self, index: dict):
        self.by_name = defaultdict(list)
        self.by_file = defaultdict(list)
        self.entities = {}
        for e in index["entities"]:
            if e["is_test"]:
                continue
            self.entities[e["id"]] = e
            self.by_name[e["name"]].append(e)
            self.by_file[e["file"]].append(e)
        self.files = [f["id"] for f in index["files"] if not f["is_test"]]
        self.class_names = {e["name"] for e in self.entities.values() if e["kind"] == "class"}

    def name(self, dotted: str) -> list[str]:
        """'a.b.Cls.meth' -> ids whose name is 'meth', narrowed by the dotted prefix when it fits."""
        parts = [p for p in dotted.split(".") if p]
        if not parts:
            return []
        cands = self.by_name.get(parts[-1], [])
        for n in range(len(parts), 1, -1):  # longest dotted suffix that still matches
            suffix = "." + ".".join(parts[-n:])
            narrowed = [e for e in cands if ("." + e["qualified_name"]).endswith(suffix)]
            if narrowed:
                return [e["id"] for e in narrowed]
        return [e["id"] for e in cands]

    def file(self, path: str) -> str | None:
        path = path.replace("\\", "/")
        hits = [f for f in self.files if path.endswith(f) or f.endswith(path.lstrip("./"))]
        return min(hits, key=len) if hits else None

    def frame(self, path: str, line: int) -> str | None:
        f = self.file(path)
        inside = [e for e in self.by_file.get(f, []) if e["start_line"] <= line <= e["end_line"]]
        return min(inside, key=lambda e: e["end_line"] - e["start_line"])["id"] if inside else None


def find(bug_report, index: dict, resolver: Resolver | None = None) -> list[tuple[str, float, list[str]]]:
    """Ranked [(entity_id, score, reasons)] for the entities a report names."""
    r = resolver or Resolver(index)
    found = extract(report_text(bug_report))
    score = defaultdict(float)
    why = defaultdict(set)

    def add(ids, weight, reason):
        if not ids or len(ids) > MAX_MATCHES:
            return
        for i in ids:
            score[i] += weight / len(ids)
            why[i].add(reason)

    for path, line, _ in found["frames"]:
        add([x] if (x := r.frame(path, line)) else [], 3.0, "traceback")
    for path in found["files"]:
        f = r.file(path)
        if f:
            add([e["id"] for e in r.by_file[f] if e["parent"] == f], 0.5, "file")
    for kind, weight in (("calls", 2.0), ("classes", 2.0), ("code", 1.0), ("codeish", 1.0)):
        for name in found[kind]:
            add(r.name(name), weight, kind)
    for name in found["capitalised"]:
        if name in r.class_names:
            add([e["id"] for e in r.by_name[name] if e["kind"] == "class"], 1.0, "class_name")
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(i, round(s, 4), sorted(why[i])) for i, s in ranked]
