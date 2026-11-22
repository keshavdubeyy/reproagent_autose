# ADR 0012: Hand the search results to the Fault Localiser; run the ablation with a per-bug night runner

Status: accepted (22 Nov 2026)

## Context
The pipeline-health report (`autose/evaluation/pipeline.py`) showed a broken hand-off. Bug Understanding's
search found the function the developers fixed more often than the Fault Localiser picked it. The cause
was simple: the localiser never saw the search results. It used only the traceback, the reproduction
test's calls and the names in the report.

The A–E ablation also needs about 160 Docker runs on one laptop with about 25 GB of free disk.

## Decision

### Search → localiser hand-off
- `IndexedExplorer.search` keeps the top 10 ranked definitions (`CodeContext.ranked`). The prompt still
  shows 4 snippets.
- The agent passes this list to `FaultLocalizer.localize(retrieved=...)`.
- Each retrieved definition adds `max(6 − 0.5·rank, 1.5)` to its score. A retrieved class credits its
  `__init__`, since classes are not patch targets.
- The traceback (10 or more) and the reproduction test's asserted call (8) stay the strongest signals.
- Configuration A (mid-term keyword search) does not hand anything over, so A remains the mid-term
  baseline.

### Evidence
- **How it was measured:** `autose/evaluation/localizer_handoff.py`, an evaluator-side check. The
  developers' own new tests stand in for the reproduction test.
- **Result on the 40-bug subset:** the fixed function is among the localiser's candidates for 23/40
  bugs with the hand-off and 14/40 without. No bug is lost.
- **Tuning:** the weight was chosen on django only. The weight barely matters; handing 10 results
  instead of 4 adds one bug.
- **Bug found by the smoke test:** in flask-4045 the search ranked `Blueprint` first, but the class
  was dropped. That led to the `__init__` rule.

### Night runner
- **Command:** `python -m autose.evaluation.night --configs A C --tag night1`.
- **Order:** all requested configurations run back to back on each bug, so every image is pulled once.
- **Images:** each image is deleted after its bug, unless it was on the machine before the run.
- **Clean stop:** containers named `autose-agent-*` / `autose-eval-*` are removed at start, after each
  run and on SIGINT/SIGTERM.
- **Resume and disk guard:** the run is resumable, and it stops when free disk falls below 6 GB.
- **Paper table:** `autose/evaluation/tables.py` turns the summaries into the paper's ablation table.
  Only bugs present in every run are compared, with wins and losses counted against the first
  configuration.

## Consequences
- The localiser now shows more candidates from the search, which can push out a callee the slice
  found. This was measured (no losses on the subset) but is not guaranteed.
- One bug runs at a time; four configurations on 40 bugs take two nights.
- Seeds and reruns use the same command with another `--seed` / `--tag`.
