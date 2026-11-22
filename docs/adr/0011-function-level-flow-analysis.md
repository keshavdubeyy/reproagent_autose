# ADR 0011: Function-level CFG, data flow and slicing on demand (a lightweight code property graph)

Status: accepted (22 Nov 2026)

## Context
The knowledge package (ADR 0007) keeps AST symbols, a call graph and an import (dependency) graph. It
says which functions are connected, not which of those connections can affect a wrong value. The team
asked for the other classic program representations as well: control flow graph, data flow analysis,
program slicing and a code property graph that combines them.

## Decision
- `autose/intel/flow.py` builds, for one function at a time: a statement-level CFG (branches, loops,
  break/continue, try/except/finally, return/raise, match), reaching definitions (def -> use edges for
  variables and attribute paths such as `self.items`), structural control dependence, and backward slices
  from a line or from the return statements.
- Together with the knowledge package this is a lightweight CPG: AST + CFG + data dependence + control
  dependence inside functions, linked across functions by CALLS / IMPORTS / INHERITS edges.
- It is computed on demand (0.35 ms per function; all 23,955 django functions in 8.5 s, no failures)
  and not stored, so the index stays small and fast to build.
- Use in the agent: the Fault Localizer slices each top candidate from its failing traceback line, or
  from its returns, and ranks callees whose call lies in the slice above callees that cannot affect the
  result (`FaultLocalizer(use_slicing=...)`, on by default).

## Consequences
- Python is dynamic: no alias analysis, `self.x` is one variable, calls are not followed inside the
  slice (the call graph does that). Results are approximations and the paper says so.
- On the 19 SWE-bench Lite bugs whose report has a traceback with a project frame, the crashing
  function is the fixed one in 11; slicing cuts callee candidates from 5.6 to 2.1 on average and keeps
  1 of the 2 fixed functions found among callees. Its real effect is measured in the agent runs.
- Inter-procedural slicing (following parameters to callers) and storing the CPG are future work.
