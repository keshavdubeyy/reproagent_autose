# Contributing

## Workflow
1. Pull the latest `main`.
2. Create a branch: `feat/<thing>`, `fix/<thing>`, `docs/<thing>` or `eval/<thing>`.
3. Make small, focused commits using [Conventional Commits](https://www.conventionalcommits.org/), for example `feat(pipeline): add fix stage retries`.
4. Run `make test`. Unit tests must pass and must not need an LLM.
5. Open a PR using the template, and get **one teammate's review** before merging.
6. Squash-merge, then delete the branch.

## Rules
- `reproagent/` uses only the standard library plus pytest. No hosted LLM SDKs: the model runs locally through Ollama.
- The harness decides whether a stage succeeded. Never trust the model's claim of success.
- Agent file access goes through `Workspace`. Only an approved Apply writes to a user's repo.
- Record any design change as a new ADR in `docs/adr/`.
- Never commit generated benchmark data (`benchmarks/repos/`, `benchmarks/envs/`), virtual environments or UI sessions.
