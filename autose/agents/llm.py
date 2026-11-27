"""Local chat model through Ollama, with native tool calling.

Replaces context-engineering-v2@1c65307 src/utils/llm.py (LangChain ChatOpenAI) and the
LangGraph ReAct agent in src/agents/common.py run_agent_with_tools. Defaults fit a 4 GB GPU:
qwen3:4b, 8K context, every layer on the GPU (Ollama otherwise splits it 55/45 with the CPU,
which is 4x slower), temperature 0, thinking off.

Two guards, after AutoSE-Labs/autose DR-022 and DR-016:
  context   Ollama does not refuse a prompt longer than num_ctx: it silently drops its start,
            which is where the system prompt and the bug report are (Layer 3 runs had calls at
            8185 of 8192 tokens). Before each call the prompt is measured; if it plus the answer
            would not fit, old messages in the middle (tool output first) are shortened, never
            the system prompt, the task or the latest turn; then the answer budget shrinks. After
            the call, Ollama's own count shows whether it trimmed anyway. All of it is counted
            in usage (overflow_risk, shrunk, trimmed).
  reasoning qwen3 can reason even with thinking off (more so with tools). Any <think> text is cut
            out of the answer, so it is never sent back in the history, and counted
            (thinking_chars).
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field

OLLAMA = "http://127.0.0.1:11434"  # not "localhost": on Windows that tries IPv6 first and waits 2 s
CHARS_PER_TOKEN = 3.0   # conservative for code (qwen3 averages about 3.3 on Python source)
PER_MESSAGE_TOKENS = 8  # chat template overhead per message
SAFETY_TOKENS = 64
MIN_ANSWER_TOKENS = 512
KEEP_HEAD = 200
THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


def estimate_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    chars = sum(len(m.get("content") or "") + len(json.dumps(m["tool_calls"])) if m.get("tool_calls")
                else len(m.get("content") or "") for m in messages)
    chars += len(json.dumps(tools)) if tools else 0
    return int(chars / CHARS_PER_TOKEN) + PER_MESSAGE_TOKENS * len(messages)


def strip_reasoning(text: str) -> tuple[str, int]:
    """(the answer without <think> blocks, characters removed). An unclosed block runs to the end."""
    if "<think>" not in text:
        return text, 0
    out = THINK.sub("", text)
    return out.strip(), len(text) - len(out)


def fit(messages: list[dict], tools: list[dict] | None, num_ctx: int, predict: int) -> tuple[list[dict], int, bool]:
    """Messages and answer budget that fit num_ctx. Shortens messages between the task (the first
    user message) and the last two, oldest first; keeps the system prompt and the task whole."""
    budget = num_ctx - predict - SAFETY_TOKENS
    if estimate_tokens(messages, tools) <= budget:
        return messages, predict, False
    out = [dict(m) for m in messages]
    first_user = next((i for i, m in enumerate(out) if m["role"] == "user"), 0)
    middle = [i for i in range(first_user + 1, max(first_user + 1, len(out) - 2))]
    for keep in (KEEP_HEAD, 0):
        for i in sorted(middle, key=lambda i: (out[i]["role"] != "tool", i)):  # tool output first, oldest first
            c = out[i].get("content") or ""
            if len(c) > keep + 40:
                out[i]["content"] = (c[:keep] + " [... removed: the context was full]") if keep else "[removed]"
                if estimate_tokens(out, tools) <= budget:
                    return out, predict, True
    est = estimate_tokens(out, tools)
    predict = max(MIN_ANSWER_TOKENS, min(predict, num_ctx - est - SAFETY_TOKENS))
    return out, predict, True


@dataclass
class LLM:
    model: str = "qwen3:4b"
    num_ctx: int = 8192
    num_gpu: int | None = 99
    temperature: float = 0.0
    think: bool = False
    max_tokens: int = 1800
    think_tokens: int = 1500  # extra output budget for the hidden reasoning when think is on
    url: str = OLLAMA
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0,
                                                 "overflow_risk": 0, "shrunk": 0, "trimmed": 0, "thinking_chars": 0})

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None,
             fmt: str | dict | None = None) -> dict:
        """One model turn -> {"content": str, "tool_calls": [{"name", "arguments"}], "prompt_tokens",
        "trimmed"}. fmt="json" forces JSON (or a JSON schema)."""
        predict = (max_tokens or self.max_tokens) + (self.think_tokens if self.think else 0)
        u = self.usage
        for k in ("overflow_risk", "shrunk", "trimmed", "thinking_chars"):
            u.setdefault(k, 0)
        messages, predict, shrunk = fit(messages, tools, self.num_ctx, predict)
        if shrunk:
            u["overflow_risk"] += 1
            u["shrunk"] += 1
        opts = {"num_ctx": self.num_ctx, "temperature": self.temperature, "num_predict": predict}
        if self.num_gpu is not None:
            opts["num_gpu"] = self.num_gpu
        body = {"model": self.model, "messages": messages, "stream": False, "think": self.think, "options": opts}
        if tools:
            body["tools"] = tools
        if fmt:
            body["format"] = fmt
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        t = time.time()
        with urllib.request.urlopen(req, timeout=1800) as r:
            d = json.load(r)
        pt = d.get("prompt_eval_count", 0)
        u["calls"] += 1
        u["prompt_tokens"] += pt
        u["output_tokens"] += d.get("eval_count", 0)
        u["seconds"] = round(u["seconds"] + time.time() - t, 1)
        # Ollama counts only the prompt tokens it evaluated (cached ones are not counted), so this
        # catches trimming when it shows, not always
        trimmed = pt >= self.num_ctx - 8
        if trimmed:
            u["trimmed"] += 1
        msg = d.get("message", {})
        content, removed = strip_reasoning(msg.get("content", "") or "")
        u["thinking_chars"] += removed + len(msg.get("thinking") or "")
        calls = [{"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}
                 for c in msg.get("tool_calls") or []]
        return {"content": content, "tool_calls": calls, "prompt_tokens": pt, "trimmed": trimmed}
