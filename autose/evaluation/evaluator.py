"""Run one SWE-bench Lite bug's hidden tests against a patch, inside Docker.

Design choices:
- Environment: the official prebuilt SWE-bench image per bug (repo at base_commit + exact
  Python/conda env at /testbed). Rebuilding 300 old environments ourselves would be slow
  and a source of grading errors.
- Grading: the official swebench test specs, log parsers and get_eval_report, so our
  "resolved" means exactly what published SWE-bench numbers mean.
- Our own runner instead of swebench.run_instance: on Windows that writes eval.sh with
  CRLF line endings, which bash in the container cannot run. We send files as bytes.
"""
from __future__ import annotations

import io
import json
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import docker
from swebench.harness.docker_utils import exec_run_with_timeout
from swebench.harness.grading import get_eval_report
from swebench.harness.utils import make_test_spec

HARNESS_FILE = Path("benchmarks/swe_bench_lite/private/swebench_harness.json")
WORKDIR = "/testbed"
# SWE-bench images are published for x86 only; on Apple Silicon Docker runs them under emulation.
PLATFORM = "linux/amd64"
PATCH_FILE = "/tmp/patch.diff"
# Same fallback order as the official harness.
APPLY_CMDS = [
    "git apply --verbose",
    "git apply --verbose --3way",
    "git apply --verbose --reject",
    "patch --batch --forward --fuzz=5 -p1 -i",
]


def load_harness(path: Path = HARNESS_FILE) -> dict[str, dict]:
    """Official SWE-bench Lite records (with eval_script, image, log_parser), keyed by instance_id."""
    return {r["instance_id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}


def put_file(container, path: str, data: bytes) -> None:
    """Copy bytes into the container unchanged (no newline conversion)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(Path(path).name)
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
    container.put_archive(str(Path(path).parent.as_posix()), buf.getvalue())


@dataclass
class EvalResult:
    instance_id: str
    label: str                 # e.g. "gold", "no_patch", or a run name
    resolved: bool
    patch_applied: bool | None  # None when no patch was given
    timed_out: bool
    runtime_s: float
    f2p_passed: int
    f2p_total: int
    p2p_passed: int
    p2p_total: int
    error: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _apply_patch(container, patch: str) -> tuple[bool, str]:
    put_file(container, PATCH_FILE, patch.encode("utf-8"))
    out = b""
    for i, cmd in enumerate(APPLY_CMDS):
        if i:  # a failed attempt can leave partial state; start clean
            container.exec_run(["bash", "-c", "git checkout -- . ; git clean -fd"], workdir=WORKDIR)
        res = container.exec_run(f"{cmd} {PATCH_FILE}", workdir=WORKDIR)
        out = res.output
        if res.exit_code == 0:
            return True, out.decode("utf-8", "replace")
    rev = container.exec_run(f"git apply --check --reverse {PATCH_FILE}", workdir=WORKDIR)
    return rev.exit_code == 0, out.decode("utf-8", "replace")


def evaluate(record: dict, patch: str | None, label: str, out_dir: Path,
             client: docker.DockerClient, timeout: int = 1800) -> EvalResult:
    """Apply `patch` (None = leave the buggy code as is), run the hidden tests, grade.

    Writes out_dir/{eval.sh,apply.log,test_output.txt,report.json}.
    """
    iid = record["instance_id"]
    spec = make_test_spec(record)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"autose-eval-{iid.lower()}-{label}"
    start = time.time()
    container = None
    applied = None
    try:
        try:
            client.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass
        container = client.containers.create(spec.image, command="tail -f /dev/null", name=name, platform=PLATFORM,
                                             working_dir=WORKDIR, user="root", detach=True)
        container.start()

        if patch is not None:
            applied, apply_log = _apply_patch(container, patch)
            (out_dir / "apply.log").write_text(apply_log, encoding="utf-8")
            if not applied:
                return EvalResult(iid, label, False, False, False, time.time() - start,
                                  0, len(spec.FAIL_TO_PASS), 0, len(spec.PASS_TO_PASS), "patch did not apply")

        script = spec.eval_script.encode("utf-8")
        (out_dir / "eval.sh").write_bytes(script)
        put_file(container, "/eval.sh", script)
        output, timed_out, _ = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout)
        log_path = out_dir / "test_output.txt"
        log_path.write_bytes(output.encode("utf-8") if isinstance(output, str) else output)

        report = get_eval_report(test_spec=spec,
                                 prediction={"instance_id": iid, "model_name_or_path": label,
                                             "model_patch": patch or ""},
                                 test_log_path=str(log_path), include_tests_status=True)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        r = report[iid]
        st = r.get("tests_status", {})
        f2p, p2p = st.get("FAIL_TO_PASS", {}), st.get("PASS_TO_PASS", {})
        err = None
        if timed_out:
            err = f"timed out after {timeout}s"
        elif not r.get("patch_successfully_applied"):
            err = "no parseable test output" + (f" ({r['infra_failure_reason']})" if r.get("infra_failure_reason") else "")
        return EvalResult(iid, label, bool(r["resolved"]), applied, timed_out, round(time.time() - start, 1),
                          len(f2p.get("success", [])), len(spec.FAIL_TO_PASS),
                          len(p2p.get("success", [])), len(spec.PASS_TO_PASS), err)
    except Exception as exc:  # docker or grading failure: record it, don't stop the batch
        return EvalResult(iid, label, False, applied, False, round(time.time() - start, 1),
                          0, len(spec.FAIL_TO_PASS), 0, len(spec.PASS_TO_PASS), f"crash: {type(exc).__name__}: {exc}")
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
