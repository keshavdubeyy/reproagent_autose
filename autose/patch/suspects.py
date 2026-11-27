"""Where else the fix must go, when a patch leaves an example line still wrong without an
exception to follow (and in addition to the traceback page faults in loop.Job.page_faults).

A 4B model cannot debug this itself (mini-swe-agent leaves it to a large model's print
debugging; context-engineering-v2 to a large model walking the call graph). The harness does the
debugging and hands the model the conclusions:
  A  same pattern   the lines the patch changed, searched for elsewhere: copied code with the same
                    mistake is a classic second location
  C  run path       the repository functions that ran for the still-wrong line (per line, under the
                    profiler), minus those that also ran for lines that are now right, ranked by
                    call-graph distance to the located code (Layer 1 invokes edges), same file and
                    Layer 2 rank
  D  value trail    the arguments and return values of the located code and its neighbours on the
                    still-wrong line, before and after the patch: which function still returns the
                    wrong value, and which ones the patch did not change
(B, the still-wrong lines themselves, is check.compare's "wrong".)
"""
from __future__ import annotations

import difflib
import re

from autose.patch import check
from autose.trace.runner import EntityMap

TRIVIAL = {"return", "else:", "pass", "try:", "break", "continue", "return None", "return self", "return rv",
           "return expr", "raise", "finally:", "except:", ")", "]", "}", "return True", "return False"}
MIN_PATTERN_CHARS = 10
MAX_WATCH = 25
TRAIL_LINES = 8
RUN_PATH_RANK = 20   # a run-path suspect not near the located code must be in Layer 2's top 20


def _code(l: str) -> str:
    return re.sub(r"\s+", " ", l.split("#")[0]).strip()


def changed_lines(old: str, new: str) -> list[str]:
    """Lines the patch removed or rewrote, normalised; trivial ones left out."""
    out = []
    for l in difflib.ndiff(old.splitlines(), new.splitlines()):
        if l.startswith("- "):
            c = _code(l[2:])
            if len(c) >= MIN_PATTERN_CHARS and c not in TRIVIAL and c not in out:
                out.append(c)
    return out


