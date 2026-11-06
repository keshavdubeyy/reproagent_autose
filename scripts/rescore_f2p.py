"""Add the 'valid by meaning' score to saved reproduction results, without re-running the agent.

For every saved result: strict fail-to-pass PASS -> also valid by meaning; not reproduced -> not valid.
A reproduced test that FAILED the strict check is run again on the fixed version: if its only failures are
error-message wording differences, the MessageJudge (local SLM, temperature 0) decides whether the two
messages mean the same thing (unless the report is about the message itself).

  python scripts/rescore_f2p.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproagent.llm import DEFAULT_MODEL, LLM  # noqa: E402
from reproagent.models import BugReport, ReproTest  # noqa: E402
from reproagent.verification import FailToPassChecker, MessageJudge, TestExecutor  # noqa: E402

BENCHES = {"shopcart": "bugs.json", "bugsinpy": "manifest.json", "multiloc": "bugs.json"}


def main() -> None:
    judge = MessageJudge(LLM(DEFAULT_MODEL))
    for bench, man in BENCHES.items():
        bugs = {b["id"]: b for b in json.loads((ROOT / "benchmarks" / bench / man).read_text(encoding="utf-8"))["bugs"]}
        shims = [str(ROOT / "benchmarks/bugsinpy/compat_shims")] if bench == "bugsinpy" else []
        checker = FailToPassChecker(TestExecutor(timeout_s=90, extra_paths=shims))
        for p in sorted((ROOT / "results" / bench).glob("*.json")):
            o = json.loads(p.read_text(encoding="utf-8"))
            b = bugs.get(o["bug_id"])
            fixed = ROOT / "benchmarks" / bench / "work" / o["bug_id"] / "fixed"
            if o["verdict"] != "reproduced" or not o.get("final_test") or b is None:
                o["fail_to_pass_semantic"], o["message_checks"] = (False if o.get("fail_to_pass") is not None
                                                                   else None), []
            elif o.get("fail_to_pass"):
                o["fail_to_pass_semantic"], o["message_checks"] = True, []
            elif fixed.exists():
                report = BugReport(b["id"], b["title"], b["body"], bench)
                _, meaning, _, checks = checker.check_with_meaning(
                    fixed, ReproTest(len(o["attempts"]), o["final_test"]), o["bug_id"], report, judge)
                o["fail_to_pass_semantic"], o["message_checks"] = meaning, checks
                print(f"[{bench}] {p.stem}: strict=FAIL by-meaning={meaning} "
                      + "; ".join(f"'{c['expected']}' vs '{c['actual']}' same={c['same']} ({c['by']})"
                                  for c in checks))
                _patch_markdown(p.with_suffix(".md"), meaning, checks)
            p.write_text(json.dumps(o, indent=2), encoding="utf-8")


def _patch_markdown(md: Path, meaning: bool, checks: list) -> None:
    if not md.exists() or not checks:
        return
    old = md.read_text(encoding="utf-8")
    if "**Valid by meaning:**" in old:      # replace an earlier note (re-score)
        head, _, rest = old.partition("**Valid by meaning:**")
        rest = rest.split("\n\n", 2)
        md.write_text(head.rstrip("\n") + "\n\n" + (rest[2] if len(rest) > 2 else ""), encoding="utf-8")
    lines = [f"**Valid by meaning:** {'YES' if meaning else 'NO'} - the only failures on the fixed version are "
             "error-message wording differences:\n"]
    lines += [f"- expected `{c['expected']}`, fixed code says `{c['actual']}` -> "
              f"{'same meaning' if c['same'] else 'NOT the same'} ({c['by']}: {c['why']})" for c in checks]
    text = md.read_text(encoding="utf-8").replace("**Fail-to-pass check against fixed version:** FAIL\n",
                                                  "**Fail-to-pass check against fixed version:** FAIL\n\n"
                                                  + "\n".join(lines) + "\n", 1)
    md.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
