# ReproAgent: mid-term progress report (reproduce, then fix)

Keshav Dubey (2025204041) · Rishabh Singh (2025204025) · Aasritha Kalluri (2025204019)
AutoSE, Prof. Karthik Vaidhyanathan

## 1. Status at a glance

| Proposal deliverable | Status |
|---|---|
| (i) Bug-reproduction agent on a local SLM | **Working prototype.** All 6 workflow steps are implemented end-to-end on `qwen2.5-coder:3b` via Ollama, on an 8 GB laptop |
| (ii) Interface for supplying bug reports and projects | CLI: `python -m reproagent --repo … --issue <json, md or GitHub URL> [--fixed …] [--fix]`, and a **local web UI** (`python -m reproagent.webui`): browse every saved result, run the agent live on a new bug, and see the evaluation summary |
| (iii) Generated and executed reproduction tests | Tests run in a sandboxed copy of the project. Retries are driven by execution feedback |
| (iv) Result report with evidence | Markdown + JSON per bug: verdict, final test, pytest evidence, attempt log, timings |
| (v) Experimental findings vs. baselines | Benchmark of 20 bugs (12 real BugsInPy + 8 seeded) + 1 vague report, 5 configurations. ReproAgent: 14/20 valid vs. 6/20 for the baseline (§4) |
| **(vi) Bug resolution (added on the instructor's request)** | **Working prototype.** Fault localisation from the failing reproduction test, SLM patch generation, sandboxed validation (the reproduction test must pass, no regressions in related existing tests), refinement from feedback. CLI flag `--fix`. Evaluated in 4 configurations with hidden oracles: 7/17 reproduced bugs correctly fixed by `fix_full` (§4.3). **Multi-location fixing** (`fix_multi`): 2/4 reproduced multi-location bugs fixed correctly vs. 0/4 for `fix_full` (§4.4) |

## 2. Architecture (stable)

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full derivation: use case → bounded contexts → shared models → modules → classification → number of agents. Summary:

* **3 bounded contexts (as on the slides):** Issue Management, **Bug Resolution** (phase 1: reproduce; phase 2: fix), Verification.
* **Shared models:** `BugSpec` (behaviour / acceptance criteria), `ReproTest`, **`Patch`** (proposed patch), `Analysis` / `PatchCheck` (feedback), `Outcome` / `FixOutcome` (status + evidence).
* **Modules:**
  * 8 tools: issue retrieval, AST repo explorer, fault localizer, patch applier, similar-code finder (consistency sweep), sandboxed test executor, patch validator (regression runner, partial-progress detection), and the evaluation-only fail-to-pass / hidden-oracle checks.
  * 5 SLM capabilities: bug specifier, test generator, result judge, patch generator, consistency-sweep reviewer (`fix_multi`).
* **Agent count: 1 agent with a fixed two-phase workflow**, using the SLM in 5 narrow roles over a shared tool layer. We chose against 2–3 agents for four reasons: the 8 GB RAM / one-model constraint; the weak free-form tool calling of 3B models; the need for clean ablations; and the strict reproduce → fix dependency, which gives a second agent nothing to do in parallel.

## 3. Benchmark

| Set | Bugs | Source | Notes |
|---|---|---|---|
| ShopCart | 8 + 1 vague | In-house e-commerce app with seeded bugs | Includes the course's "discount applied below ₹1,000" use case. `shop_vague` ("the website doesn't work properly") should give *needs more information* |
| Multi-location | 5 | Seeded in ShopCart (`benchmarks/multiloc`) | Each bug needs changes in **two** definitions: the same mistake in two functions, two methods, caller + constant across files, or two causes of one symptom. `build.py` checks that fixing only one place still fails the hidden oracle |
| BugsInPy | 12 | youtube-dl (7), thefuck (4), tqdm (1) | Buggy tree = GitHub tarball of the buggy commit. Fixed tree = buggy + BugsInPy patch. Reports are symptom-only rewrites (never mention the fix). `scripts/validate_benchmark.py` checks that every bug manifests on this machine |

**Validity criterion (proposal §5):** a test counts as a *valid reproduction* only if the agent accepts it (it fails on the buggy version) **and** it passes on the fixed version. The fixed version is used only for scoring, never shown to the agent.

**Valid by meaning (second, reported score):** strict fail-to-pass also rejects a test whose *only* failure on the fixed version is the wording of an expected error message (`pytest.raises(..., match=...)`), e.g. the test expects "quantity must be positive" and the developer's code says "quantity cannot be negative". We keep the strict score as the headline and add a second score. A test is valid by meaning if every failure on the fixed version is such a wording difference and the two messages mean the same thing. Three layers decide:
1. **Report rule (no model):** if the report is about the message itself (it talks about the message or quotes the expected wording), the wording must match, so it stays invalid.
2. **Text rule:** one message contains the other.
3. **SLM message judge** (temperature 0): "do these two messages express the same rule or problem?". Its prompt was chosen on a separate dev set of 14 message pairs, none from the benchmark: 14/14 correct, including opposite-meaning traps such as "must be positive" vs. "must be negative". A longer prompt variant scored 12/14 and was rejected.

`scripts/rescore_f2p.py` adds this score to saved results without re-running the agent.

**Fix criterion:** each bug has a hidden oracle (`benchmarks/*/oracle_checks.py`): a few assertions on the intended behaviour, including boundary values, that fail on the buggy version and pass on the developer-fixed version. A patch is *plausible* if the agent's own validation accepts it. It is *correct* if it is plausible **and** passes the hidden oracle. This is the plausible-vs-correct distinction from the program-repair literature. It catches patches that only special-case the test's input.

## 4. Preliminary results

All numbers come from **one run per configuration** with `qwen2.5-coder:3b`, CPU only, a budget of 4 attempts and fail-to-pass scoring. Raw per-bug JSON/Markdown is in `results/`, and the auto-generated tables are in [`results/SUMMARY.md`](../results/SUMMARY.md).

### 4.1 Valid reproductions (fail-to-pass) per configuration

| Mode | ShopCart (8) | BugsInPy (12) | **Total (20)** | False positives* | Avg attempts | Avg time / bug |
|---|---|---|---|---|---|---|
| `full` (ReproAgent) | 5 | 9 | **14 (70%)** | 1 | 2.2 | 80 s |
| `full_v2` (+ symbol-aware feedback) | 3 | 11 | **14 (70%)** | 2 | 2.4 | 85 s |
| `no_context` (H1 ablation) | 0 | 7 | **7 (35%)** | 2 | 2.9 | 100 s |
| `single_shot` (H2 ablation) | 2 | 7 | **9 (45%)** | 0 | 1.0 | 45 s |
| `baseline` (description only, 1 shot) | 0 | 6 | **6 (30%)** | 1 | 1.0 | 37 s |

\*False positive = the agent reported *reproduced*, but the test also fails on the fixed version. Every one was caught by the fail-to-pass check.
The vague report `shop_vague` correctly ended as **needs more information** in all 5 modes, after ~10 s.

Per-bug outcomes:

| Bug | full_v2 | full | no_context | single_shot | baseline |
|---|---|---|---|---|---|
| shop_coupon | V (2) | V (1) | NR | NR | NR |
| shop_discount (slide use case) | NR | V (1) | NR | NR | NR |
| shop_empty_checkout | V (1) | V (1) | NR | V (1) | NR |
| shop_gst | FP | V (2) | FP | NR | FP |
| shop_remove_missing | NR | NR | NR | NR | NR |
| shop_set_quantity_zero | V (4) | V (2) | NR | NR | NR |
| shop_shipping | FP | FP | NR | NR | NR |
| shop_stock | NR | NR | NR | V (1) | NR |
| thefuck_25 | V (3) | NR | NR | NR | NR |
| thefuck_27 | NR | NR | NR | NR | NR |
| thefuck_31 | V (3) | V (3) | NR | NR | NR |
| thefuck_32 | V (3) | NR | NR | NR | NR |
| tqdm_1 | V (1) | V (1) | V (1) | V (1) | V (1) |
| youtube-dl_3 | V (2) | V (4) | FP | V (1) | NR |
| youtube-dl_11 | V (1) | V (1) | V (3) | V (1) | NR |
| youtube-dl_13 | V (1) | V (1) | V (1) | V (1) | V (1) |
| youtube-dl_17 | V (1) | V (3) | V (3) | NR | V (1) |
| youtube-dl_27 | V (2) | V (1) | V (1) | V (1) | V (1) |
| youtube-dl_29 | V (1) | V (1) | V (1) | V (1) | V (1) |
| youtube-dl_43 | V (1) | V (1) | V (1) | V (1) | V (1) |

V (n) = valid reproduction at attempt n, FP = false positive, NR = not reproduced.

### 4.2 Reading the results against the hypotheses

* **H1 (code context helps): supported.** `full` has 14/20 valid reproductions vs. 7/20 for `no_context`. The effect is largest on ShopCart (5 vs. 0), whose API the model has never seen. On youtube-dl the gap is small (`no_context` still gets 6/7 youtube-dl bugs), probably because youtube-dl is in the model's pre-training data. This is a contamination threat for real-world benchmarks.
* **H2 (execution feedback / retries help): supported.** `full` has 14/20 vs. 9/20 for `single_shot`. In `full`, 5 of the 14 valid reproductions needed more than one attempt. The cost is time: about 80 s vs. 45 s per bug.
* **Simple baseline:** 6/20. The full agent more than doubles it.
* **Accuracy (did it fail for the intended reason?):** 14 of the 15 tests that `full` reported as reproduced were valid fail-to-pass tests (93%). The one false positive (`shop_shipping`) is analysed in §5.
* **Valid by meaning:** of all 9 strict failures among accepted tests, exactly one was only an error-message wording difference: `ml_quantity` ("quantity must be positive" vs. "cannot be negative"). The judge accepted it, so multi-location reproduction is 1/5 strict and **2/5 by meaning**. Every other false positive is a real test mistake and stays invalid; the headline numbers above do not change.
* **`full_v2` (symbol-aware feedback):** better on BugsInPy (11/12 vs. 9/12). The thefuck bugs, which failed because of hallucinated imports, are now reproduced (`thefuck_25`, `thefuck_32`). It is worse on ShopCart (3/8 vs. 5/8). For `shop_discount`, the first attempt (which `full_v2` does not change) already produced a different test under the same prompt and seed. So this difference is **run-to-run variance** of the CPU sampler, not an effect of the new feedback. The same variance shows in our two `full` runs on BugsInPy (old vs. fixed harness): both gave 9/12, but `thefuck_31` and `thefuck_32` swapped outcomes. **With one run and N = 20, differences of 1–2 bugs are not meaningful.** Repeated runs are the first item of remaining work.
* **Time:** a valid reproduction takes ~60–90 s on CPU (2–4 SLM calls of 20–40 s each). Test execution itself is under 2 s.

### 4.3 Bug resolution (fixing)

Fixing is attempted for the **17 bugs with an accepted reproduction test** (6 ShopCart, 11 BugsInPy; the test comes from `full`, or from `full_v2` for `thefuck_25`/`thefuck_32`). Same model, CPU only, one run. Full tables: `results/FIX_SUMMARY.md`; per-bug patches and diffs: `results/fix/`.

*Plausible* = the agent's own validation accepted the patch. *Correct* = plausible **and** the hidden oracle passes (boundary-value checks the agent never sees; for BugsInPy derived from the developers' fix).

