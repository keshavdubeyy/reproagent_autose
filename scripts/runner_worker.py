"""One runner-UI job: the agent on a user-written bug report. Started by scripts/runner_ui.py.

    .venv-autose/bin/python scripts/runner_worker.py runs/ui/<job>/job.json

Prints the agent's stage lines as they happen and ends with one `[result] {...}` JSON line.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from autose.agent.llm import LLM  # noqa: E402
from autose.agent.loop import AgentConfig, ReproAgent  # noqa: E402
from autose.agent.models import BugReport  # noqa: E402
from autose.agent.report import save  # noqa: E402

CONFIGS = {
    "A": dict(retrieval="explorer", review="off", loop_control=False),
    "C": dict(retrieval="hybrid", review="off", loop_control=False),
    "D": dict(retrieval="graph", review="off", loop_control=False),
    "E": dict(retrieval="graph", review="rules", loop_control=True),
}


def say(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text())
    out_dir = job_path.parent
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # SystemExit -> executors' finally blocks run
    cfg = CONFIGS[job.get("config", "D")]
    llm = LLM(job.get("model") or os.environ.get("AUTOSE_MODEL", "qwen3:8b"), seed=int(job.get("seed", 7)))
    t0 = time.time()
    result: dict = {"target": job["target"]}
    try:
        if job["target"] == "swebench":
            from autose.evaluation.evaluator import load_harness
            from autose.run import load_reports, run_one
            iid = job["bug_id"]
            report = BugReport(id=iid, title=job["title"], body=job["body"], source="runner-ui")
            harness = load_harness()
            grade = bool(job.get("grade"))
            args = argparse.Namespace(mode="full", budget=int(job.get("budget", 4)), fix=bool(job.get("fix", True)),
                                      fix_mode="fix_full", report="full", timeout=180, check_f2p=grade,
                                      grade=grade, seed=int(job.get("seed", 7)), **cfg)
            say(f"[runner] SWE-bench {iid} in Docker (config {job.get('config')}); the first run pulls the image")
            row = run_one(args, iid, out_dir, llm, load_reports(), harness, report=report)
            result.update(row)
            result["report_file"] = str(out_dir / f"{iid}.full.json")
        else:
            repo = Path(job["repo"]).expanduser()
            fixed = Path(job["fixed"]).expanduser() if job.get("fixed") else None
            if not repo.is_dir():
                raise FileNotFoundError(f"project folder not found: {repo}")
            bug_id = job.get("bug_id") or job["id"]
            report = BugReport(id=bug_id, title=job["title"], body=job["body"], source="runner-ui")
            say(f"[runner] local project {repo} (config {job.get('config')})")
            agent = ReproAgent(llm, AgentConfig(mode="full", budget=int(job.get("budget", 4)),
                                                fix=bool(job.get("fix", True)), fix_mode="fix_full",
                                                board_dir=str(out_dir / "blackboard"), **cfg))
            out = agent.run(report, repo, fixed)
            save(out, out_dir)
            result.update(verdict=out.verdict.value, fail_to_pass=out.fail_to_pass, llm_calls=out.llm_calls,
                          attempts=len(out.attempts),
                          fix_status=out.fix.status if out.fix is not None else None,
                          report_file=str(out_dir / f"{bug_id}.full.json"))
    except SystemExit:
        result["error"] = "stopped by user"
        raise
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["wall_s"] = round(time.time() - t0, 1)
        (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        say("[result] " + json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
