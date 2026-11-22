# ADR 0009: Keep the fix hidden from the agent

Status: accepted (22 Nov 2026)

## Decision
- `benchmarks/*/private/` (gold patches, hidden tests, harness records) is never committed and only
  `autose/evaluation` reads it. The agent gets the public bug report and a code snapshot exported with
  `git archive` (no history).
- Report fields written by the refining model that reveal the answer (`RootCause`, `Title`) are dropped.
- SWE-bench images contain the full git history. `DockerExecutor` detaches HEAD and deletes every other ref
  at container start, so `git log --all` shows nothing after the buggy commit. The agent has no shell
  tool, only pytest on files it writes.

## Consequences
- The remaining leak path is pre-training contamination of the model on these public repositories. It is
  reported as a threat to validity.
