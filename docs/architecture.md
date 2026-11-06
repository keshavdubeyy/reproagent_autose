# Architecture

How ReproAgent is built and how the AI fits in. Diagrams are Mermaid (they render on GitHub).
For *why* each decision was made, see [docs/adr/](adr/). For the lecture-concept mapping, see [design-overview.md](design-overview.md).

**Core idea: Agent = model + harness.** The model (`qwen3:8b`, local via Ollama) is frozen. Everything we built is the harness around it: tools, sandbox, budgets, retries, verification, human approval.

---

## 1. Layers (component view)

```mermaid
flowchart TB
  subgraph UI["Interface layer"]
    WEB["Chat UI<br/>static/index.html"]
    SRV["HTTP server<br/>ui.py (stdlib, 127.0.0.1)"]
    CLI["CLI<br/>cli.py (run / eval)"]
  end
  subgraph APP["Application layer"]
    SES["Session<br/>session.py<br/>state machine + persistence"]
    PIPE["ReproAgent pipeline<br/>pipeline.py<br/>Reproduce → Fix + harness checks"]
  end
  subgraph AGENT["Agent layer"]
    LOOP["AgentLoop<br/>agent.py (ReAct, step budget, trace)"]
    LLM["LLM client<br/>llm.py → Ollama /api/chat"]
  end
  subgraph ENV["Execution layer"]
    WS["Workspace<br/>workspace.py<br/>temp-copy sandbox, path-confined tools, pytest runner"]
  end
  MODEL[("qwen3:8b<br/>local Ollama")]
  REPO[("User repo<br/>(read-only until Apply)")]

  WEB <--> SRV --> SES --> PIPE
  CLI --> PIPE
  PIPE --> LOOP --> LLM --> MODEL
  LOOP -- tool calls --> WS
  PIPE -- checks --> WS
  WS -- copies --> REPO
  SES -- Apply (human-approved) --> REPO
```

| Module | Responsibility | Uses AI? |
|---|---|---|
| `llm.py` | Talk to Ollama; count calls, tokens, seconds | is the AI boundary |
| `agent.py` | Generic tool-calling loop: model picks tool → we run it → result back | orchestrates AI |
| `workspace.py` | Sandbox copy; `list_files`, `search_code`, `read_file`, `write_test`, `edit_file`, `run_pytest`, `revert`, `diff` | no |
| `pipeline.py` | Stage order, prompts, retries, all pass/fail decisions | calls AI; **decides without AI** |
| `session.py` | Intake (report → structured bug), session states, Apply | one AI call (intake) |
| `ui.py`, `cli.py` | Entry points | no |

---

## 2. End-to-end flow (where AI is used)

Legend: 🤖 = LLM call · ⚙️ = deterministic code · 🧑 = human

```mermaid
flowchart TD
  A["🧑 Connect repo"] --> B["⚙️ Inspect: venv detect + health check"]
  B --> C["🧑 Bug report: text / .md / GitHub URL"]
  C --> D["⚙️ Fetch issue (if URL)"]
  D --> E["🤖 Intake: report → JSON<br/>title, observed, expected, steps, missing"]
  E --> F{"🧑 Confirm / edit"}
  F --> G["🤖 Reproduce loop<br/>explore code, write test, run it"]
  G --> H{"⚙️ classify_failure<br/>pytest exit 1 for a real reason?"}
  H -- "no (import/syntax error, passes)" --> I{"retries left?"}
  I -- yes, with feedback --> G
  I -- no --> X1["not reproduced / needs info"]
  H -- yes --> J{"🧑 Fix it / Save test / Stop"}
  J -- fix --> K["⚙️ Baseline: which tests already fail"]
  K --> L["🤖 Fix loop<br/>read code, edit source (tests read-only)"]
  L --> M{"⚙️ Verify<br/>repro test passes AND no new failures"}
  M -- no --> N{"retries left?"}
  N -- "yes: revert + feedback" --> L
  N -- no --> X2["not fixed (hint & retry possible)"]
  M -- yes --> O{"🧑 Apply / Download / Discard"}
  O -- apply --> P["⚙️ Check repo unchanged → write test + patch"]
```

