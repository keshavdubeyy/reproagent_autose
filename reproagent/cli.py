"""Command-line interface.

  python -m reproagent run --repo examples/toy_calc --report examples/toy_calc/ISSUE.md
  python -m reproagent eval --dataset benchmarks/dataset.json --modes full,single_shot,desc_only
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .llm import DEFAULT_MODEL, LLM
from .pipeline import ReproAgent, to_dict


def render_md(d: dict) -> str:
    return f"""# ReproAgent report: {d['bug_id']} ({d['mode']})

| field | value |
|---|---|
| reproduction | **{d['status']}** (valid: {d['repro_valid']}, attempts: {d['repro_attempts']}) |
| fail-to-pass oracle | {d['fail_to_pass']} |
| fix | **{d['fix_status']}** (attempts: {d['fix_attempts']}, regression-free: {d['regression_free']}, passes developer test: {d['gt_pass']}) |
| time | {d['seconds']} s, {d['llm'].get('calls')} LLM calls |

## Reproduction test
```python
{d['test_code']}
```

## Evidence (pytest on buggy code)
```
{d['evidence']}
```

## Proposed patch
```diff
{d['patch'] or '(none)'}
```

## Agent explanation
{d['explanation']}
"""


def cmd_run(a):
    agent = ReproAgent(LLM(a.model), a.mode, a.repro_budget, a.fix_budget, a.max_steps, not a.no_fix)
    d = to_dict(agent.run(a.repo, Path(a.report).read_text(), Path(a.repo).name, a.fixed_repo))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / f"{d['bug_id']}_{a.mode}.json").write_text(json.dumps(d, indent=2))
    (out / f"{d['bug_id']}_{a.mode}.md").write_text(render_md(d))
    print(f"[{d['bug_id']}] repro={d['status']} f2p={d['fail_to_pass']} fix={d['fix_status']} ({d['seconds']}s)")
    print(f"report: {out / (d['bug_id'] + '_' + a.mode + '.md')}")


def cmd_eval(a):
    """dataset.json: [{"id", "repo", "report", "fixed_repo"?}] with paths relative to the dataset file."""
    ds_path = Path(a.dataset)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    base = ds_path.parent
    rows = []
    bugs = json.loads(ds_path.read_text())
    if a.only:
        bugs = [b for b in bugs if b["id"] in a.only.split(",")]
    for mode in a.modes.split(","):
        for bug in bugs:
            agent = ReproAgent(LLM(a.model), mode, a.repro_budget, a.fix_budget, a.max_steps, not a.no_fix)
            fixed = base / bug["fixed_repo"] if bug.get("fixed_repo") else None
            py = str(base / bug["python"]) if bug.get("python") else None
            d = to_dict(agent.run(base / bug["repo"], (base / bug["report"]).read_text(), bug["id"], fixed,
                                  py, bug.get("gt_test")))
            rows.append(d)
            print(f"{mode:12s} {bug['id']:12s} repro={d['status']:15s} f2p={d['fail_to_pass']} "
                  f"fix={d['fix_status']} gt={d['gt_pass']} {d['seconds']}s", flush=True)
            (out / f"{bug['id']}_{mode}.json").write_text(json.dumps(d, indent=2))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"eval_{stamp}.json").write_text(json.dumps(rows, indent=2))
    print(f"saved {out / f'eval_{stamp}.json'}")


def main():
    p = argparse.ArgumentParser(prog="reproagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "eval"):
        s = sub.add_parser(name)
        s.add_argument("--model", default=DEFAULT_MODEL)
        s.add_argument("--repro-budget", type=int, default=3)
        s.add_argument("--fix-budget", type=int, default=3)
        s.add_argument("--max-steps", type=int, default=15)
        s.add_argument("--no-fix", action="store_true")
        s.add_argument("--out", default="results")
    r = sub.choices["run"]
    r.add_argument("--repo", required=True)
    r.add_argument("--report", required=True)
    r.add_argument("--fixed-repo")
    r.add_argument("--mode", default="full", choices=["full", "desc_only", "single_shot"])
    e = sub.choices["eval"]
    e.add_argument("--dataset", required=True)
    e.add_argument("--modes", default="full,single_shot,desc_only")
    e.add_argument("--only", help="comma-separated bug ids")
    a = p.parse_args()
    {"run": cmd_run, "eval": cmd_eval}[a.cmd](a)


if __name__ == "__main__":
    main()
