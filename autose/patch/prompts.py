"""Prompts for the Layer 4 patcher (qwen3:4b, 8K context): one short system prompt, one task
prompt with the report, Layer 3's diagnosis and the located code, and a feedback prompt after
each checked attempt. Answers are JSON (enforced by the schema in loop.py): free text let the
model reason past its token limit and quote code fragments that looked like edits. Each attempt
rewrites the code from the original, so the model never reasons about its own earlier diff."""

SYSTEM = """You fix bugs in a Python repository by editing the code you are shown.
Answer in JSON with:
- problem: one sentence, what exactly is wrong in the code
- fix: one or two sentences, what you change
- edits: a list; each edit has function, search and code:
  * change a few lines (the usual case): function = its name, search = the exact existing lines
    to change, copied from the code shown (one to five lines, the lines themselves, not the
    def line), code = the new lines that replace them
  * restructure a whole function: function = its name, search = "", code = the complete new
    function from its def line (same name)
  * add a new method to the class shown: function = "(new method)", search = "", code = the def
  * add a new helper function: function = "(new function)", search = "", code = the def
Rules: change as little as possible; Python 3.9; import inside the function what it newly needs;
no tests, prints or example code; do not rename existing functions."""
# AUTOSE: small exact replacements first. On the 6-bug pilot every edit that fixed a bug was a
# one-to-three-line replacement; whole-function rewrites applied but fixed none.

TASK = """Bug report:
{report}

Diagnosis from the code investigation (usually right about where, may be wrong about details):
{diagnosis}

Code you may edit:
{code}

Fix the bug."""

# Condensed from context-engineering-v2@1c65307 prompts/fix_steps.txt ("Guard as early as possible",
# "Prefer returning the input unchanged", "Fix at the right abstraction level", "Cover every affected
# path", "Preserve return conventions"), here given to the model that writes the code (profile
# fix_rules) instead of to a separate planning call, which hurt the 4B model.
FIX_RULES = """
How to choose the fix:
- Fix the cause, not the symptom: change the code where the wrong value is first produced or the
  wrong assumption is made, so every input of the same kind works, not only the report's example.
- Guard as early as possible. An early return for an input the function cannot handle usually
  returns the input unchanged, in the function's normal return type.
- Cover every affected path: if the same wrong check or assumption appears in other places of the
  code shown, change each of them the same way.
- Compare the result with the report's expected output as a whole: every part that still differs
  needs a change.
- Keep the behaviour for inputs that already work, and keep the function's return conventions."""

TOOL_SYSTEM = """You fix bugs in a Python repository with tools. Tools: view_code (read a function),
replace (exact existing lines -> new lines, inside one function; the usual edit), write_function
(a complete new version of a function, or a new method / helper), run_check (runs the bug
report's example and the existing tests on your current code), submit (finish).
Work like this: make the smallest change that fixes the bug, run_check, read the result, fix
what is still wrong, run_check again, then submit. Copy `old` exactly from the code shown.
Python 3.9; import inside a function what it newly needs; no tests or prints; do not rename
existing functions. Say in one sentence what you are doing before each tool call."""

TOOL_TASK = """

Use the tools to fix it: edit, then run_check."""

TOOL_NUDGE = """Use a tool: replace or write_function to edit, run_check to test, submit to finish."""

STEPS = """

Your plan for the fix (follow it unless the code shows it is wrong):
{steps}"""

# Condensed from context-engineering-v2@1c65307 prompts/fix_steps.txt (its guidelines for writing
# fix steps a repair agent follows); AUTOSE: JSON steps naming one function each, no file paths.
PLAN_SYSTEM = """You plan bug fixes. You do not write code. For each change the fix needs, name the
function to change and say exactly what to change there and why, in one or two sentences.
Guidelines:
- Fix the code that encodes the wrong assumption, not code that merely triggers the symptom.
- Guard as early as possible; an early return usually returns the input unchanged, in the shape
  the function normally returns.
- If several code paths share the wrong assumption, give each its own step.
- Name inner functions directly; give formulas when a value must be computed.
- Order steps as the code executes. No test changes."""

PLAN = """Bug report:
{report}

Diagnosis from the code investigation (usually right about where, may be wrong about details):
{diagnosis}

Code you may change:
{code}

Give the fix steps as JSON: steps, each with function and change."""

FEEDBACK ="""The harness applied your edits and ran the report's example code and the existing tests:
{result}

{ask}"""

ASK_REVISE = """Revise the fix. Give all the edits again, complete (they are applied to the original
code, not on top of your previous attempt)."""

BUILDING = """

The code above already contains your earlier, partly-right fix. Your edits now apply on top of it."""

ASK_IMPROVE = """Your patch is partly right, so it is kept: the code shown now includes it. Give only the
further edits needed on top of it."""

ASK_DIFFERENT ="""Find a different fix: re-read the code shown and the line that raises, and change
what actually computes the wrong thing. Give all the edits, complete."""

REFUSED ="""Your edits could not be applied:
{errors}

Give all the edits again, complete, following the edit rules."""
