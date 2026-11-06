# Benchmarks

| File | Purpose |
|---|---|
| `dataset.json` | Bugs used in evaluation: buggy repo, fixed repo, report, interpreter, developer test |
| `reports/` | Hand-written natural-language bug report per bug (the agent's only input besides the repo) |
| `setup_bugsinpy.py` | Clones BugsInPy, builds buggy/fixed snapshots in `repos/` and one Python 3.8 venv per bug in `envs/`, and checks that the developer test is fail-to-pass |
| `summarize.py` | Turns result JSON files into the tables in the report |

`repos/` and `envs/` are generated (≈175 MB) and git-ignored. Rebuild them with:

```bash
python benchmarks/setup_bugsinpy.py
```
