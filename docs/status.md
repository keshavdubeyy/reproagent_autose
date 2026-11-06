# Project Status (as of 6 Nov 2026)

## Done
- **Pipeline:** Reproduce → Fix, both as ReAct loops on local `qwen3:8b`, with retries and feedback.
- **Harness checks:** valid-failure classifier (pytest exit 1 only), baseline-vs-after suite diff, read-only tests during Fix, revert between attempts, fail-to-pass oracle, developer-test check.
- **Sandbox:** temp-copy workspace, path-confined tools, scrubbed env, timeouts.
- **Baselines:** `single_shot` and `desc_only` modes.
- **Benchmark:** toy bug + 8 BugsInPy bugs (tqdm, thefuck, httpie), each with its own Python 3.8 environment and a hand-written report.
- **Interfaces:** CLI (`run`, `eval`) and a chat UI (connect repo → report/GitHub issue → confirm → reproduce → fix → apply), verified end to end.
- **Docs:** spec (EARS), 5 ADRs, architecture, design overview, product flows, AGENTS.md. 12 unit tests (no LLM needed).

## Pilot results (13 of 27 runs)

| mode | runs | harness-valid repro | true repro (fail-to-pass) | fixed (harness) | fix passes developer test | mean time |
|---|---|---|---|---|---|---|
| full | 9 | 7 | 3 | 2 | 1 | 383 s |
| single_shot | 4 | 2 | 2 | 1 | 0 | 369 s |

Key observation: 4 of 7 "valid" reproductions from the full agent **failed the oracle** — the test failed on the buggy code but also on the fixed code, so it was not testing the bug. The harness alone can't tell; the oracle can.

## Left
1. Finish the evaluation (14 runs left, incl. all `desc_only`); ideally 3 seeds per run for variance.
2. Failure analysis: categorise every failed run (wrong location, assert-the-bug, env issue, budget exhausted, needs_info).
3. Reduce false reproductions — e.g. require the test to reference the reported symptom, or a self-check step against the "expected" behaviour.
4. Optional: MCP server exposing our tools; one `SKILL.md` with a with/without ablation.
5. More bugs (target 15–20) for the final report.
6. Final 8-page ACM report, demo video, viva.

## Major challenges
1. **False reproductions.** The small model often writes a test that fails for the wrong reason. This is our biggest quality problem.
2. **Speed.** Runs take 1.5–14 min on a laptop; a full eval takes hours. This limits how many bugs and seeds we can run.
3. **Old-project environments.** BugsInPy bugs need Python 3.8 and old pinned deps that often don't build on Apple Silicon; we built one venv per bug with fallbacks.
4. **Small-model tool use.** It gives up early (`needs_info`), repeats tool calls, or makes ambiguous edits; we added harness guards (soft needs_info, line-disambiguated edits).
5. **Fix overfitting.** A patch can pass our repro test but fail the developer's test (tqdm-3 single_shot).
