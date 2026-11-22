"""Thin client for a locally running Small Language Model served by Ollama.

No hosted AI service is used. Default model: qwen2.5-coder:3b (~1.9 GB, Q4) which fits
entirely in a 4 GB GPU / 8 GB RAM laptop. Override with REPROAGENT_MODEL.
"""
from __future__ import annotations

import ast
import json
import os
import re
import time

import requests

DEFAULT_MODEL = os.environ.get("AUTOSE_MODEL", os.environ.get("REPROAGENT_MODEL", "qwen2.5-coder:3b"))
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
LLM_TIMEOUT_S = int(os.environ.get("AUTOSE_LLM_TIMEOUT", "600"))


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, temperature: float = 0.2,
                 num_ctx: int = 6144, seed: int = 7):
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.seed = seed
        self.calls = 0
        self.total_time = 0.0

    def chat(self, system: str, user: str, json_mode: bool = False,
             temperature: float | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_ctx": self.num_ctx,
                "seed": self.seed,
                "num_predict": 1536,   # guard against runaway generations (one hit the 600 s timeout)
            },
        }
        if json_mode:
            payload["format"] = "json"
        if self.model.startswith("qwen3"):
            payload["think"] = False   # qwen3 "thinking" spends minutes per call and eats num_predict
        t0 = time.time()
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=LLM_TIMEOUT_S)
        r.raise_for_status()
        self.calls += 1
        self.total_time += time.time() - t0
        return r.json()["message"]["content"]

    def chat_json(self, system: str, user: str) -> dict:
        raw = self.chat(system, user, json_mode=True)
        return parse_json(raw)


def parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def is_cut_off(raw: str) -> bool:
    """The answer opened a code block that never closes: the model hit its output-length limit."""
    return raw.count("```") % 2 == 1


def extract_code(raw: str) -> str:
    """Pull the python code block out of an LLM answer. A block cut off by the length limit has no
    closing fence: take what follows the opening fence and drop the unfinished tail, keeping the
    longest prefix that is valid Python (so pytest can at least collect it)."""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    m = re.search(r"```(?:python|py)?\s*\n", raw)
    code = raw[m.end():] if m else raw
    lines = code.strip().splitlines()
    for end in range(len(lines), 0, -1):
        part = "\n".join(lines[:end])
        try:
            ast.parse(part)
            return part.rstrip() + "\n"
        except SyntaxError:
            continue
    return code.strip() + "\n"