Only **three boxes are AI**. Every diamond (decision) is code or a human — the model never grades itself.

---

## 3. Inside one agent loop (ReAct)

```mermaid
sequenceDiagram
  participant P as Pipeline (harness)
  participant L as AgentLoop
  participant M as LLM (qwen3:8b)
  participant W as Workspace (sandbox)
  P->>L: system prompt + bug + tools + budget (15 steps)
  loop until finish or budget
    L->>M: messages + tool schemas
    M-->>L: tool call e.g. search_code("median")
    L->>W: execute (path-confined, ≤50 hits)
    W-->>L: result or error text
    L->>L: append observation (errors are observations, not crashes)
  end
  M-->>L: finish(summary)
  L-->>P: trace
  P->>W: run_pytest — harness decides pass/fail
```

Nested loops (lecture L07): **tool step** (≤15) → **attempt** (≤3 per stage, revert between) → **stage** (reproduce, fix) → **human** (confirm, apply).

---

## 4. Session state machine

```mermaid
stateDiagram-v2
  [*] --> awaiting_report
  awaiting_report --> understanding: report submitted
  understanding --> awaiting_confirm
  awaiting_confirm --> reproducing: confirmed
  reproducing --> awaiting_fix_decision: reproduced
  reproducing --> needs_info
  reproducing --> not_reproduced
  reproducing --> env_error
  needs_info --> understanding: more details
  awaiting_fix_decision --> fixing: Fix it
  awaiting_fix_decision --> test_saved: Save test
  fixing --> awaiting_apply: verified
  fixing --> not_fixed
  not_fixed --> fixing: retry with hint
  awaiting_apply --> applied
  awaiting_apply --> discarded
```

Every action is rejected while the session is busy (`_guard`), and sessions persist to `results/sessions/S-XXXX.json`.

---

## 5. Main classes

```mermaid
classDiagram
  class LLM { +model +temperature +stats +chat(messages, tools, format) }
  class AgentLoop { +tools +handlers +max_steps +run(system, user) }
  class Trace { +steps +on_step }
  class Workspace {
    +root +python
    +list_files() +search_code() +read_file()
    +write_test() +edit_file() +replace_lines()
    +run_pytest() +failing_tests() +revert() +diff() +preflight()
  }
  class ReproAgent {
    +mode: full|single_shot|desc_only
    +run(repo, report, ...) Result
    -_reproduce() -_fix() -_oracle() -_gt_check()
  }
  class Session { +submit_report() +confirm() +fix(hint) +apply() +discard() }
  class SessionStore { +create() +get() +list() +events() }
  ReproAgent --> LLM
  ReproAgent --> AgentLoop
  AgentLoop --> Workspace
  AgentLoop --> Trace
  Session --> ReproAgent
  Session --> Workspace
  SessionStore --> Session
```

---

## 6. Safety boundaries

| Risk | Guard |
|---|---|
| Agent edits user files | Works on a temp copy; writes to the real repo only on human **Apply**, and only if the repo hasn't changed since the session started |
| Agent escapes the folder | All paths resolved and confined to the sandbox root |
| Agent "fixes" by editing tests | Test files are read-only in the Fix stage |
| Fake reproduction (import error, assert-the-bug) | `classify_failure` (exit 1 only) + fail-to-pass oracle in evaluation |
| Runaway loops | Step budget, retry budget, pytest timeout |
| Data leaving the machine | Local model only; UI bound to 127.0.0.1 |

---

## 7. Evaluation harness

`cli.py eval` runs each bug in `benchmarks/dataset.json` in three modes (`full`, `single_shot`, `desc_only`) with the same model. Per run it records: reproduced, fail-to-pass on the fixed version, fixed, patch passes the developer's test, LLM calls, tokens, seconds. `benchmarks/summarize.py` turns the JSON into tables.