| Mode | Plausible | **Correct** | Plausible but wrong | Precision (correct / plausible) | Avg SLM calls | Avg time |
|---|---|---|---|---|---|---|
| `fix_full` (repro test + feedback, budget 3) | 8/17 | **7/17 (41%)** | 1 | 88% | 2.8 | 145 s |
| `fix_single` (repro test, 1 attempt) | 4/17 | **4/17 (24%)** | 0 | 100% | 1.0 | 33 s |
| `fix_no_repro` (report only, 1 attempt) | 11/17 | **4/17 (24%)** | 7 | 36% | 1.0 | 42 s |

Correct fixes by `fix_full`: `shop_coupon`, `shop_discount`, `shop_empty_checkout`, `thefuck_32`, `youtube-dl_11`, `youtube-dl_17`, `youtube-dl_43`. (By benchmark: ShopCart 3/6, BugsInPy 4/11.) The running example `shop_discount` (buggy `DISCOUNT_THRESHOLD = 100`) is fixed on the 2nd attempt: the patch makes `bulk_discount` compare against ₹1,000. It is correct, but it hard-codes the value instead of correcting the constant, a style weakness. In a later live run the agent instead *inverted* the comparison (`<`), which passes the reproduction test and `tests/test_cart.py` but is wrong. That is why the hidden oracle and stronger regression tests matter.

