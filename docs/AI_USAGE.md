# AI usage disclosure (draft, for the team to check)

This project uses AI in two ways. One is inside the system we built. The other is in how we built it.

## 1. AI inside the system

- **Model.** The agent's language model is **Qwen3-8B**, served locally by Ollama with thinking turned off. No hosted AI service is called when the system runs.
- **What the model does.** It has four narrow roles:
  1. Turning a bug report into a spec.
  2. Writing the reproduction test.
  3. Writing the patch.
  4. An optional patch review (`--review slm`), plus a judge that compares error messages.
- **What stays deterministic Python.** Control flow, search, fault localisation, testing, the review rules, the blackboard and every evaluation step.
- **What the model never sees.** The hidden ground truth: the developers' fix and the hidden tests.

## 2. AI used while building the project

- **Tool.** We used Claude Code (Anthropic) as a coding assistant during development.
- **What we used it for:**
  - writing and refactoring code and unit tests
  - building the evaluation scripts (localisation, pipeline health, quality scorecard, night runner)
  - debugging Docker and SWE-bench issues
  - drafting the progress pages and documentation
- **Who decided what.** Design decisions are recorded as ADRs in `docs/adr/`. The team made them and reviewed them.
- **How we checked the assistant's code:**
  - 90+ unit tests
  - the quality scorecard (`python -m autose.evaluation.quality`)
  - official SWE-bench grading
- **Errors these checks caught, including the assistant's own:**
  - The localisation scorer picked the wrong function for 14 of 200 bugs. This was found by the quality check and fixed on 22 Nov.
  - A guard that was too loose.
  - A regression check that ran unrelated tests.
- **The paper.** TODO (team): state which parts of the paper text were drafted or edited with AI help, and who checked them.

## 3. What we verified ourselves

- **Results come from our runs.** Every number in the paper is produced by scripts in this repository from run logs in `runs/` and `results/`. Each table names the command that produced it.
- **The benchmark fix stays hidden.** It is never committed, and the agent's containers carry no git history after the buggy commit (ADR-0009).
