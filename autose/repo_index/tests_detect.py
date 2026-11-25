"""Tag test files, test classes and test functions by pytest / unittest naming rules.

Static rules only (running `pytest --collect-only` would need the repo's installed
environment). They follow pytest's defaults plus the layouts of the SWE-bench repos:
  file      test_*.py, *_test.py, conftest.py, tests.py (Django apps), or any file
            under a tests/ test/ testing/ directory (pytest keeps its own in testing/)
  class     in a test file, named Test* / *Test / *Tests / *TestCase, or with a base
            named *TestCase (unittest, Django)
  function  in a test file, named test*, defined at module level or in a test class
"""
from __future__ import annotations

TEST_DIRS = {"tests", "test", "testing"}
TEST_CLASS_SUFFIXES = ("Test", "Tests", "TestCase")


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if name.startswith("test_") or name.endswith("_test.py") or name in ("conftest.py", "tests.py"):
        return True
    return any(p in TEST_DIRS for p in parts[:-1])


def is_test_class(name: str, bases: list[str]) -> bool:
    return (name.startswith("Test") or name.endswith(TEST_CLASS_SUFFIXES)
            or any(b.endswith("TestCase") for b in bases))


def is_test_function(name: str, parent_kind: str, parent_is_test_class: bool) -> bool:
    if not name.startswith("test"):
        return False
    return parent_kind == "file" or (parent_kind == "class" and parent_is_test_class)
