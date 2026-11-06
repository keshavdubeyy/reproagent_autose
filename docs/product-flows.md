# ReproAgent: Product Flows

ReproAgent is a local, chat-style assistant. A developer connects a Python repository, gives it a bug report, and
gets back (1) a failing test that proves the bug and (2) a verified fix, which they choose to apply or not.
The model runs locally (Ollama) and nothing leaves the machine. The agent never writes to the user's repository
without an explicit "Apply".

## Principles
1. **Evidence over claims.** Every success shown to the user has been checked by the harness, and the evidence is one click away (pytest output, the diff, the checks).
2. **The user stays in control at the two moments that matter:** confirming what the bug *is*, and accepting a change to their code.
3. **Honest outcomes.** "Not reproduced", "needs more info" and "environment problem" are first-class results with a next step, not errors.
4. **Show the route, hide the noise.** Stage progress is always visible. Tool-level steps are collapsed by default.

## Flow 0: Connect a workspace
1. The user opens ReproAgent (`python -m reproagent.ui`) and clicks **Connect repository**.
2. They enter a local folder path. The **Try a benchmark bug** chips fill in a repo, a report and an interpreter for demos.
3. ReproAgent inspects the folder and shows a **workspace card**:
   - **Project:** name, number of Python files, test folder found or not, git branch.
   - **Interpreter:** auto-detects `.venv`/`venv`, otherwise uses the current Python, and can be overridden.
   - **Health check:** pytest runs a trivial test in this environment.
4. **Outcomes:**
   - *ready* → the composer unlocks.
   - *problem* → a red card with the exact error and a suggested fix (wrong interpreter, missing pytest). Bad environments never get blamed on the agent.

## Flow 1: Give the bug
The user types into the composer, or:
- **pastes a GitHub issue URL**, which ReproAgent fetches (title and body only, after the user asks), or
- **attaches** a `.md`/`.txt` file.

## Flow 2: Intake (understanding the report)
1. The model turns the report into an **understanding card**:
   - title
   - observed behaviour
   - expected behaviour
   - steps or inputs
   - code entities mentioned (functions, modules)
   - missing information
2. **Actions:** **Looks right, reproduce it** · **Edit** (the user corrects any field) · **Add details** (reply in chat).
3. If key information is missing (no expected behaviour, no entry point), the card says so. The user can still continue.

## Flow 3: Reproduce
1. The route bar shows **Understand → Reproduce → Verify failure → Fix → Verify fix**.
2. The live activity feed shows searches, file reads, test writes and test runs, collapsed into one line each.
3. **Outcomes, each a card:**
   - **Reproduced:** the test code, plus evidence that it fails on the current code for a valid reason (an assertion or the expected exception, not an import error). Actions: **Fix it** · **Save test to repo** · **Stop here**.
   - **Not reproduced:** what was tried and why each attempt was rejected. Actions: **Add details and retry** · **Show attempts**.
   - **Needs info:** the agent's specific question, answered in the composer, which triggers a retry.

## Flow 4: Fix
1. The agent edits a sandboxed copy and is not allowed to edit the reproduction test.
2. **Fixed card:**
   - **Diff:** a unified diff with the number of files and lines changed.
   - **Checks:** ✓ reproduction test now passes · ✓ no previously passing test fails (N tests) · attempts used.
   - **Actions:** **Apply to repository** (writes the patch and adds the test, after a confirm step; refused if the files changed meanwhile) · **Download patch** · **Discard**.
3. **Not fixed card:** the best attempt's diff and the failing check. Actions: **Retry with a hint** · **Keep the test only**.

## Flow 5: Sessions and history
- **Sessions:** each bug is a session in the left sidebar, with a status dot (working, waiting for you, done, failed). Sessions survive a page reload.
- **Needs you:** the count of sessions waiting on a decision.
- **Evaluation:** benchmark results for the research side (modes, fail-to-pass, fix rate).

## States per session
`connecting → understanding → awaiting_confirm → reproducing → (reproduced | not_reproduced | needs_info | env_error)
 → awaiting_fix_decision → fixing → (fixed | not_fixed) → (applied | discarded)`
