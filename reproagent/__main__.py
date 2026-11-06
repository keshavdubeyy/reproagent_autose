"""CLI:  python -m reproagent --repo PATH --issue ISSUE.json|ISSUE.md|GITHUB_ISSUE_URL
                            [--fix [--fix-mode fix_full|fix_single|fix_no_repro]]
                            [--fixed PATH] [--mode full|no_context|single_shot|baseline]
                            [--budget 4] [--model qwen2.5-coder:3b] [--out results/single]"""
from __future__ import annotations

import argparse
from pathlib import Path

from .agent import FIX_MODES, MODES, AgentConfig, ReproAgent
from .issue_management import IssueRetrieval
from .llm import DEFAULT_MODEL, LLM
from .report import save, to_markdown


def main() -> None:
    ap = argparse.ArgumentParser(prog="reproagent",
                                 description="Reproduce a bug report as a failing test, then optionally fix it.")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--issue", required=True)
    ap.add_argument("--fixed", type=Path)
    ap.add_argument("--mode", default="full", choices=list(MODES))
    ap.add_argument("--budget", type=int, default=4)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--fix", action="store_true", help="after reproducing, generate and validate a patch")
    ap.add_argument("--fix-mode", default="fix_full", choices=list(FIX_MODES))
    ap.add_argument("--out", type=Path, default=Path("results/single"))
    a = ap.parse_args()

    report = IssueRetrieval().load(a.issue)
    agent = ReproAgent(LLM(a.model), AgentConfig(mode=a.mode, budget=a.budget, use_judge=not a.no_judge,
                                                         fix=a.fix, fix_mode=a.fix_mode))
    outcome = agent.run(report, a.repo, a.fixed)
    path = save(outcome, a.out)
    print("\n" + to_markdown(outcome))
    print(f"Report saved to {path}")
    if outcome.fix is not None and outcome.fix.status == "fixed":
        print("A validated patch is in the report. The project itself was NOT modified.")


if __name__ == "__main__":
    main()
