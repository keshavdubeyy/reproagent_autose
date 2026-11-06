# ADR-0001: Local SLM via Ollama
**Status:** Accepted

**Context:** The course encourages local models. Our research question concerns small, locally running agents.

**Decision:** Use Ollama's `/api/chat` with native tool calling. The default model is `qwen3:8b` with thinking disabled and temperature 0.2. The model can be swapped with `--model` or `REPRO_MODEL`.

**Consequences:** Zero API cost and private code, but roughly 10–40 s per call on a laptop and weaker tool use than hosted models. The harness must compensate with strict checks and short, focused tool outputs.
