# Plan for pushing to GitHub (not done yet)

1. Create a **private** repo `reproagent` under one team member's account; add the other two as collaborators (and the TA/instructor if required).
2. Check nothing heavy or private is tracked: `.venv/`, `benchmarks/envs/` (118 MB), `benchmarks/repos/` (57 MB) and `results/sessions/` are git-ignored; `results/pilot/` (~320 KB) is kept as evaluation evidence.
3. Rebuild instructions live in the README (`benchmarks/setup_bugsinpy.py` recreates repos and envs).
4. Push the existing history as is — the commits are already meaningful steps (prototype → benchmark → hardening → UI → docs), which the mid-submission asks for.
5. From then on: one branch per piece of work, short PRs, so every teammate has commits.
6. Tag `mid-submission` at the commit we submit.

Commands (when we decide to push):
```bash
git remote add origin git@github.com:<owner>/reproagent.git
git push -u origin master
git tag mid-submission && git push origin mid-submission
```
