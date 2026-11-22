# ADR 0008: Deterministic Python orchestrator and a file blackboard instead of LangGraph

Status: accepted (22 Nov 2026)

## Context
The architecture calls for a LangGraph orchestrator, an event bus and a shared blackboard. The ported agent
already has a deterministic workflow (reproduce -> localise -> patch -> validate) with budgets.

## Decision
- Keep a plain Python orchestrator (`autose/agent/loop.py`). The control flow stays deterministic; the
  model only fills narrow roles (spec, test, patch, review).
- Agents exchange typed JSON artifacts in the run directory: BUG_SPEC, CHANGE_PLAN + PATCH, TEST_REPORT,
  REVIEW_REPORT. That is the blackboard; the orchestrator reads them to decide the next step.

## Consequences
- No new framework to learn or debug in the last week; the design's rule "the control plane is
  deterministic Python, not a free-running LLM" still holds.
- No event bus: events are the artifacts themselves, read in order.