**Reading against the hypotheses:**
* **H3 (a reproduction test helps fixing): supported, mainly through validation.** With the same budget of one attempt, using the reproduction test does not raise the number of correct fixes (4 vs. 4). But without it the agent **cannot tell a fix from a non-fix**: 11 patches are "plausible" (they only had to avoid regressions) and 7 of them are wrong (precision 36%). With the reproduction test every accepted single-shot patch was correct, and `fix_full` was right in 7 of 8 cases. For a developer, this is the difference between a patch that can be trusted and a guess.
* **H2 for fixing (feedback helps): supported.** `fix_full` vs. `fix_single`: 7 vs. 4 correct. `shop_discount`, `thefuck_32` and `youtube-dl_11` were each fixed on the 2nd attempt after feedback (not applicable → plausible; regression → plausible).
* The two settings are complementary on some bugs: `fix_no_repro` correctly fixed `shop_set_quantity_zero` and `shop_shipping`, where `fix_full` did not (see §5, classes 9 and 11). With N = 17 and one run, differences of 1–2 bugs are noise.

**Patch attempt outcomes in `fix_full`** (40 attempts): repro test still fails 21, not applicable 6, regression caught by existing tests 5, plausible 8. Regressions are real: e.g. for `youtube-dl_13` every `urljoin` rewrite broke `test/test_utils.py::test_urljoin`, so the validator rejected it, as it should.

