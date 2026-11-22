"""Loop controller: failure fingerprints decide whether to keep going, switch strategy, or stop.

A fingerprint is (stage, outcome category, exception type, failure line with numbers / quoted text /
addresses removed). The same fingerprint again means the last feedback did not help:

  seen twice in a row  -> "switch": tell the model it is repeating itself and must change approach
                          (the repair loop also moves to the next localised candidate)
  seen three times     -> "stop": further attempts would repeat the same failure; save the budget

Deterministic Python, no model involved (architecture rule 6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def fingerprint(stage: str, category: str, exc: str | None, line: str | None) -> str:
    t = (line or "").lower()
    t = re.sub(r"0x[0-9a-f]+", "A", t)
    t = re.sub(r"(['\"]).*?\1", "S", t)
    t = re.sub(r"\d+(\.\d+)?", "N", t)
    t = re.sub(r"\s+", " ", t).strip()[:160]
    return f"{stage}|{category}|{exc or '-'}|{t}"


@dataclass
class Decision:
    action: str            # continue | switch | stop
    message: str = ""
    fingerprint: str = ""


@dataclass
class LoopController:
    switch_after: int = 2
    stop_after: int = 3
    history: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)

    def observe(self, stage: str, category: str, exc: str | None, line: str | None) -> Decision:
        fp = fingerprint(stage, category, exc, line)
        self.history.append(fp)
        run = 0
        for x in reversed(self.history):
            if x != fp:
                break
            run += 1
        if run >= self.stop_after:
            d = Decision("stop", f"the same failure happened {run} times in a row; stopping this loop", fp)
        elif run >= self.switch_after:
            d = Decision("switch", ("You produced exactly the same failure as last time, so the previous "
                                    "approach does not work. Use a clearly different approach."), fp)
        else:
            d = Decision("continue", "", fp)
        self.log.append({"stage": stage, "fingerprint": fp, "action": d.action})
        return d

    @property
    def stops(self) -> int:
        return sum(1 for e in self.log if e["action"] == "stop")

    @property
    def switches(self) -> int:
        return sum(1 for e in self.log if e["action"] == "switch")
