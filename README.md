# Autose: ReproAgent on SWE-bench Lite

AutoSE course project (Keshav Dubey, Rishabh Singh, Aasritha Kalluri).

Autose is a bug-fixing agent that runs on a **local small language model** (Ollama `qwen3:8b`, no hosted AI). Given a bug report and a Python repository, it works in four stages:

1. **Bug Understanding.** It searches a per-commit *knowledge package* (AST symbols, code graph, BM25) for the code the report is about. Then it writes a pytest test that **reproduces** the bug and refines that test using execution feedback.
2. **Code Modification.** It localises the fault using the failing test, the program slice and the search results. It then writes a patch and applies it safely through the AST.
3. **Testing.** The reproduction test must now pass, and the tests of the changed module must not regress. The result is bound to the patch's hash.
4. **Review.** An independent reviewer rejects patches that edit tests or use names that exist nowhere in the project. Only a tested patch that the reviewer did not reject is frozen.

Every step publishes a schema-checked JSON artifact on a file **blackboard**. A **loop controller** stops repeating failures.

## Evaluation

- **SWE-bench Lite.** 295 of the 300 bugs are validated. Grading uses the official SWE-bench harness in Docker.
- **Mid-term datasets.** ShopCart and BugsInPy are run locally.

## Layout

```
autose/
  agent/                  the agent (never imports autose/evaluation)
    loop.py               orchestrator: reproduce -> localise -> patch -> validate -> review -> freeze
    issue.py              bug report -> BugSpec
    explore.py            RepoExplorer (mid-term keyword search), IndexedExplorer (knowledge package), test generator
    repair.py             fault localiser (traceback, test calls, program slice, search results), patch generator/applier
    verify.py             pytest parsing, result analyser + false-reproduction guards, patch validator
    blackboard.py         BUG_SPEC / CHANGE_PLAN / TEST_REPORT / REVIEW_REPORT / FROZEN_PATCH + events.jsonl
    review.py             independent review (rules, optional local model)
    control.py            loop controller (failure fingerprints: switch after 2, stop after 3)
    workspace.py          LocalExecutor (subprocess) and DockerExecutor (SWE-bench image)
  intel/                  repository intelligence
    index.py              knowledge package per repo@commit (SQLite: symbols, CALLS/INHERITS/IMPORTS, FTS5)
    retrieve.py           bm25 / hybrid / graph retrieval
    flow.py               CFG, reaching definitions, control dependence, backward slicing
  evaluation/             evaluator only: reads benchmarks/*/private
    evaluator.py          official grading of one patch in Docker
    localization.py       search quality against the developers' fix (file@k, function@k)
    localizer_handoff.py  does handing search results to the fault localiser help?
    pipeline.py           information-flow health: 11 checkpoints and the hand-offs between them
    quality.py            quality scorecard: a rubric per component, 80% gate
    night.py              night runner: configs A/C/D/E on every bug, one image at a time
  run.py                  CLI: `local` (any project) and `swebench` (batch, resumable)
benchmarks/swe_bench_lite/public   bug reports + code snapshots without git history; private/ never committed
docs/adr/                 design decisions 0006-0011
tests/                    unit tests (no model or Docker needed)
```

## Setup

```bash
python3.12 -m venv .venv-autose
.venv-autose/bin/pip install -e ".[dev]" pytest requests docker swebench
ollama pull qwen3:8b
python scripts/build_benchmark.py code --only pallets__flask-4045     # code snapshot(s)
.venv-autose/bin/python -m pytest tests -q
```

## Run

**One SWE-bench bug, full design**, with grading by the hidden tests:

```bash
python -m autose.run swebench --ids pallets__flask-4045 --model qwen3:8b --fix --retrieval graph \
  --review rules --loop-control --check-f2p --grade
```

**A local project** (no Docker needed):

```bash
python -m autose.run local --repo benchmarks/shopcart/work/shop_discount/buggy \
  --fixed benchmarks/shopcart/work/shop_discount/fixed --issue benchmarks/shopcart/issue_shop_discount.json --fix
```

**The ablation** runs on the 40-bug subset. It resumes where it stopped, deletes each Docker image after its bug, and cleans up its containers on Ctrl-C:

```bash
python -m autose.evaluation.night --configs A C --tag night1 --model qwen3:8b
python -m autose.evaluation.night --configs D E --tag night2 --model qwen3:8b
```

| Config | What it adds |
|---|---|
| A | mid-term agent: keyword search, test-guided repair (baseline) |
| C | knowledge package, hybrid search |
| D | code-graph re-ranking; the search results also go to the fault localiser |
| E | D + independent review + loop controller (full design) |

## Measurements

```bash
python -m autose.evaluation.localization --out results/localization_v2.json    # search, 195 bugs, no model
python -m autose.evaluation.localizer_handoff --all                            # search -> localiser hand-off
python -m autose.evaluation.pipeline runs/night1_C --html ../pipeline.html --js ../pipeline-data.js
python -m autose.evaluation.quality --html ../quality.html                     # quality gate, ~1 min
```

Search results on 195 bugs, scored against the developers' fix:

| Search | File in top 5 | Function in top 10 |
|---|---|---|
| Mid-term keyword search | 53% | 24% |
| Graph search | 81% | 57% |

On sympy, which was never used for tuning, function-in-top-10 goes from 25% to 51%.

The quality scorecard checks 10 components against 32 rubric criteria. All 10 components currently pass the 80% gate.

## Rules the code keeps

- **Hidden ground truth.** The fix and the hidden tests are evaluator-only. The agent never imports `autose/evaluation`, and the snapshots and containers carry no git history after the buggy commit.
- **Test results bind to a patch.** A test result counts only for its exact patch hash. Only a tested patch that the review did not reject is frozen.
- **Deterministic control.** The control flow is deterministic Python. The model fills narrow roles: spec, test, patch and the optional review.