### 4.4 Multi-location fixes (`fix_multi`)

All 17 developer fixes in our original benchmark change a **single definition**, so we built 5 multi-location bugs (§3) and a `fix_multi` mode with four additions (details in ARCHITECTURE.md):
1. one reproduction test function per symptom;
2. a prompt that allows changing several definitions/files;
3. **cumulative repair**: a patch that makes some test functions pass without regressions is kept; the next attempt re-localises on the patched code, using only the still-failing test functions, and cannot see (so cannot undo) the kept definitions; progress earns one extra attempt (max 5);
4. a **consistency sweep** for the same mistake in similar code.

| | Multi-location bugs (4 reproduced) | Single-location bugs (17) |
|---|---|---|
| `fix_full` | 0 correct | 7 correct (41%), 1 plausible-but-wrong |
| **`fix_multi`** | **2 correct** (`ml_boundaries`, `ml_quantity`) | 7 correct (41%), 0 plausible-but-wrong |

* Both successes follow the intended path: patch 1 fixes one place and is kept (`partial`). Re-localisation then points at the second place (`shipping_fee`, `Cart.add`), and patch 2 completes the fix, confirmed by the hidden oracle. `fix_full` cannot do this: it throws away a half-fix and starts again.
* `fix_multi` does not hurt single-location bugs (same 7/17; the set differs by run-to-run variance: `shop_set_quantity_zero` gained, `thefuck_32` lost).
* **The bottleneck is now reproduction:** only 1 of the 5 multi-symptom reports got a *valid* reproduction test, and `ml_gst` none. The tests for `ml_shipping` and `ml_coupon` were wrong (argument types, reversed assertion), so no correct fix could pass them (§5 class 14).
* Two design bugs were found and fixed while evaluating: (a) partial progress on the last attempt left no budget; (b) after progress the model kept editing (and once undid) the already-fixed definition. This led to focusing re-localisation on the still-failing test and hiding kept definitions. The 4 multi-location runs were re-run after these fixes; the 17 single-location `fix_multi` runs were made before them (they never reached a partial step, which is where the fixes act).
* The sweep never fired usefully here: these bugs are caught by the per-symptom tests first. It is kept for mistakes that the test does not cover.

## 5. What we learned / known failures

In `full` mode, 26 of the 45 attempts were **invalid tests** (the test could not run). That is the largest failure class, ahead of reproductions (15) and tests that passed (4).

