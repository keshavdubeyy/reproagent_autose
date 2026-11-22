# ADR 0006: SWE-bench Lite is the main benchmark, run in its official Docker images

Status: accepted (22 Nov 2026)

## Context
The mid-term used 20 small bugs (ShopCart + BugsInPy) on the host Python. That is too few and too easy to
support claims, and BugsInPy needs old Python versions that are hard to install on a Mac. The draft
architecture named BugsInPy; the `final-sub` work had already built and validated SWE-bench Lite.

## Decision
- SWE-bench Lite is the main benchmark: 295 of 300 bugs validated (fail without the fix, pass with it).
- Every test the agent runs executes inside the bug's official SWE-bench image (`DockerExecutor`): exact
  environment, nothing installed on the host. Grading uses the official `swebench` grading code.
- ShopCart + BugsInPy (mid-term) stay as a small, fast second dataset run with `LocalExecutor`.

## Consequences
- Results are comparable with published SWE-bench numbers.
- Images are x86 only: on Apple Silicon they run under emulation (`platform=linux/amd64`), ~3-6 min per bug.
- One image is ~4 GB, so runs are bug-major and delete images after use (ADR 0010).
