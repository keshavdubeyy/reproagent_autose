"""LLM client guards: the prompt is made to fit the context window, reasoning is stripped."""
import io
import json

from autose.agents import llm as llm_mod
from autose.agents.llm import LLM, estimate_tokens, fit, strip_reasoning


class FakeOllama:
    def __init__(self, reply, prompt_eval=100):
        self.reply, self.prompt_eval, self.bodies = reply, prompt_eval, []

    def __call__(self, req, timeout=None):
        self.bodies.append(json.loads(req.data))
        d = {"message": self.reply, "prompt_eval_count": self.prompt_eval, "eval_count": 10}
        return io.BytesIO(json.dumps(d).encode())


def test_reasoning_is_cut_out_and_counted(monkeypatch):
    fake = FakeOllama({"content": "<think>long musing about the bug</think>\n{\"a\": 1}", "thinking": "more"})
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake)
    m = LLM()
    r = m.chat([{"role": "user", "content": "hi"}])
    assert r["content"] == '{"a": 1}' and m.usage["thinking_chars"] == len("<think>long musing about the bug</think>") + 4
    assert strip_reasoning("<think>cut off before the end") == ("", len("<think>cut off before the end"))
    assert strip_reasoning("plain") == ("plain", 0)


def test_long_conversation_is_shortened_but_not_its_start_or_latest_turn(monkeypatch):
    system = {"role": "system", "content": "RULES " * 50}
    task = {"role": "user", "content": "BUG REPORT " * 300}
    old = [{"role": "tool", "content": "old tool output " * 400, "tool_name": "view_code"} for _ in range(4)]
    latest = [{"role": "assistant", "content": "checking"}, {"role": "tool", "content": "LATEST " * 300}]
    msgs = [system, task] + old + latest
    assert estimate_tokens(msgs) + 2000 > 8192
    out, predict, shrunk = fit(msgs, None, 8192, 2000)
    assert shrunk and estimate_tokens(out) + predict <= 8192
    assert out[0] == system and out[1] == task and out[-2:] == latest       # kept whole
    assert any("removed: the context was full" in m["content"] for m in out[2:6])
    assert msgs[2]["content"].startswith("old tool output")                 # the caller's list is not changed
    fake = FakeOllama({"content": "ok"}, prompt_eval=8190)                  # Ollama says the window was full
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake)
    m = LLM()
    r = m.chat(msgs, max_tokens=2000)
    assert r["trimmed"] and m.usage["trimmed"] == 1 and m.usage["shrunk"] == 1
    sent = fake.bodies[0]
    assert sent["messages"][1] == task and sent["options"]["num_predict"] <= 2000


def test_a_short_prompt_is_sent_unchanged(monkeypatch):
    fake = FakeOllama({"content": "ok", "tool_calls": [{"function": {"name": "run_check", "arguments": {}}}]})
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake)
    m = LLM()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    r = m.chat(msgs, max_tokens=500)
    assert fake.bodies[0]["messages"] == msgs and fake.bodies[0]["options"]["num_predict"] == 500
    assert r["tool_calls"] == [{"name": "run_check", "arguments": {}}] and not r["trimmed"]
    assert m.usage["shrunk"] == 0 and m.usage["overflow_risk"] == 0