| # | Failure class | Example (from `results/`) | Status / mitigation |
|---|---|---|---|
| 1 | **Import / API hallucination** | `shop_stock`: `cart.products["P1"]` (the real attribute is `items`), 4 times in a row. `shop_remove_missing`: `from shopcart.product import …`. thefuck: `NameError: Command`, `types` | `full_v2` answers these errors with the real definition site and attributes from the AST index. This fixed `thefuck_25` and `thefuck_32`. It does not yet cover stdlib names: `shop_gst` in `full_v2` failed 3× on `NameError: Decimal` |
| 2 | **Wrong oracle accepted (false positive)** | `shop_shipping`: `Product("product1", 100, 10)`, wrong argument order, but the failing value happened to match the report. `shop_gst` (`no_context`, `baseline`, `full_v2`): the expected value is computed with float `round(12.25*0.18, 2)`, or left unrounded (`2.205`), so the test also fails on the fixed code. `youtube-dl_3` (`no_context`): an invented expected value, plus our evidence tokenizer mis-read a string containing quotes | All were caught by fail-to-pass. **This shows why the agent's own "reproduced" claim needs an independent validity check.** Planned: constructor signature hints, rejecting expected values computed with floats, fixing quoted-string tokenisation |
| 3 | **Scenario drift** | `shop_discount` (`full_v2`): the test used a ₹2,000 TV instead of the report's ₹800 order. Evidence matching correctly rejected it 4 times ("report observed 80/720, your test produced 86"), but the 3B generator did not converge | The feedback is right but not followed. Planned: put the report's concrete values into a test skeleton |
| 4 | **Reversed oracle** | `shop_remove_missing`: the test asserts `pytest.raises(KeyError)`, i.e. it asserts the *bug* rather than the expected behaviour | Planned: static check before execution |
| 5 | **Unreliable 3B judge** (seen during development) | Too lenient: it accepted a test with the wrong subtotal (860). Too strict: it rejected a correct test because "80 is not the same as observed" | Replaced by deterministic evidence matching. The SLM judge is now only a fallback |
| 6 | **Harness / infrastructure** | Old youtube-dl emitted hundreds of SyntaxWarnings, and our output truncation dropped the failure section (fixed, BugsInPy `full` re-run). One generation ran away and hit the 600 s HTTP timeout (`thefuck_32`, `no_context`; fixed with `num_predict` = 1536 and re-run) | Fixed |
| 7 | **Not reproduced by any mode** | `thefuck_27` (the model never builds a valid `Command` for the `open` rule), `shop_remove_missing` | Open |

**Bug resolution failures** (`fix_full`, from `results/fix/`):

| # | Failure class | Example | Status / mitigation |
|---|---|---|---|
| 8 | **Patch does not fix the root cause** (largest class: 21 of 40 attempts) | `youtube-dl_3`, `_27`, `_29`, `shop_gst`: three rewrites each, and the reproduction test still fails the same way | Feedback shows the new failure, but the 3B model often changes surface details. Planned: show the failing line/values from the traceback inside the candidate code; try a 7B model |
| 9 | **Right location found, wrong definition edited** | `shop_set_quantity_zero`: the test asserts on `cart.remove()`, and the model rewrote `remove` instead of `set_quantity` (ranked first) | Planned: tell the model which definition the localiser ranks first and why |
| 10 | **Regressions** (5 attempts) | `youtube-dl_13`: every new `urljoin` broke `test_utils.py::test_urljoin` | **Caught by the validator** (works as intended) |
| 11 | **Garbage in, garbage out** | `shop_shipping`: the reproduction test was a false positive (§5 class 2), so the "fix" set `FREE_SHIPPING_THRESHOLD = 0`, which makes that test pass. It is plausible but wrong, and the hidden oracle catches it | Fixing inherits reproduction errors: improving the reproduction precision (class 2) also improves fixing |
| 12 | **Truncated / invalid output** | `tqdm_1`: long `tqdm` methods hit the 1,536-token output cap, giving an unterminated string | Planned: send only the method, not the whole class; raise the cap for long definitions |
| 13 | **Harness bug (fixed)** | New imports were inserted inside a multi-line `from x import (...)`, so `youtube-dl_13`/`_29` patches were syntax errors | Fixed (insert after the end of the last import node), unit test added, the 4 affected runs re-run |
| 14 | **Multi-symptom reports are hard to reproduce** | `ml_shipping`: test passes the whole bill dict to `shipping_fee`. `ml_coupon`: asserts `apply_coupon(' save10 ') == False` (the bug, not the expectation). `ml_gst`: 4 invalid tests | Same root causes as classes 1, 2 and 4, multiplied by the number of symptoms. Planned: validate each test function separately and keep the valid ones |

