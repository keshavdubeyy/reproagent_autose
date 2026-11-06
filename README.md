# ReproAgent: a local SLM agent that reproduces and fixes software bugs

AutoSE course project: Keshav Dubey, Rishabh Singh, Aasritha Kalluri.

ReproAgent reads a natural-language bug report and a Python project. It writes a pytest test, runs it in a sandbox, refines it from execution feedback, and reports one verdict with evidence: **reproduced**, **not reproduced** or **needs more information**.
With `--fix`, it then localises the fault from the failing test, asks the SLM for a patch, and validates the patch in a sandbox: the reproduction test must pass and related existing tests must not regress. Your project is never modified: the patch is reported as a diff.
Everything runs locally. The SLM is `qwen2.5-coder:3b` served by Ollama. No hosted AI service is used.

* Architecture and design rationale: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
* Mid-term progress report (results, failures, remaining work): [docs/PROGRESS_REPORT.md](docs/PROGRESS_REPORT.md)

## Setup (Windows / Linux / macOS, 8 GB RAM is enough)

```bash
python -m venv .venv
```
```bash
.venv/Scripts/pip install -r requirements.txt
```
```bash
ollama pull qwen2.5-coder:3b
```

## Quick demo: the "discount below ₹1,000" bug from the course slides

```bash
python benchmarks/shopcart/build.py
```
```bash
python -m reproagent --repo benchmarks/shopcart/work/shop_discount/buggy --fixed benchmarks/shopcart/work/shop_discount/fixed --issue benchmarks/shopcart/issue_shop_discount.json
```

Reproduce **and fix**:

```bash
python -m reproagent --repo benchmarks/shopcart/work/shop_discount/buggy --issue benchmarks/shopcart/issue_shop_discount.json --fix
```

`--issue` also accepts a Markdown file or a public GitHub issue URL. `--fixed` is optional: when given, the final test is also checked fail-to-pass against the fixed version.
Other options: `--mode full|full_v2|no_context|single_shot|baseline`, `--fix-mode fix_full|fix_single|fix_no_repro|fix_multi` (`fix_multi` = multi-location repair: keeps partial fixes, re-localises, sweeps for the same mistake), `--budget 4`, `--model qwen3:8b`.
The Markdown and JSON evidence report is written to `results/single/`.

## Web UI

```
python -m reproagent.webui          # then open http://127.0.0.1:8000
```

No extra packages are needed (standard library only). Two tabs:
* **Our tested results**: pick any evaluated bug, a reproduction mode and a fix mode to see the saved test, pytest evidence, patch diff and hidden-oracle verdict instantly. No model needed, handy for the demo.
* **Run on a new bug**: paste a bug report (or a GitHub issue URL), give the project folder, tick "Also fix the bug", and watch the agent's log live. Benchmark bugs can be loaded as examples. Results are also saved in `results/webui/`.

## Benchmarks and evaluation

```bash
python scripts/fetch_bugsinpy.py
```
```bash
python scripts/run_eval.py --bench shopcart
```
```bash
python scripts/run_eval.py --bench bugsinpy
```
```bash
python scripts/run_eval.py --summary
```

Add the second score, valid by meaning (error-message wording differences judged by meaning), to saved results without re-running the agent:

```bash
python scripts/rescore_f2p.py
```

Fixing, run on every bug the agent reproduced. It is scored with hidden oracles in `benchmarks/*/oracle_checks.py` and writes `results/FIX_SUMMARY.md`:

```bash
python scripts/run_fix_eval.py --modes fix_full fix_single fix_no_repro fix_multi
```

Multi-location bugs (build, reproduce, then fix):

```bash
python benchmarks/multiloc/build.py
```
```bash
python scripts/run_eval.py --bench multiloc --modes full
```
```bash
python scripts/run_fix_eval.py --bench multiloc --modes fix_full fix_multi
```

* `benchmarks/shopcart/`: in-house e-commerce app with 8 seeded bugs + 1 deliberately vague report.
* `benchmarks/multiloc/`: 5 seeded ShopCart bugs that each need changes in two places (checked by `build.py`).
* `benchmarks/bugsinpy/`: 13 real bugs from [BugsInPy](https://github.com/soarsmu/BugsInPy) (youtube-dl, thefuck, tqdm) with symptom-only reports. `scripts/validate_benchmark.py` checks each one manifests on this machine.

## Layout

```
reproagent/
  models.py            shared models between bounded contexts
  llm.py               Ollama client (local SLM)
  issue_management.py  IssueRetrieval (tool) + BugSpecifier (SLM)
  reproduction.py      RepoExplorer (tool) + TestGenerator (SLM)              [Bug Resolution: reproduce]
  resolution.py        FaultLocalizer (tool) + PatchGenerator (SLM) + PatchApplier (tool)  [Bug Resolution: fix]
  verification.py      Sandbox/TestExecutor (tool) + ResultAnalyzer (rules+SLM) + PatchValidator
                       + FailToPassChecker / hidden-oracle runner (evaluation only)
  agent.py             orchestrator: reproduce -> fix workflow, feedback loops, ablation modes
  report.py            Markdown/JSON evidence report
  webui.py             local web UI (saved results + live runs), standard library only
scripts/               benchmark fetching/validation, evaluation runner
tests/                 unit tests of the deterministic components (no SLM needed)
```
