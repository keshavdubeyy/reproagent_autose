"""Run the agent: on a local project, or on SWE-bench Lite bugs inside their Docker images.

Local project (mid-term style, no Docker):
  python -m autose.run local --repo benchmarks/shopcart/work/shop_discount/buggy \
      --issue benchmarks/shopcart/issue_shop_discount.json --fix

SWE-bench Lite (needs Docker + `python scripts/build_benchmark.py code`):
  python -m autose.run swebench --ids pallets__flask-4045 --fix
  python -m autose.run swebench --subset benchmarks/swe_bench_lite/subset.json --mode no_context
  ...add --grade to score with the hidden tests (evaluator only; reads benchmarks/*/private).

Every bug writes runs/<run-name>/<instance_id>.<mode>.{json,md}; a run is resumable (finished bugs
are skipped) and appends one line per bug to runs/<run-name>/summary.jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from autose.agent.issue import IssueRetrieval
from autose.agent.llm import DEFAULT_MODEL, LLM
from autose.agent.loop import FIX_MODES, MODES, AgentConfig, ReproAgent
from autose.agent.models import BugReport
from autose.agent.report import save, to_markdown

PUBLIC = Path("benchmarks/swe_bench_lite/public")


# ------------------------------------------------------------------ SWE-bench bug reports (public)
def load_reports(variant: str = "full") -> dict[str, dict]:
    rows = json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))
    return {r["instance_id"]: r for r in rows}


def to_report(row: dict, variant: str = "full") -> BugReport:
    """`raw` = only the GitHub issue text; `full` = issue text + Steps / Expected / Observed sections."""
    br = row["bug_report"]
    desc = (br.get("Description") or "").replace("\r\n", "\n").strip()
    title, _, rest = desc.partition("\n")
    body = rest.strip()
    if variant == "full":
        for key, head in (("StepsToReproduce", "Steps to reproduce"), ("ExpectedBehavior", "Expected behavior"),
                          ("ObservedBehavior", "Observed behavior")):
            v = br.get(key)
            if v:
                v = "\n".join(f"- {x}" for x in v) if isinstance(v, list) else str(v)
                body += f"\n\n## {head}\n{v.strip()}"
    return BugReport(id=row["instance_id"], title=title.strip(), body=body, source=f"swe-bench-lite:{variant}")


def run_swebench(a) -> int:
    from autose.agent.workspace import DockerExecutor, swebench_image

    reports = load_reports()
    ids = list(a.ids or [])
    if a.subset:
        ids += json.loads(Path(a.subset).read_text(encoding="utf-8"))
    if not ids:
        sys.exit("give --ids or --subset")
    out_dir = Path("runs") / (a.run_name or f"{a.mode}_{a.retrieval}{'+' + a.fix_mode if a.fix else ''}_{a.report}")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = out_dir / "summary.jsonl"
    done = {json.loads(l)["instance_id"] for l in summary.read_text().splitlines() if l.strip()} \
        if summary.exists() else set()
    (out_dir / "config.json").write_text(json.dumps(vars(a), indent=2, default=str), encoding="utf-8")

    llm = LLM(a.model, seed=a.seed)
    harness = None
    if a.grade or a.check_f2p:
        from autose.evaluation.evaluator import load_harness   # evaluator-only data
        harness = load_harness()

    for i, iid in enumerate(ids, 1):
        if iid in done:
            continue
        repo = PUBLIC / "code" / iid
        if not repo.exists():
            print(f"[{i}/{len(ids)}] {iid}: no code snapshot, run `python scripts/build_benchmark.py code --only {iid}`")
            continue
        image = harness[iid]["image"] if harness and "image" in harness.get(iid, {}) else swebench_image(iid)
        report = to_report(reports[iid], a.report)
        print(f"\n[{i}/{len(ids)}] {iid}  ({image})", flush=True)
        t0 = time.time()
        ex = fixed_ex = None
        row = {"instance_id": iid, "mode": a.mode, "retrieval": a.retrieval, "fix_mode": a.fix_mode if a.fix else None}
        try:
            ex = DockerExecutor(image, timeout_s=a.timeout)
            if a.check_f2p:
                fixed_ex = DockerExecutor(image, timeout_s=a.timeout, base_patch=harness[iid]["patch"])
            row_pub = reports[iid]
            agent = ReproAgent(llm, AgentConfig(mode=a.mode, budget=a.budget, fix=a.fix, fix_mode=a.fix_mode,
                                                retrieval=a.retrieval, repo_id=row_pub["repo"],
                                                commit=row_pub["base_commit"],
                                                board_dir=str(out_dir / "blackboard")), executor=ex)
            out = agent.run(report, repo, fixed_executor=fixed_ex)
            save(out, out_dir)
            row.update(verdict=out.verdict.value, fail_to_pass=out.fail_to_pass, llm_calls=out.llm_calls,
                       attempts=len(out.attempts), time_s=out.total_time_s)
            if out.fix is not None:
                row.update(fix_status=out.fix.status, fix_llm_calls=out.fix.llm_calls)
                if a.grade and out.fix.final_diff:
                    import docker
                    from autose.evaluation.evaluator import evaluate
                    res = evaluate(harness[iid], out.fix.final_diff, a.mode, out_dir / "eval" / iid,
                                   docker.from_env(timeout=1800))
                    row.update(resolved=res.resolved, f2p=f"{res.f2p_passed}/{res.f2p_total}",
                               p2p=f"{res.p2p_passed}/{res.p2p_total}", eval_error=res.error)
        except Exception as exc:   # record and continue the batch
            row.update(error=f"{type(exc).__name__}: {exc}")
        finally:
            for e in (ex, fixed_ex):
                if e is not None:
                    e.close()
        row["wall_s"] = round(time.time() - t0, 1)
        with summary.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print("  ->", json.dumps(row), flush=True)
    return 0


def run_local(a) -> int:
    report = IssueRetrieval().load(a.issue)
    agent = ReproAgent(LLM(a.model, seed=a.seed),
                       AgentConfig(mode=a.mode, budget=a.budget, fix=a.fix, fix_mode=a.fix_mode,
                                   retrieval=a.retrieval, board_dir=str(Path(a.out) / "blackboard")))
    outcome = agent.run(report, a.repo, a.fixed)
    path = save(outcome, a.out)
    print("\n" + to_markdown(outcome))
    print(f"Report saved to {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="autose", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mode", default="full", choices=list(MODES))
    common.add_argument("--budget", type=int, default=4)
    common.add_argument("--fix", action="store_true", help="after reproducing, generate and validate a patch")
    common.add_argument("--fix-mode", default="fix_full", choices=list(FIX_MODES))
    common.add_argument("--model", default=DEFAULT_MODEL)
    common.add_argument("--seed", type=int, default=7)
    common.add_argument("--retrieval", default="explorer", choices=["explorer", "bm25", "hybrid", "graph"],
                        help="how Bug Understanding finds code: mid-term keyword search or the knowledge package")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lo = sub.add_parser("local", parents=[common], help="a project on this machine")
    lo.add_argument("--repo", required=True, type=Path)
    lo.add_argument("--issue", required=True, help="JSON / Markdown file or public GitHub issue URL")
    lo.add_argument("--fixed", type=Path, help="fixed version (evaluation only)")
    lo.add_argument("--out", type=Path, default=Path("runs/local"))

    sw = sub.add_parser("swebench", parents=[common], help="SWE-bench Lite bugs in Docker")
    sw.add_argument("--ids", nargs="*")
    sw.add_argument("--subset", help="JSON list of instance ids")
    sw.add_argument("--report", default="full", choices=["full", "raw"],
                    help="raw = GitHub issue text only; full = + Steps/Expected/Observed")
    sw.add_argument("--timeout", type=int, default=180, help="seconds per pytest run")
    sw.add_argument("--run-name")
    sw.add_argument("--check-f2p", action="store_true", help="evaluation: re-run the final test on the gold fix")
    sw.add_argument("--grade", action="store_true", help="evaluation: grade the final patch with hidden tests")
    a = ap.parse_args()
    return run_swebench(a) if a.cmd == "swebench" else run_local(a)


if __name__ == "__main__":
    sys.exit(main())
