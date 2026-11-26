"""Local chat model through Ollama, with native tool calling.

Replaces context-engineering-v2@1c65307 src/utils/llm.py (LangChain ChatOpenAI) and the
LangGraph ReAct agent in src/agents/common.py run_agent_with_tools. Defaults fit a 4 GB GPU:
qwen3:4b, 8K context, every layer on the GPU (Ollama otherwise splits it 55/45 with the CPU,
which is 4x slower), temperature 0, thinking off.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field

OLLAMA = "http://127.0.0.1:11434"  # not "localhost": on Windows that tries IPv6 first and waits 2 s


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
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0})

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None,
             fmt: str | dict | None = None) -> dict:
        """One model turn -> {"content": str, "tool_calls": [{"name", "arguments"}]}. fmt="json" forces JSON."""
        predict = (max_tokens or self.max_tokens) + (self.think_tokens if self.think else 0)
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
        u = self.usage
        u["calls"] += 1
        u["prompt_tokens"] += d.get("prompt_eval_count", 0)
        u["output_tokens"] += d.get("eval_count", 0)
        u["seconds"] = round(u["seconds"] + time.time() - t, 1)
        msg = d.get("message", {})
        calls = [{"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}
                 for c in msg.get("tool_calls") or []]
        return {"content": msg.get("content", "") or "", "tool_calls": calls,
                "prompt_tokens": d.get("prompt_eval_count", 0)}
