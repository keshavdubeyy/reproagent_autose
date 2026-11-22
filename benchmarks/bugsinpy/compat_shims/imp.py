"""Minimal stand-in for the `imp` module (removed in Python 3.12) used by old BugsInPy subjects."""
import importlib.machinery
import importlib.util
import sys


def load_source(name, pathname, file=None):
    loader = importlib.machinery.SourceFileLoader(name, pathname)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module
