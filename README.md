# ReproAgent

**A local small-language-model agent that turns a written bug report into a failing reproduction test, then fixes the bug and verifies the fix.**

CS6.403 Autonomous Software Engineering (IIIT Hyderabad, Monsoon 2026). Team: Keshav Dubey, Rishabh Singh, Aasritha Kalluri.

> Scope change after the proposal review: the reviewer asked that the harness also **fix** the bug.
> A verified reproduction test now feeds a Fix stage, and that stage counts as successful only when
> the harness has checked the result (see [ADR-0005](docs/adr/0005-add-fix-stage.md)).

## Documentation
| Doc | What it covers |
|---|---|
| [docs/design-overview.md](docs/design-overview.md) | How we planned the agent, where AI is / isn't used, lecture concepts mapped to code |
| [docs/architecture.md](docs/architecture.md) | Layers, end-to-end flow, agent loop, state machine, classes, safety (diagrams) |
| [docs/spec.md](docs/spec.md) | Requirements in EARS form |
| [docs/product-flows.md](docs/product-flows.md) | UI flows |
| [docs/adr/](docs/adr/) | Architecture decision records 0001–0005 |
| [docs/status.md](docs/status.md) | What's done, what's left, challenges |

## Requirements
- macOS/Linux, Python ≥ 3.11, [uv](https://github.com/astral-sh/uv), [Ollama](https://ollama.com) with `qwen3:8b` (~5 GB).
- Benchmarks only: `python3.8` available for the BugsInPy per-bug environments (`benchmarks/setup_bugsinpy.py`).

## Pipeline

```
bug report + repo
      │
      ▼
┌──────────────┐  tools: list_files, search_code, read_file, write_test, run_test
│ 1. Reproduce │──► harness check: test FAILS on buggy code for a valid reason
└──────┬───────┘     (not ImportError / SyntaxError / collection error); retry with feedback
       │ valid repro test
       ▼
┌──────────────┐  tools: read tools + replace_lines / edit_file, run_test (test is read-only)
│ 2. Fix       │──► harness check: repro test PASSES and the existing suite still passes;
└──────┬───────┘     otherwise revert and retry with feedback
       ▼
  report (.md + .json): status, test, pytest evidence, patch, attempts, time, tokens
```

Evaluation oracle: where a known-fixed version exists, the generated test must **pass on the fixed version** (fail-to-pass).

## Quick start

```bash
ollama pull qwen3:8b
uv venv && uv pip install -e .
.venv/bin/python -m pytest                     # harness unit tests (no LLM)
.venv/bin/python -m reproagent run --repo examples/toy_calc \
    --report examples/toy_calc/ISSUE.md --fixed-repo examples/toy_calc_fixed
.venv/bin/python -m reproagent eval --dataset benchmarks/dataset.json --modes full,single_shot,desc_only
```

### Chat UI

```bash
.venv/bin/python -m reproagent.ui        # then open http://127.0.0.1:8765
```
A local, chat-style app (flows in [docs/product-flows.md](docs/product-flows.md)):
1. **Connect** a repository folder. The interpreter is auto-detected and a health check runs. You can also pick a benchmark bug.
2. **Describe the bug**: type it, paste a GitHub issue link, or attach a `.md`/`.txt` report.
3. **Confirm** the agent's structured understanding, or edit it.
4. **Reproduce**: steps stream live. You get a failing test as evidence, then choose **Fix it**, **Save test** or **Stop**.
5. **Fix**: the patch is verified against the reproduction test and the existing suite. Then **Apply to repository** (after a confirm step), **Download patch** or **Discard**. If the fix fails, use **Retry with a hint**.

Sessions persist in `results/sessions/`. The **Evaluation** page shows the benchmark results.

Modes (for the hypotheses):
- `full`: an agent loop with code tools and execution feedback.
- `single_shot`: one generation from the report plus the file list, with no feedback. Baseline for H2.
- `desc_only`: one generation from the report alone. Baseline for H1.

## Repository layout
```
reproagent/            the agent (stdlib + pytest only)
  llm.py               Ollama client, token/time accounting
  agent.py             generic ReAct tool loop with step budget and trace
  workspace.py         sandbox copy, path-confined tools, pytest runner
  pipeline.py          Reproduce + Fix stages, harness checks, oracle
  session.py           chat sessions: intake, state machine, Apply
  cli.py, ui.py        command line and local web server
  static/index.html    chat UI
tests/                 unit tests (no LLM needed)
examples/              toy bug (buggy + fixed) for demos and tests
benchmarks/            BugsInPy dataset, bug reports, setup + summary scripts
results/               pilot evaluation JSON and a demo run
docs/                  design overview, architecture, spec, ADRs, flows, status
report/                mid-submission report (ACM LaTeX)
Makefile               make setup | test | ui | demo | bench | eval | summary
```
