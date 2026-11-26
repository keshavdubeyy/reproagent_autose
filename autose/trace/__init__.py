"""Execution traces for Layer 3: run the bug report's code against the snapshot and record which
repository functions ran (autose/trace/runner.py), so the agents get evidence that does not
depend on the report's wording."""
from autose.trace.runner import trace_instance
from autose.trace.snippets import statements

__all__ = ["statements", "trace_instance"]
