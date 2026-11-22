# Autose: ReproAgent on SWE-bench Lite

AutoSE course project (Keshav Dubey, Rishabh Singh, Aasritha Kalluri).

A bug-fixing agent that runs on a **local small language model** (Ollama, no hosted AI). Given a bug report and a Python repository it:

1. turns the report into a spec (expected vs observed behaviour)
2. writes a pytest test that **reproduces** the bug, runs it, and refines it from the execution feedback
3. uses that failing test to **localise and fix** the bug, and validates the patch (the reproduction test now passes, and nearby existing tests don't regress)

It is evaluated on **SWE-bench Lite** (295 of the 300 bugs validated) with the official SWE-bench grading, and on the mid-term ShopCart and BugsInPy bugs.

## Layout

```
autose/
  agent/                 the agent (ported from the mid-term ReproAgent v2)
    workspace.py         NEW: Sandbox + Executor. LocalExecutor (subprocess) or DockerExecutor (SWE-bench image)
    loop.py              orchestrator: reproduce -> fix, ablation modes (full, no_context, single_shot, baseline)
    issue.py             bug report -> BugSpec (SLM)
    explore.py           repository explorer + test generator (SLM)
    repair.py            fault localiser, patch generator (SLM), patch applier, git-compatible diffs
    verify.py            pytest parsing, result analyser (evidence matching + SLM judge), patch validator
    models.py, llm.py, report.py
  evaluation/            evaluator only: reads benchmarks/*/private, so the agent must never import it
    evaluator.py         grade one patch with the hidden tests in Docker (official swebench grading)
    validate.py          checks that every bug fails on buggy code and passes with the real fix
  run.py                 CLI: `local` (any project) and `swebench` (batch, resumable, optional grading)
benchmarks/
  swe_bench_lite/public  bug reports (+ code snapshots, built locally). private/ is never committed
  shopcart, multiloc, bugsinpy   mid-term benchmarks (second dataset)
scripts/build_benchmark.py       builds benchmarks/swe_bench_lite
tests/                   unit tests, no model or Docker needed
```

How the pieces fit: the agent always reads and edits a **host copy** of the buggy code (`public/code/<id>`). The executor decides where pytest runs. On SWE-bench, every run copies only the files the agent wrote into the bug's official Docker image (`/testbed`, exact environment) and rolls them back afterwards.

## Setup

```bash
python3.12 -m venv .venv-autose
.venv-autose/bin/pip install -e . pytest requests
ollama pull qwen2.5-coder:3b        # or qwen2.5-coder:7b / qwen3:8b
.venv-autose/bin/python -m pytest tests -q
```

## Run

Local project (no Docker). Build the ShopCart bugs first with `python benchmarks/shopcart/build.py`:

```bash
python -m autose.run local --repo benchmarks/shopcart/work/shop_discount/buggy \
  --fixed benchmarks/shopcart/work/shop_discount/fixed \
  --issue benchmarks/shopcart/issue_shop_discount.json --fix
```

SWE-bench Lite. This needs Docker, `python scripts/build_benchmark.py code --only <id>` and, for `--grade` / `--check-f2p`, the private files:

```bash
python -m autose.run swebench --ids pallets__flask-4045 --fix --check-f2p --grade
python -m autose.run swebench --subset benchmarks/swe_bench_lite/subset.json --mode no_context --run-name h1_no_context
```

Results go to `runs/<run-name>/`: one JSON + Markdown report per bug, and `summary.jsonl` with one line per bug. A run resumes where it stopped.

Options: `--mode full|full_v2|no_context|single_shot|baseline`, `--fix-mode fix_full|fix_single|fix_no_repro|fix_multi`, `--report full|raw` (with or without the Steps/Expected/Observed sections), `--budget`, `--model`, `--seed`.

## Status (22 Nov)

- [x] Agent ported, with all 35 mid-term + glue unit tests passing
- [x] Local end-to-end run (ShopCart)
- [ ] **DockerExecutor on a real SWE-bench image** (not yet run: needs a machine with Docker). Check the pytest install in the image, the conda activation and the file sync
- [ ] Context retrieval for large repos (`explore.py` scans every file; slow and weak on django/sympy)
- [ ] Evaluation subset `benchmarks/swe_bench_lite/subset.json`
