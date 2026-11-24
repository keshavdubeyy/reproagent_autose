"""Shared blackboard (ADR 0008): every agent step publishes one schema-checked JSON artifact.

    runs/<run>/blackboard/<bug_id>/
        01_BUG_SPEC.json        Bug Understanding: spec, retrieved code, accepted reproduction test
        02_CHANGE_PLAN.json     Code Modification: localised candidates, changed definitions, diff, patch hash
        03_TEST_REPORT.json     Testing: result for exactly that patch hash
        ..._REVIEW_REPORT.json  Review: APPROVE / REJECT / NEEDS_HUMAN for a patch hash
        ..._FROZEN_PATCH.json   the final patch, frozen by hash
        events.jsonl            one line per published artifact (the event log)

Rules enforced on write (the architecture's "never relaxed" rules):
  * every artifact matches its schema (required fields and types)
  * a TEST_REPORT or REVIEW_REPORT refers to a patch hash that a CHANGE_PLAN published
  * a patch can be frozen only if a TEST_REPORT accepted that exact hash (and no REVIEW_REPORT rejected it)
Without a folder the board lives in memory (unit tests, local runs that don't need files).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

NoneType = type(None)
SCHEMAS: dict[str, dict[str, tuple]] = {
    "BUG_SPEC": {"summary": (str,), "expected": (str,), "observed": (str,), "failure_kind": (str,),
                 "retrieved": (list,), "verdict": (str,), "reproduction_test": (str, NoneType),
                 "attempts": (int,), "evidence": (str,)},
    "CHANGE_PLAN": {"attempt": (int,), "patch_kind": (str,), "candidates": (list,), "changed": (list,),
                    "files": (list,), "patch_sha": (str,), "diff": (str,), "applies": (bool,)},
    "TEST_REPORT": {"attempt": (int,), "patch_sha": (str,), "category": (str,), "accepted": (bool,),
                    "repro_status": (str, NoneType), "regressions": (list,), "feedback": (str,)},
    # TEST_REPORT also carries "regression_checked" (bool/None) since 24 Nov; optional, so older boards stay valid.
    "REVIEW_REPORT": {"patch_sha": (str,), "decision": (str,), "reasons": (list,), "by": (str,)},
    "FROZEN_PATCH": {"patch_sha": (str,), "diff": (str,), "test_report": (int,)},
}
DECISIONS = {"APPROVE", "REJECT", "NEEDS_HUMAN"}


class BlackboardError(ValueError):
    pass


def patch_sha(diff: str) -> str:
    return hashlib.sha256((diff or "").encode("utf-8")).hexdigest()[:16]


class Blackboard:
    def __init__(self, root: Path | None, bug_id: str):
        self.dir = Path(root) / bug_id if root else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.items: list[dict] = []

    # ---------------------------------------------------------------- write
    def post(self, kind: str, author: str, data: dict) -> int:
        """Validate and publish an artifact. Returns its sequence number."""
        schema = SCHEMAS.get(kind)
        if schema is None:
            raise BlackboardError(f"unknown artifact kind {kind}")
        for key, types in schema.items():
            if key not in data:
                raise BlackboardError(f"{kind}: missing field {key!r}")
            if not isinstance(data[key], types):
                raise BlackboardError(f"{kind}.{key}: expected {'/'.join(t.__name__ for t in types)}, "
                                      f"got {type(data[key]).__name__}")
        clash = {"seq", "kind", "author", "at"} & set(data)
        if clash:
            raise BlackboardError(f"{kind}: reserved field(s) {sorted(clash)}")
        if kind in ("TEST_REPORT", "REVIEW_REPORT") and data["patch_sha"] not in self._published_patches():
            raise BlackboardError(f"{kind} refers to patch {data['patch_sha']} that no CHANGE_PLAN published")
        if kind == "REVIEW_REPORT" and data["decision"] not in DECISIONS:
            raise BlackboardError(f"REVIEW_REPORT.decision must be one of {sorted(DECISIONS)}")
        if kind == "FROZEN_PATCH":
            self._check_freeze(data)
        seq = len(self.items) + 1
        item = {"seq": seq, "kind": kind, "author": author, "at": round(time.time(), 2), **data}
        self.items.append(item)
        if self.dir:
            (self.dir / f"{seq:02d}_{kind}.json").write_text(json.dumps(item, indent=1), encoding="utf-8")
            with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"seq": seq, "kind": kind, "author": author, "at": item["at"],
                                    "patch_sha": data.get("patch_sha")}) + "\n")
        return seq

    def freeze(self, diff: str, author: str = "orchestrator") -> int:
        sha = patch_sha(diff)
        report = self.latest("TEST_REPORT", patch_sha=sha, accepted=True)
        return self.post("FROZEN_PATCH", author, {"patch_sha": sha, "diff": diff,
                                                   "test_report": report["seq"] if report else -1})

    # ---------------------------------------------------------------- read
    def all(self, kind: str | None = None, **match) -> list[dict]:
        return [i for i in self.items if (kind is None or i["kind"] == kind)
                and all(i.get(k) == v for k, v in match.items())]

    def latest(self, kind: str, **match) -> dict | None:
        found = self.all(kind, **match)
        return found[-1] if found else None

    # ---------------------------------------------------------------- rules
    def _published_patches(self) -> set[str]:
        return {i["patch_sha"] for i in self.items if i["kind"] == "CHANGE_PLAN"}

    def _check_freeze(self, data: dict) -> None:
        sha = data["patch_sha"]
        if patch_sha(data["diff"]) != sha:
            raise BlackboardError("FROZEN_PATCH: diff does not match its hash")
        if not self.all("TEST_REPORT", patch_sha=sha, accepted=True):
            raise BlackboardError(f"cannot freeze patch {sha}: no accepted TEST_REPORT for this exact patch")
        if self.all("REVIEW_REPORT", patch_sha=sha, decision="REJECT"):
            raise BlackboardError(f"cannot freeze patch {sha}: the review rejected it")
