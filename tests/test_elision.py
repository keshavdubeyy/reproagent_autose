"""Fix 3: the model writes `# ... (existing code)` instead of lines it keeps. The applier must put the
original lines back (night 1+2: such patches silently deleted up to 190 lines), or ask for the full code."""

from autose.agent.models import FaultCandidate, Patch
from autose.agent.repair import FaultLocalizer, PatchApplier, _elision_lines, fill_elisions

ORIG = '''class Printer:
    def render(self, expr, mode):
        """Render. Example:

        >>> p.render(x, 'a')
        ...
        """
        table = ['asin', 'acos', 'atan']
        if mode == 'a':
            name = 'a' + expr
        else:
            name = expr
        out = []
        for t in table:
            out.append(t)
        out.append(name)
        return ''.join(out)
'''


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "p.py").write_text(ORIG)
    loc = FaultLocalizer(tmp_path)
    return loc, [FaultCandidate("pkg/p.py", "pkg.p", "Printer.render", 2, 17, "", 1.0)]


def test_placeholder_in_the_middle_keeps_the_original_lines(tmp_path):
    """sympy-14774: only the table line changes; the model skips the rest of the method."""
    loc, cands = _repo(tmp_path)
    p = Patch(1, "class Printer:\n    def render(self, expr, mode):\n"
                 "        # ... (existing code)\n"
                 "        table = ['asin', 'acos', 'atan', 'acsc', 'asec', 'acot']\n"
                 "        # ... (rest of the method unchanged)\n")
    assert PatchApplier(loc).apply(p, cands) is None
    new = p.files["pkg/p.py"]
    assert new == ORIG.replace("'atan']", "'atan', 'acsc', 'asec', 'acot']")
    removed = [l for l in p.diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    assert len(removed) == 1                              # one line changed, nothing deleted


def test_bare_ellipsis_placeholder(tmp_path):
    loc, cands = _repo(tmp_path)
    p = Patch(1, "class Printer:\n    def render(self, expr, mode):\n        ...\n"
                 "        if mode == 'a':\n            name = 'A' + expr\n        ...\n")
    assert PatchApplier(loc).apply(p, cands) is None
    assert p.files["pkg/p.py"] == ORIG.replace("'a' + expr", "'A' + expr")


def test_unplaceable_placeholder_asks_for_the_complete_definition(tmp_path):
    """The line after the placeholder does not exist in the current code: the gap is unknown."""
    loc, cands = _repo(tmp_path)
    p = Patch(1, "class Printer:\n    def render(self, expr, mode):\n        # ... (other code)\n"
                 "        result = compute(expr)\n        return result\n")
    err = PatchApplier(loc).apply(p, cands)
    assert err and "placeholder" in err and "COMPLETE" in err and not p.files


def test_strings_real_stubs_and_normal_comments_are_not_placeholders():
    src = ('def f(x):\n    """Doc.\n\n    >>> f(1)\n    ...\n    """\n'
           '    # Any remaining kwargs must be properties\n'
           '    # Return pairs [(a, b), ...]\n    return x\n')
    assert _elision_lines(src) == []
    stub = "def g():\n    ...\n"
    assert fill_elisions(stub, stub) == (stub, None)       # `...` that is already there stays


def test_truncated_candidate_copied_back_keeps_the_hidden_tail():
    """Definitions over MAX_DEF_LINES are shown cut, ending in our own '# ... (truncated)'."""
    orig = "def f(x):\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c + x\n"
    new = "def f(x):\n    a = 10\n    b = 2\n    # ... (truncated)\n"
    merged, bad = fill_elisions(orig, new)
    assert bad is None and merged == "def f(x):\n    a = 10\n    b = 2\n    c = 3\n    return a + b + c + x"


# ------------------------------------------------------------------ guards: the applier never silently loses code
def test_silently_dropped_docstring_lines_are_put_back():
    """sympy-14308: the model copied a long docstring but left out 21 lines of it."""
    from autose.agent.repair import guard_definition
    orig = 'def f(x):\n    """Doc.\n\n    More doc.\n    Even more.\n    """\n    return x + 1\n'
    new = 'def f(x):\n    """Doc.\n    """\n    return x + 2\n'
    merged, problem = guard_definition(orig, new)
    assert problem is None
    assert merged == 'def f(x):\n    """Doc.\n\n    More doc.\n    Even more.\n    """\n    return x + 2'


def test_large_code_deletion_is_refused():
    """django-12497: the rewrite kept the start of a long method and dropped 61 lines of checks."""
    from autose.agent.repair import GUARD_MAX_DROPPED, guard_definition
    body = "".join(f"    check_{i}(x)\n" for i in range(GUARD_MAX_DROPPED + 5))
    orig = "def f(x):\n    a = 1\n" + body + "    return a\n"
    merged, problem = guard_definition(orig, "def f(x):\n    a = 2\n    return a\n")
    assert problem and "deletes" in problem and "COMPLETE" in problem
    small = "def f(x):\n    a = 1\n    check_0(x)\n    check_1(x)\n    return a\n"   # a real fix may delete a few
    assert guard_definition(small, "def f(x):\n    a = 1\n    return a\n")[1] is None


def test_a_function_cannot_replace_a_class(tmp_path):
    """sympy-13437: `class bell(Function)` with its methods was replaced by a 20-line `def bell(n)`."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "n.py").write_text("class bell:\n    def eval(self, n):\n        return n\n")
    loc = FaultLocalizer(tmp_path)
    cands = [FaultCandidate("pkg/n.py", "pkg.n", "bell", 1, 3, "", 1.0)]
    p = Patch(1, "def bell(n):\n    return 0\n")
    err = PatchApplier(loc).apply(p, cands)
    assert err and "CLASS" in err and not p.files


def test_llm_records_prompt_window_and_cut_off_warnings():
    from autose.agent.llm import LLM
    llm = LLM(num_ctx=6144)
    llm.calls = 1
    llm._check_window({"prompt_eval_count": 3000, "done_reason": "stop"})
    assert llm.warnings == [] and llm.max_prompt_tokens == 3000
    llm.calls = 2
    llm._check_window({"prompt_eval_count": 5000, "done_reason": "length", "eval_count": 1536})
    assert [w["kind"] for w in llm.warnings] == ["prompt_window", "reply_cut_off"]
    assert all(w["call"] == 2 for w in llm.warnings)