class Suspects:
    def __init__(self, job):
        self.job = job
        self.ents = {e["id"]: e for e in job.index["entities"]}
        self.emap = EntityMap(job.index)
        self._texts: dict[str, list[str]] = {}
        self._before: dict[tuple, dict] = {}

    # ---------- helpers ----------
    def lines(self, f: str, files_now: dict) -> list[str]:
        if f in files_now:
            return files_now[f].splitlines()
        if f not in self._texts:
            try:
                self._texts[f] = self.job.original(f).splitlines()
            except (OSError, UnicodeDecodeError):
                self._texts[f] = []
        return self._texts[f]

    def known(self) -> set:
        j = self.job
        return {t["id"] for t in j.targets} | {m["id"] for ms in j.members.values() for m in ms}

    def inside_target(self, eid: str | None) -> bool:
        ids = {t["id"] for t in self.job.targets}
        while eid:
            if eid in ids:
                return True
            eid = self.ents.get(eid, {}).get("parent")
        return False

    def editable(self, eid: str | None) -> bool:
        e = self.ents.get(eid or "")
        return bool(e) and not e["is_test"] and e["kind"] in ("function", "method")

    def generic(self, eid: str) -> bool:
        """Plumbing that runs for nearly everything: constructors and other dunder methods, error
        classes. On the pilot these were the run-path suspects of every wrong line."""
        e = self.ents[eid]
        parent = self.ents.get(e.get("parent") or "", {}).get("name", "")
        return (e["name"].startswith("__") and e["name"].endswith("__")) or \
            parent.endswith(("Error", "Exception", "Warning"))

    def neighbours(self) -> dict[str, str]:
        """Functions one call away from the located code: id -> 'called by X' / 'calls X'."""
        ids = {t["id"] for t in self.job.targets}
        out = {}
        for r in self.job.index["relationships"]:
            if r["type"] != "invokes":
                continue
            if r["source"] in ids and r["target"] not in ids:
                out.setdefault(r["target"], f"called by `{self.ents.get(r['source'], {}).get('name', r['source'])}`")
            elif r["target"] in ids and r["source"] not in ids:
                out.setdefault(r["source"], f"calls `{self.ents.get(r['target'], {}).get('name', r['target'])}`")
        return out

    # ---------- A ----------
    def same_pattern(self, files_now: dict) -> tuple[list[tuple[str, str]], list[str]]:
        """(functions elsewhere with a changed line in them, with the reason) and notes about
        copies left inside the code already being edited."""
        pats: dict[str, str] = {}
        for f, new in files_now.items():
            for c in changed_lines(self.job.original(f), new):
                pats.setdefault(c, f)
        if not pats:
            return [], []
        files = sorted(self.job.files) + [x["id"] for x in self.job.index["files"]
                                          if not x["is_test"] and x["id"].endswith(".py") and x["id"] not in self.job.files]
        found, notes, known = [], [], self.known()
        for f in files:
            for n, l in enumerate(self.lines(f, files_now), 1):
                c = _code(l)
                if c not in pats:
                    continue
                eid = self.emap.innermost(f, n)
                if self.inside_target(eid) or eid in known:
                    notes.append(f"`{c[:90]}` (the code you changed) is still at {f} line {n}")
                elif self.editable(eid) and eid not in [x for x, _ in found]:
                    found.append((eid, f"has `{c[:90]}`, the same code you changed in {pats[c]}"))
            if len(found) >= 4:
                break
        return found, notes[:3]

    # ---------- C + D ----------
    def watch_list(self, extra: list[str] = ()) -> list[list]:
        j, nb = self.job, self.neighbours()
        ids = [t["id"] for t in j.targets if t["kind"] in ("function", "method")] + list(extra)
        rank = j.layer2
        ids += sorted((e for e in nb if self.editable(e)), key=lambda e: rank.get(e, 10 ** 6))
        ids = list(dict.fromkeys(i for i in ids if i in self.ents))[:MAX_WATCH]
        return [[self.ents[i]["file"], self.ents[i]["start_line"], self.ents[i]["name"]] for i in ids]

    def probe(self, files_now: dict, watch: list) -> dict:
        return check.probe_run(self.job.repo, self.job.snapshot, self.job.stmts, files_now, watch)

    def before(self, watch: list) -> dict:
        key = tuple(map(tuple, watch))
        if key not in self._before:
            self._before[key] = self.probe({}, watch)
        return self._before[key]

    def ran(self, probe: dict, i: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for f, l, n, cnt in probe.get("per", {}).get(str(i), []):
            eid = self.emap(f, l, n)
            if eid:
                out[eid] = out.get(eid, 0) + cnt
        return out

    def run_path(self, c: dict, after: dict) -> list[tuple[str, str]]:
        wrong = c.get("wrong") or []
        right = [r["i"] for r in c["rows"] if (r.get("fixed") or r.get("expected")) and not r.get("wrong")]
        bad: dict[str, int] = {}
        for i in wrong:
            for e, n in self.ran(after, i).items():
                bad[e] = bad.get(e, 0) + n
        good = set().union(*[set(self.ran(after, i)) for i in right]) if right else set()
        nb, rank, known = self.neighbours(), self.job.layer2, self.known()
        files = self.job.files
        base = self.job.baseline
        cands = [e for e in bad if e not in good and e not in known and e not in base and self.editable(e)
                 and not self.inside_target(e) and not self.generic(e)
                 and (e in nb or self.ents[e]["file"] in files or rank.get(e, 10 ** 6) < RUN_PATH_RANK)]
        cands.sort(key=lambda e: (e not in nb, self.ents[e]["file"] not in files, rank.get(e, 10 ** 6), bad[e]))
        stmt = self.job.stmts[wrong[0]].strip().splitlines()[-1][:80] if wrong else ""
        return [(e, f"runs for `{stmt}`, which is still wrong" + (f"; {nb[e]}" if e in nb else "")) for e in cands[:2]]

    def trail_text(self, c: dict, before: dict, after: dict) -> str:
        out = []
        for i in (c.get("wrong") or [])[:2]:
            b = {(f, n): (a, r) for f, l, n, a, r in before.get("trail", {}).get(str(i), [])}
            rows = after.get("trail", {}).get(str(i), [])
            if not rows:
                continue
            stmt = self.job.stmts[i].strip().splitlines()[-1][:80]
            out.append(f"While running `{stmt}` (still wrong), these functions returned:")
            seen = set()
            for f, l, n, args, ret in rows:
                if (f, n) in seen:
                    continue
                seen.add((f, n))
                old = b.get((f, n))
                when = ("unchanged by your patch" if old and old[1] == ret
                        else f"before your patch: {old[1]}" if old else "did not run before your patch")
                out.append(f"- {n}({args[:100]}) returned {ret}  ({when})")
                if len(out) >= TRAIL_LINES + 1:
                    break
        return "\n".join(out)

    def investigate(self, c: dict, files_now: dict) -> tuple[list[tuple[str, str]], str]:
        """(functions to page in, with reasons; text for the model) for a checked patch."""
        if not c.get("wrong"):
            return [], ""
        pattern, notes = self.same_pattern(files_now)
        watch = self.watch_list([e for e, _ in pattern])
        after = self.probe(files_now, watch)
        path = self.run_path(c, after)
        trail = self.trail_text(c, self.before(watch), after)
        found, seen = [], set()
        for e, why in pattern + path:
            if e not in seen:
                seen.add(e)
                found.append((e, why))
        text = "\n".join(filter(None, ["\n".join(f"Note: {n}." for n in notes), trail]))
        return found, text
