"""Generic tool-calling loop used by both the Reproduce and the Fix stage."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from .llm import LLM


def tool(name: str, desc: str, optional: tuple = (), **params: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": "string", "description": v} for k, v in params.items()},
                "required": [p for p in params if p not in optional],
            },
        },
    }


@dataclass
class Trace:
    steps: list[dict] = field(default_factory=list)
    on_step: object = None  # optional callback(step) for live UIs

    def add(self, kind: str, **kw):
        step = {"kind": kind, "t": round(time.time(), 2), **kw}
        self.steps.append(step)
        if self.on_step:
            self.on_step(step)


class AgentLoop:
    """Runs the model with a tool registry until it calls `finish` or the step budget runs out."""

    def __init__(self, llm: LLM, tools: list[dict], handlers: dict[str, Callable[..., str]],
                 max_steps: int = 15, trace: Trace | None = None):
        self.llm, self.tools, self.handlers = llm, tools, handlers
        self.max_steps = max_steps
        self.trace = trace or Trace()

    def run(self, system: str, user: str) -> dict | None:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(self.max_steps):
            msg = self.llm.chat(msgs, self.tools)
            msgs.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                self.trace.add("text", content=msg.get("content", "")[:1000])
                msgs.append({"role": "user", "content": "Use a tool. Call `finish` when you are done."})
                continue
            for c in calls:
                name = c["function"]["name"]
                args = c["function"].get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if name == "finish":
                    self.trace.add("finish", args=args)
                    return args
                fn = self.handlers.get(name)
                try:
                    result = fn(**args) if fn else f"ERROR: unknown tool {name}"
                except Exception as e:  # tool errors are observations, not crashes
                    result = f"ERROR: {type(e).__name__}: {e}"
                self.trace.add("tool", name=name, args={k: str(v)[:300] for k, v in args.items()},
                               result=str(result)[:1500])
                msgs.append({"role": "tool", "content": str(result)[:6000], "tool_name": name})
        self.trace.add("budget_exhausted")
        return None
