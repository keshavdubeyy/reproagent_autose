"""Layer 4 strategy per model size: one patch loop, different settings for 4B, 8B and 14B models.

The approaches studied (mini-swe-agent, AutoSE-Labs/autose, context-engineering-v2) differ mainly
in how much they trust the model to track its own work. A profile picks those choices:
  context       how much report and code the prompt carries (bigger models get more)
  plan          write fix steps in words first (context-engineering-v2 fix_steps)
  build_on_best a partly-right attempt (the error is gone or the output now matches, nothing
                newly broken) becomes the starting code of the next attempt, which edits on top of
                it, as an agent editing files in place does (AutoSE-Labs, mini-swe-agent); otherwise
                every attempt starts from the original code
  atomic        refuse a whole answer if any edit in it is refused (AutoSE-Labs DR-007): needed
                once attempts build on each other, so a half-applied answer cannot become the base
Measured: slm4, on the 6-bug sympy pilot (3/6 verified; plan on gave 2/6, atomic on lost 13480).
slm8 and slm14 are the starting points for those sizes, to be checked on the same pilot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Profile:
    name: str
    max_params_b: float        # used for models up to this size (billions of parameters)
    num_ctx: int               # context window asked of Ollama
    num_gpu: int | None        # layers on the GPU (99 = all; None = let Ollama split GPU/CPU)
    max_attempts: int
    plan: bool
    build_on_best: bool
    atomic: bool
    report_chars: int
    diagnosis_chars: int
    code_chars: int
    full_class_chars: int      # a class longer than this is shown as an outline
    answer_tokens: int
    retry_temperature: float
    edit_mode: str = "json"      # "json": one structured answer per attempt; "tools": tool calls
    tool_steps: int = 12        # tool mode: model turns allowed
    fix_rules: bool = False     # add prompts.FIX_RULES (context-engineering-v2 fix guidelines) to the system prompt
    candidates: int = 1         # >1 (JSON mode): sample this many candidates, filter by tests, vote (sample.py)
    alt_locations: int = 2      # with candidates: how many go one each to the next suspect locations
    line_hints: bool = True     # show the located lines that ran when the report's example failed
    repro: bool = True          # add the reproduction stage's kept tests (repro.json) to the checks


PROFILES = {
    # 4B in 8K: each attempt is a fresh, complete fix of the original; no plan (it over-plans);
    # edits are applied as far as they go and the rest reported back.
    "slm4": Profile("slm4", 5, 8192, 99, 4, False, False, False, 2200, 1200, 7000, 3000, 2500, 0.7, "json"),
    # 8B: more context; a partly-right patch is kept and improved instead of rewritten.
    "slm8": Profile("slm8", 10, 12288, None, 4, False, True, True, 3000, 1600, 10000, 4500, 3000, 0.6, "tools", 14),
    # 14B: plans first, builds on its partial fixes, sees most of the code.
    "slm14": Profile("slm14", 20, 16384, None, 5, True, True, True, 4000, 2000, 14000, 6000, 4000, 0.5, "tools", 18),
}


def model_size_b(model: str) -> float | None:
    """qwen3:4b -> 4.0, qwen2.5-coder:14b-instruct -> 14.0, llama3.1:8b-q4 -> 8.0; None if no size."""
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![a-z])", model.lower().split("/")[-1])
    return float(m.group(1)) if m else None


def for_model(model: str, name: str = "auto", **overrides) -> Profile:
    """The named profile, or with "auto" the smallest profile that covers the model's size
    (slm4 when the size cannot be read from the name). Keyword overrides replace single fields."""
    if name != "auto":
        p = PROFILES[name]
    else:
        size = model_size_b(model)
        p = next((p for p in sorted(PROFILES.values(), key=lambda p: p.max_params_b)
                  if size is not None and size <= p.max_params_b), None)
        if p is None:
            p = PROFILES["slm4"] if size is None else max(PROFILES.values(), key=lambda p: p.max_params_b)
    return replace(p, **{k: v for k, v in overrides.items() if v is not None}) if overrides else p
