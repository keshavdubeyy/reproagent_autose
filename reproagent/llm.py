"""Minimal Ollama chat client (stdlib only, no hosted AI service)."""
from __future__ import annotations

import json
import os
import time
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("REPRO_MODEL", "qwen3:8b")


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, temperature: float = 0.2, timeout: int = 600):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}

    def chat(self, messages: list[dict], tools: list[dict] | None = None, format: str | None = None) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": self.temperature, "num_ctx": 16384},
        }
        if format:
            body["format"] = format
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        self.stats["calls"] += 1
        self.stats["seconds"] += time.time() - t0
        self.stats["prompt_tokens"] += data.get("prompt_eval_count", 0)
        self.stats["completion_tokens"] += data.get("eval_count", 0)
        return data["message"]
