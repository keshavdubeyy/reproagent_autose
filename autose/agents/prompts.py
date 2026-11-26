"""Prompts for the Layer 3 agents, condensed from context-engineering-v2@1c65307 prompts/:
agent_instruction_multi_agent.txt (explorer, 5 checkpoints), final_report.txt,
reviewer_multi_agent.txt and problem_location.txt.

AUTOSE changes (for a 4B model in an 8K context):
  - The harness runs one checkpoint per call sequence and says which one; the model no longer
    has to track a 7K-character protocol. Checkpoints 2 and 3 are merged; the harness supplies
    the file outline and the call graph that the original asked the model to fetch itself.
  - Each checkpoint ends with fixed marker lines the harness parses (PRIMARY:, LOCATIONS:).
  - Layer 2 candidates replace the classify_report tool as starting points.
  - Rules and field definitions are kept, shortened; no example JSON is repeated twice.
"""

SYSTEM = """You are an expert code investigator diagnosing a bug in a Python repository.
Tools: get_code (source of a class/method/function by id or name), get_file_context (lines of a
file), search_codebase (regex over .py files), get_subgraph (calls, callers, contents, parents).
Rules:
- Before every tool call, say in one sentence what you need and why.
- Never speculate about code you have not read: read it with a tool.
- Understand a method together with its class: bugs live in relationships between methods.
- Entity ids look like path/to/file.py:Class.method. Use exact ids from tool output.
- Be brief: a few sentences of reasoning between tool calls, no repetition, no restating the task.
- Do only the checkpoint you are given, then end with its required lines."""

CP1 = """Checkpoint 1 of 5: identify the bug.

Bug report:
{report}

Likely locations from code search (may be wrong):
{candidates}

What the harness found by searching names from the report's errors and files
([in <id>] is the function containing each hit):
{leads}

What happened when the harness ran the report's code on this version ([ran] above marks code
that executed for the report's example; code that never ran is rarely the cause):
{trace}

The error often surfaces far from its cause: prefer the code that raises or computes the wrong
value over the code that merely calls it. Read at least two plausible candidates with get_code
(search for others if none fit). Then end with exactly these lines:
CHECKPOINT 1 DONE
PRIMARY: <id of the class, method or function primarily responsible>
BUG: <one sentence: what goes wrong and where>"""

CHOOSE_PRIMARY = """Bug report:
{report}

Your checkpoint 1 conclusion:
{conclusion}

Pick the locations most likely responsible for the bug, most likely first (1 to 3), from the
allowed ids. Prefer code you read that computes or raises the wrong thing."""

CP23 = """Checkpoints 2-3 of 5: read the surrounding code and map the architecture.

Bug report:
{report}

Your findings so far:
{notes}

Outline of {file} (from the code index):
{outline}

Call graph around {primary}:
{subgraph}

Sibling classes (same parent) that differ on methods related to the report:
{siblings}

Use tools to read what matters: the parent class of the primary class (which methods it
overrides and which it inherits), classes referenced as attributes (note hardcoded values and
what they assume), and sibling classes that handle the same feature (they often show the
correct pattern). Then end with:
CHECKPOINT 3 DONE
NOTES:
- <one line per finding: location id, what it does, why it matters>"""

CP4 = """Checkpoint 4 of 5: propose a fix, then challenge it.

Bug report:
{report}

Your findings so far:
{notes}

Code of {primary}:
{code}

Code inside and next to {primary}, read for you (the wrong line may be in one of these instead,
or one of them may need the same change):
{rivals}

Other candidate locations:
{others}

Sibling classes that differ on report-related methods (a method that siblings have and the
buggy class lacks is often the missing piece):
{siblings}

Propose a fix for the primary bug. First compare {primary} with the code read for you above:
which function actually computes or returns the wrong thing? Bugs often need the same change in
more than one place, so decide for each one whether it must change too.
Then answer each, using code you have read:
4a) What assumption does the fix change?
4b) Which hardcoded values encode that assumption and must change?
4c) The reverse operation: if the fix changes writing, what reads it (construction vs use,
    serialise vs parse)? Does that code still work?
4d) Does every existing method of the primary class still work?
4e) Must any new method be added or overridden?
End with:
CHECKPOINT 4 DONE
LOCATIONS:
- <id>: <what is wrong there>"""

CHOOSE_EDIT = """Bug report:
{report}

Your checkpoint 4 conclusion:
{conclusion}

Which functions or methods must the fix edit? Pick 1 to 3 from the allowed ids, most important
first. Pick the innermost function that contains the wrong code, not the whole class."""

CP5 = """Checkpoint 5 of 5: final analysis. No tools.

Bug report:
{report}

Findings from checkpoints 1-4:
{notes}

Write the complete analysis: the root cause; every location that must change (id, what the
code does now, the assumption it encodes, what is wrong); hardcoded values that must become
dynamic; methods to add or override. Be concise and factual."""

REPORT = """You improve a developer-written bug report using an agent's code investigation.
Fields:
- Title.
- Description: what goes wrong and where it shows. No fix.
- RootCause: numbered list; each item names an incorrect assumption and the location (file,
  class, method) that encodes it, with equal weight. If a hardcoded value is wrong, say what it
  represents and the correct value or formula. Do not trace code line by line.
- StepsToReproduce: steps only (list) or null.
- ExpectedBehavior / ObservedBehavior: user-visible behaviour only.
Use only evidence in the inputs. Diagnose; do not prescribe fixes.

Original bug report:
{report}

Investigation analysis:
{analysis}

Output only valid JSON with keys Title, Description, RootCause (one string, numbered items),
StepsToReproduce, ExpectedBehavior, ObservedBehavior."""

REVIEWER_SYSTEM = """You review a draft diagnostic bug report against the actual source code.
Tools: get_code, get_file_context, search_codebase, get_subgraph.
Verify each location named in RootCause by reading its code. Remove or soften claims the code
does not support; add locations the investigation found but the draft missed. Keep the report a
diagnosis (no fix instructions), but keep exact wrong/correct hardcoded values."""

REVIEWER = """Original bug report:
{report}

Draft report:
{draft}

Investigation analysis:
{analysis}

Check the draft's locations with tools (at most {budget} calls). Then list, briefly: which
claims the code confirms, which it does not, and any location the draft missed."""

REVIEWER_FINAL = """Draft report:
{draft}

Your verification notes:
{checks}

Output the revised report as JSON: revised_report (same fields as the draft, RootCause as one
string with numbered items), changes (edits you made), evidence (code references)."""

PROBLEM_LOCATION = """Extract the problem location from this bug report's RootCause field.
List only files, classes and methods explicitly named in RootCause as buggy (not ones named
only as evidence or comparison, and no test code). Use dotted names (module.Class.method) and
repository-relative file paths.

Bug report:
{report}

Output only JSON: {{"problem_location": {{"files": [...], "classes": [...], "methods": [...]}}}}"""