**Limitations:**
* Small N (20 + 1) and a single run per configuration.
* The reports are hand-written, symptom-only rewrites.
* One model.
* Fixing: 17 bugs, mostly single-location fixes; the hidden oracles are ours (ShopCart) or derived from the developers' fix (BugsInPy), and only up to 2 related test files are used as regression tests.
* CPU-only timings.
* Possible pre-training contamination for youtube-dl.
* No container isolation.

## 6. Changes with respect to the proposal

* **Bug resolution is now in scope** (instructor's request after the proposal). The proposal had excluded automatic fixing. We added it as a second phase that **depends on the reproduction**: the accepted test drives fault localisation and is the main acceptance check for the patch. This restores the slides' *Bug Resolution* context, the *proposed patch* shared model, and the root-cause / code-modifier modules. It adds an RQ/hypothesis **H3: a reproduction test makes a small model's fixes more often correct** (`fix_full` / `fix_single` vs. `fix_no_repro`).

* **Multi-location fixing** (`fix_multi`) and a **web UI** were added after the first fixing evaluation, because real fixes often touch more than one place and our first fixer could only succeed if the model got every place right in one answer.
* **Benchmark size and source:** 20 reproducible bugs + 1 vague report (proposal: 20–30). Real bugs come only from lightweight, pure-Python BugsInPy projects (youtube-dl, thefuck, tqdm), because pandas, keras etc. cannot be installed and run at old commits on an 8 GB machine.
* **Environment compatibility:** the machine has Python 3.14, but BugsInPy targets 3.6–3.8. We added two tiny shims (`imp.load_source`, `pipes.quote`). We excluded `thefuck_18` (needs `inspect.getargspec`) and changed one example in `youtube-dl_13` whose behaviour differs on modern `urllib`. All are documented in the manifest.
* **Result analyzer design:** the proposal assumed an LLM would judge whether a failure matches the report. A 3B judge proved unreliable in both directions (§5), so we added a **deterministic evidence-matching step** (actual value or exception vs. the observed behaviour in the report). The SLM judge is used only when that step cannot decide.
* **Hardware:** the GTX 1650's driver (472.19, CUDA 11.4) is too old for current Ollama, so all numbers are CPU-only (~20–40 s per SLM call). A driver update should give a large speed-up.
* **Sandbox:** temporary project copy + subprocess + timeout. No container isolation yet (Docker is too heavy for the target laptop). Planned as an optional backend.

## 7. Remaining work (plan to final submission)

1. **Close the import/API-hallucination gap** (largest failure class): evaluate `full_v2` (symbol-aware feedback) on the whole benchmark, then add signature hints for constructors (catches the argument-order false positive).
2. **Reversed-oracle detection:** reject tests that use `pytest.raises(<the exception the report complains about>)`, or that assert the observed value, before execution.
3. **Grow the benchmark to ~30 bugs** (more thefuck/youtube-dl/tqdm/PySnooper/cookiecutter) and **repeat runs with 3 seeds**: one run per configuration is too noisy for H1/H2 claims.
4. **Model comparison:** qwen2.5-coder:1.5b / 3b / 7b and qwen3:8b (needs GPU driver update). Report time vs. success.
5. **Usability:** ~~simple web interface~~ (done: `python -m reproagent.webui`). Optional Docker sandbox.
6. **Bug resolution:** (a) use the developers' own test files from BugsInPy as the full regression suite; (b) ~~patches across several definitions/files~~ (done: `fix_multi`, §4.4); grow the multi-location benchmark with real multi-hunk BugsInPy bugs; (c) add the traceback line and local values to the patch prompt (class 8); (d) evaluate with a 7B model on GPU, where patch quality should gain most; (e) also run fixing on the bugs reproduced by `full_v2` and repeat with 3 seeds.
7. Final report: statistical comparison for H1/H2/H3, failure taxonomy, threats to validity (hand-written reports, single model, small N).
