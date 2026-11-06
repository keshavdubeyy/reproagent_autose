# ReproAgent Specification

## Goal
Given a natural-language bug report and a Python repository, produce (1) an executable test that reproduces the reported failure and (2) a minimal patch that makes the test pass without regressions, with evidence for both. Everything runs on a local small language model.

## Functional requirements (EARS)
- **FR-01** WHEN a bug report and repository path are supplied, the system SHALL create an isolated copy of the repository and never modify the original.
- **FR-02** WHEN reproducing, the system SHALL let the agent search and read project code and write and run a single pytest file.
- **FR-03** WHEN the generated test passes, or fails because of ImportError, SyntaxError, NameError, a missing fixture or a collection error, the system SHALL reject it as a reproduction and SHALL return the pytest output to the agent as feedback.
- **FR-04** WHILE the reproduction attempt budget (default 3) is not exhausted, the system SHALL retry with feedback.
- **FR-05** IF the agent declares that the report lacks the information needed, THEN the system SHALL report `needs_info` with the agent's explanation.
- **FR-06** WHEN a valid reproduction exists, the system SHALL run a Fix stage in which the agent may edit source files but SHALL NOT edit the reproduction test.
- **FR-07** The system SHALL accept a fix only if the reproduction test passes AND the repository's pre-existing test suite passes. Otherwise it SHALL revert the edits and retry within the fix budget (default 3).
- **FR-08** WHEN a known-fixed version is supplied, the system SHALL run the generated test against it and record fail-to-pass validity.
- **FR-09** The system SHALL output a Markdown and a JSON report containing status, test code, pytest evidence, patch, attempts, wall time, LLM calls and tokens, and the full tool trace.
- **FR-10** The system SHALL support the baseline modes `desc_only` and `single_shot` for evaluation.

## Non-functional requirements
- **NFR-01 Local:** no hosted AI service. The default model is `qwen3:8b` via Ollama.
- **NFR-02 Safety:** every agent file path stays inside the workspace, every test run has a timeout, and test runs use a scrubbed environment.
- **NFR-03 Cost transparency:** every run records LLM calls, tokens and wall time.
- **NFR-04 Reproducibility:** fixed budgets and temperature, and saved traces.

## Out of scope
Non-Python projects, bugs that need network, a GUI or external services, multi-repository bugs, and opening PRs.
