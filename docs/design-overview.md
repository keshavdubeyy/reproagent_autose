# ReproAgent — How We Planned It (Alignment Doc)

> Purpose: one plain-language page so the team agrees on **what we built, why, and which lecture idea each part comes from**. Not the report. When we write the report, we lift from here.

---

## 1. The problem in one paragraph

A bug report says "X is broken." Before anyone can fix it, someone has to **prove** it with a test that fails. That step is slow and manual. ReproAgent reads the bug report, explores the repo, writes a **failing pytest** that reproduces the bug, then (per reviewer feedback: *"make the harness also fix the bug"*) proposes a **patch** and proves it makes that test pass without breaking others. Everything runs **locally** on a small model (`qwen3:8b` via Ollama) — no cloud AI.

---

## 2. How we planned the agent (step by step)

We followed the lecture recipe for *Use Case 1: Autonomous Bug Fixing* — **don't start with "an agent", start with the job, split it into parts, and only use AI where a rule can't do it.**

1. **Write down the job.** "Bug report in → failing test out → patch out → proof out."
2. **Split into bounded contexts** (lecture: Issue Management / Bug Resolution / Verification & Validation).
3. **For every part, ask: does this need AI, or is a plain tool enough?** (lecture design questions: *need for AI? agentic vs fixed workflow?*)
4. **Give AI only the fuzzy parts** (understanding text, deciding what to look at, writing code). Everything checkable is **deterministic code**.
5. **Wrap the model in a harness** — tools, budgets, verification, retries. (Lecture 10: *Agent = model + harness*.)
6. **Put a human at the irreversible step** (confirm understanding, Apply patch).
7. **Write the spec first** (`spec.md`, EARS) and record decisions (ADRs).
8. **Measure against baselines**, report quality *and* cost together.

---

## 3. Where we use AI and where we don't

| # | Part (lecture name) | What it does in ReproAgent | AI or not? | Code |
|---|---|---|---|---|
| 1 | Issue Retrieval | Paste text, attach `.md`, or fetch a GitHub issue URL | **Tool** | `session.fetch_github_issue` |
| 2 | Bug Specification | Turn messy report into title / observed / expected / steps / missing | **AI** (one JSON call) | `session._understand` |
| 3 | Human check | User confirms or edits the understanding | **Human** | UI "Confirm" card |
| 4 | Repository Explorer | list files, search code (≤50 hits), read file | **Tool** | `workspace.py` |
| 5 | Test Designer | Decides where to look and writes the reproduction test | **AI** (ReAct loop) | `pipeline._reproduce` + `AgentLoop` |
| 6 | Test Executor | Runs pytest in a sandbox copy | **Tool** | `workspace.run_pytest` |
| 7 | Result Analyzer | Is it a *real* failure (exit 1) or just an import/syntax error? | **Deterministic** (we chose *not* AI) | `pipeline.classify_failure` |
| 8 | Root Cause + Fix Planner/Gen | Reads code, edits source to fix | **AI** (ReAct loop) | `pipeline._fix` |
| 9 | Code Modifier | `edit_file` / `replace_lines`; test files are read-only | **Tool** | `workspace.edit_file` |
| 10 | Verification | repro test passes? any previously-passing test now fails? (+ oracle vs real fix in eval) | **Deterministic** | `_fix`, `_oracle`, `_gt_check` |
| 11 | Apply | Write test (+ patch) into user's repo, refuse if repo changed | **Human approval + tool** | `session.apply` |

**Count:** 3 AI steps, 6 tool/deterministic steps, 2 human steps. The model never judges its own success — the harness does. This is our main design claim.

---

## 4. Lecture concepts → what we did

Legend: ✅ used · 🟡 partly / could add cheaply · ❌ deliberately not used (with reason)

### Agent basics
| Concept | Status | In ReproAgent |
|---|---|---|
| ReAct loop (think → act → observe) | ✅ | `AgentLoop`: tool call → observation → next step, `finish` tool to stop |
| Function / tool calling | ✅ | Ollama native tools; schemas built by `tool()` |
| Small models for tool calling | ✅ | `qwen3:8b`, local |
| Agent = model + harness | ✅ | Model is frozen; all our work is the harness |
| Agentic vs fixed workflow | ✅ | **Hybrid**: fixed outer pipeline (understand → reproduce → verify → fix → verify), agentic only inside reproduce and fix |
| "Too many agents is never good" | ✅ | One loop, reused for two stages. No multi-agent |

### Model control & prompting
| Concept | Status | In ReproAgent |
|---|---|---|
| Temperature / top_p | ✅ | temp 0.2 (code needs consistency) |
| Zero-shot / few-shot | ✅ zero-shot | System prompts with rules, no examples (keeps context small) |
| Chain-of-thought | ❌ | `think: false` — reasoning tokens cost time/energy on an 8B model; loop observations do the job |
| Prompt chaining | ✅ | intake JSON → repro prompt → fix prompt, each fed the previous output |
| Structured output | ✅ | intake uses `format: json` |

