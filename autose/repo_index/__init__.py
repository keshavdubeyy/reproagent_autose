"""Layer 1, knowledge building: a Python repo at one commit -> repository_index.json.

The graph builder is context-engineering-v2's (cev2_graph.py, ported with fixes);
tests_detect.py tags tests; build.py writes the single JSON artifact.
"""
from autose.repo_index.build import build_index, write_index

__all__ = ["build_index", "write_index"]
