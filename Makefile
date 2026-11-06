PY := .venv/bin/python

.PHONY: setup test ui demo bench eval summary

setup:            ## create venv and install
	uv venv && uv pip install -e .
test:             ## unit tests (no LLM needed)
	$(PY) -m pytest -q
ui:               ## chat UI on http://127.0.0.1:8765
	$(PY) -m reproagent.ui
demo:             ## run the agent on the toy bug
	$(PY) -m reproagent run --repo examples/toy_calc --report examples/toy_calc/ISSUE.md --fixed-repo examples/toy_calc_fixed
bench:            ## build BugsInPy repos and per-bug envs
	$(PY) benchmarks/setup_bugsinpy.py
eval:             ## full evaluation, all modes
	$(PY) -m reproagent eval --dataset benchmarks/dataset.json --modes full,single_shot,desc_only
summary:
	$(PY) benchmarks/summarize.py results/pilot
