# ReproAgent

**A local small-language-model agent that turns a written bug report into a failing reproduction test, then fixes the bug and verifies the fix.**

Course project for **CS6.403 Autonomous Software Engineering** (IIIT Hyderabad, Monsoon 2026, Prof. Karthik Vaidhyanathan).

| Team member | Role |
|---|---|
| Keshav Dubey | |
| Rishabh Singh | |
| Aasritha Kalluri | |

> **Status:** code is being added in reviewed pull requests by the team. See [Project status](#project-status).

---

## For evaluators: 5-minute path

1. **Read** [`docs/design-overview.md`](docs/design-overview.md). It explains how we planned the agent, where AI is and isn't used, and which course concept each part implements.
2. **See the architecture** in [`docs/architecture.md`](docs/architecture.md): the layers, the end-to-end flow, the agent loop and the state machine.
3. **Run the demo** on a toy bug, without the benchmark:
   ```bash
   make setup && make test && make demo
   ```
4. **Open the chat UI** with `make ui`, then go to http://127.0.0.1:8765.
5. **Check the results**: run `make summary` to print the evaluation tables from `results/`.

---

## What it does

```
bug report + repo
      │
      ▼
 🤖 Understand   report → structured bug (title, observed, expected, steps, missing)
      │  🧑 user confirms
      ▼
 🤖 Reproduce    explore code, write a pytest test, run it
      │  ⚙️ harness: test must FAIL on buggy code for a real reason
      ▼
 🤖 Fix          edit source code (tests are read-only)
      │  ⚙️ harness: repro test PASSES and no previously passing test breaks
      ▼
 🧑 Apply        write the test and patch into the repo (only after approval)
```

🤖 = local LLM (`qwen3:8b` via Ollama) · ⚙️ = deterministic code · 🧑 = human

**Key design choice:** *agent = model + harness*. The model never judges its own success; pytest and the harness do. Nothing leaves your machine.

## Requirements

| Need | Version | Why |
|---|---|---|
| macOS or Linux | | |
| Python | ≥ 3.11 | runs the agent |
| [uv](https://github.com/astral-sh/uv) | any | creates the virtual environment |
| [Ollama](https://ollama.com) | any, running | local model server |
| `qwen3:8b` | ~5 GB | `ollama pull qwen3:8b` |
| Python 3.8 | benchmarks only | the BugsInPy projects need old Python |

The demo runs comfortably on a 16 GB Apple Silicon laptop. One bug takes about 1.5 to 14 minutes.

## Quick start

```bash
git clone https://github.com/keshavdubeyy/reproagent_autose.git
cd reproagent_autose
ollama pull qwen3:8b
make setup        # venv + install
make test         # unit tests, no LLM needed
make demo         # reproduce + fix the toy median bug
make ui           # chat UI at http://127.0.0.1:8765
```

Reproduce the evaluation:
```bash
make bench        # build BugsInPy repos + per-bug Python 3.8 envs (~175 MB, git-ignored)
make eval         # all bugs × modes: full, single_shot, desc_only
make summary      # tables used in the report
```

## Repository layout

```
reproagent/      the agent: llm, agent loop, workspace sandbox, pipeline, sessions, CLI, UI
tests/           unit tests (no LLM needed)
examples/        toy bug, buggy + fixed versions
benchmarks/      BugsInPy dataset, bug reports, setup + summary scripts
results/         evaluation outputs
docs/            design overview, architecture, spec (EARS), ADRs, product flows, status
report/          course reports (ACM LaTeX)
```

## Documentation

| Doc | What it covers |
|---|---|
| [design-overview.md](docs/design-overview.md) | How we planned the agent, AI vs. non-AI parts, course concepts mapped to code |
| [architecture.md](docs/architecture.md) | Diagrams: components, flow, agent loop, states, classes, safety |
| [spec.md](docs/spec.md) | Requirements in EARS form |
| [adr/](docs/adr/) | Architecture decision records |
| [product-flows.md](docs/product-flows.md) | Chat UI flows |
| [status.md](docs/status.md) | What's done, what's left, challenges |

## Evaluation

- **Benchmark:** a toy bug plus 8 BugsInPy bugs (tqdm, thefuck, httpie), each with a hand-written report.
- **Modes:**
  - `full`: the agent loop with tools and retries.
  - `single_shot`: one generation from the report plus the file list.
  - `desc_only`: one generation from the report alone.
- **Metrics:**
  - Harness-valid reproduction.
  - Fail-to-pass: the test fails on the buggy version and passes on the fixed version.
  - Fix accepted by the harness.
  - Fix passes the developers' own test.
  - LLM calls, tokens and wall time.

## Project status

- [x] Proposal accepted. The reviewer asked us to also fix the bug.
- [x] Mid-submission progress report
- [ ] Code and docs merged into `main` through PRs
- [ ] Full evaluation (27 runs, 3 seeds)
- [ ] Final report, demo and viva

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: one branch per change, a pull request with one reviewer, and `make test` must pass.

## License

[MIT](LICENSE)