### Harness / loop engineering (L07)
| Concept | Status | In ReproAgent |
|---|---|---|
| Nested loops (tool / task / session / human) | ✅ | tool step → attempt (max 15 steps) → stage retries (3) → human confirm/apply |
| Budgets | ✅ | step budget, retry budget, pytest timeout |
| Verification: deterministic | ✅ | pytest exit codes, suite diff |
| Verification: state | ✅ | baseline failing tests vs after-patch; repo-changed check before apply |
| Verification: grounding | ✅ | test must fail on buggy code **and** pass on fixed code (fail-to-pass oracle) |
| Verification: model-as-judge | ❌ | Deliberately avoided — an 8B judge is unreliable; pytest is a better judge |
| Verification: human review | ✅ | confirm + apply cards in UI |
| Errors as observations | ✅ | tool errors returned to model, not crashes |

### Coding-agent anatomy (SWE-agent ACI)
| Concept | Status | In ReproAgent |
|---|---|---|
| Bounded search (≤50 results) | ✅ | `search_code` capped |
| File viewer with line numbers | ✅ | `read_file` |
| Safe editor | ✅ | `edit_file` with unique-match + `line` disambiguation |
| Workspace runtime / sandbox | ✅ | temp copy of repo, path-confined, scrubbed env |
| Context manager | 🟡 | budget-limited; no summarization yet |

### Patterns (L08, Liu et al.)
| Pattern | Status | Why |
|---|---|---|
| Proactive goal creator | ✅ | intake asks for missing info (`needs_info`) |
| Reflection (harness feedback) | ✅ | failed attempt's output + diff fed into next attempt |
| Human-in-the-loop approval | ✅ | confirm + apply |
| Voting / debate | ❌ | multiplies cost on a local model; deterministic tests already decide |
| Sub-agents | ❌ | one task, one loop is enough |
| Skill library | 🟡 | could add a `SKILL.md` for "how to write a pytest reproduction" — and **measure** it (see L10) |

### Memory & context (L04)
| Concept | Status | In ReproAgent |
|---|---|---|
| Working memory | ✅ | the message list in the loop |
| Episodic memory | ✅ | retry feedback = what went wrong last attempt |
| Session persistence | ✅ | `results/sessions/S-XXXX.json` |
| Semantic / RAG | ❌ | search tool on the live repo beats a stale index for one repo |
| MemGPT / Mem0 | ❌ | overkill for a single bug session |

### MCP & Skills
| Concept | Status | Note |
|---|---|---|
| MCP server | 🟡 | Not used. Easy add: expose `search_code/read_file/run_pytest` as an MCP server so Claude Code/Cursor can use ReproAgent's tools |
| SKILL.md / progressive disclosure | 🟡 | Optional; only worth it if an ablation shows gain |

### Specs & architecture (L05, L06)
| Concept | Status | In ReproAgent |
|---|---|---|
| Spec-driven development | ✅ | `docs/spec.md` written first |
| EARS requirements | ✅ | FR-01…FR-10 in EARS form |
| AGENTS.md | ✅ | repo root, kept short ("a map, not a manual") |
| ADRs | ✅ | `docs/adr/0001–0005` |

### Evaluation lessons (L10, guest lecture "Dumber Agent")
| Lesson | What we do |
|---|---|
| A pass rate alone says nothing — report the **delta vs a baseline** | full vs `single_shot` vs `desc_only` modes |
| Quality and cost on the **same slide** | we log calls, tokens, seconds per run |
| Maybe the eval is broken, not the agent | fail-to-pass oracle catches tests that "fail" for the wrong reason; we found & fixed exit-code-4 mislabel |
| Context files are a hypothesis — test them | if we add a SKILL.md, run with/without |
| Measure model change and context change separately | keep model fixed (`qwen3:8b`) across modes |

### Quality attributes & sustainability
| Attribute | How |
|---|---|
| Reliability | deterministic verification, sandbox, busy-guard |
| Security | path confinement, no writes to user repo until Apply, localhost-only UI |
| Cost / energy | local SLM, no thinking tokens, tokens+time logged (SWEnergy: energy ∝ time × tokens) |
| Latency | budgets cap worst case |

---

## 5. Evaluation plan (agreed shape)

- **Dataset:** toy + BugsInPy (tqdm, thefuck, httpie), one venv per bug.
- **Modes:** `full` (agent + retries) · `single_shot` (one attempt) · `desc_only` (no repo tools).
- **Metrics:** reproduced? fail-to-pass? fixed? patch passes developer's test? + tokens, seconds.
- **Hypotheses:** H1 tools+loop beat single shot · H2 retries with feedback help · H3 harness-verified fixing works on an SLM.
- **Pilot so far (13/27 runs):** full agent reproduced most bugs, fixed toy + tqdm-4 (passes dev test); several "reproduced" tests failed the oracle → shows why the oracle matters.

---

## 6. Gaps & next steps (to decide together)

1. Finish the evaluation run (paused).
2. Optional: MCP server wrapper (shows MCP concept, small effort).
3. Optional: one `SKILL.md` + with/without ablation (shows L10 lesson).
4. Failure analysis section (why oracle failures happen).
5. Mid-submission report from this doc.
