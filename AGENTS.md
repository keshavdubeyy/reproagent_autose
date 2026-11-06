# AGENTS.md

Guidance for coding agents working on this repository.

- Python ≥3.11, stdlib only in `reproagent/` apart from pytest. Do not add hosted-LLM SDKs: the model must run locally through Ollama.
- The harness decides whether a stage succeeded. Never accept a model's claim of success without running the check in `pipeline.py`.
- All file access by the agent goes through `Workspace`, which confines paths to the temporary copy. Only `Session.apply` writes to the user's repo, and only after the human approves.
- Run `.venv/bin/python -m pytest` before committing. Unit tests must not need an LLM.
- Record design changes as a new ADR in `docs/adr/`.
