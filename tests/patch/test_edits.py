import subprocess
import textwrap

import pytest

from autose.patch.edits import EditError, Workspace, apply_answer, rewrite, search_replace

SRC = textwrap.dedent('''\
    import math


    class Shape:
        @property
        def area(self):
            return self.w * self.h

        def scale(self, k):
            self.w *= k
            self.h *= k
            return self


    def half(x):
        return x / 2
    ''')
ENTS = {
    "area": {"id": "pkg/shape.py:Shape.area", "file": "pkg/shape.py", "name": "Shape.area", "start_line": 6},
    "scale": {"id": "pkg/shape.py:Shape.scale", "file": "pkg/shape.py", "name": "Shape.scale", "start_line": 9},
    "half": {"id": "pkg/shape.py:half", "file": "pkg/shape.py", "name": "half", "start_line": 15},
}


def ws():
    return Workspace(read=lambda f: SRC)


def test_rewrite_keeps_indent_and_decorators():
    w = ws()
    rewrite(w, ENTS["area"], "def area(self):\n    return abs(self.w * self.h)\n")
    new = w.text("pkg/shape.py")
    assert "    @property\n    def area(self):\n        return abs(self.w * self.h)\n" in new
    assert new.count("@property") == 1 and "def scale" in new


def test_rewrite_drops_stray_code_and_rejects_wrong_or_broken_code():
    w = ws()
    rewrite(w, ENTS["half"], "import math\n\ndef half(x):\n    return x / 2.0\n\nprint(half(3))\n")
    assert w.text("pkg/shape.py").endswith("def half(x):\n    return x / 2.0\n") and "print(" not in w.text("pkg/shape.py")
    with pytest.raises(EditError, match="def half"):
        rewrite(w, ENTS["half"], "def other(x):\n    return x\n")
    with pytest.raises(EditError, match="does not parse"):
        rewrite(w, ENTS["half"], "def half(x):\n    return (x\n")


def test_search_replace_reindents_and_needs_a_unique_match():
    w = ws()
    search_replace(w, "pkg/shape.py", "self.w *= k\nself.h *= k", "self.w = self.w * k\nself.h = self.h * k")
    assert "        self.w = self.w * k\n        self.h = self.h * k\n" in w.text("pkg/shape.py")
    with pytest.raises(EditError, match="does not match"):
        search_replace(w, "pkg/shape.py", "self.d *= k", "x")
    w2 = Workspace(read=lambda f: "a = 1\na = 1\n")
    with pytest.raises(EditError, match="2 places"):
        search_replace(w2, "m.py", "a = 1", "a = 2")


def test_search_ignores_spacing_and_blank_lines_and_accepts_one_near_copy():
    w = ws()
    # extra blank line and odd spacing inside the line: still the same code
    search_replace(w, "pkg/shape.py", "self.w  *= k\n\nself.h *=  k", "self.w *= 2 * k\nself.h *= 2 * k")
    assert "        self.w *= 2 * k\n        self.h *= 2 * k\n        return self\n" in w.text("pkg/shape.py")
    assert w.log[-1].endswith("(spacing-insensitive match)")
    # a two-line block misquoted by a character: the one near-copy is used
    w2 = ws()
    search_replace(w2, "pkg/shape.py", "self.w *= k\nself.h *= k\nreturn sel", "self.w *= k\nreturn self")
    assert "        self.w *= k\n        return self\n\n\ndef half" in w2.text("pkg/shape.py")
    assert "near-copy" in w2.log[-1]
    # a single misquoted line is not guessed
    with pytest.raises(EditError, match="does not match"):
        search_replace(ws(), "pkg/shape.py", "self.w *= kk", "x")
    # two equally near places: refused
    w3 = Workspace(read=lambda f: "def f():\n    a = 1\n    b = 2\n\ndef g():\n    a = 1\n    b = 2\n")
    with pytest.raises(EditError):
        search_replace(w3, "m.py", "a = 1\nb = 3", "a = 0")


def test_search_found_outside_the_located_function_is_applied_if_unique():
    from autose.patch.edits import apply_edits
    w = ws()
    errs = apply_edits(w, [{"function": "half", "search": "return self.w * self.h",
                            "code": "return abs(self.w * self.h)"}], [ENTS["half"]])
    assert errs == [] and "(outside the located functions)" in w.log[-1]
    assert "    def area(self):\n        return abs(self.w * self.h)\n" in w.text("pkg/shape.py")


def test_propagate_copies_an_edit_to_the_other_identical_places_in_the_located_function():
    from autose.patch.edits import apply_edits, propagate
    src = ("def gen(ms):\n    if ms:\n        yield ms\n    else:\n        yield ms\n\n\n"
           "def other(ms):\n    yield ms\n")
    ent = {"id": "m.py:gen", "file": "m.py", "name": "gen", "kind": "function", "start_line": 1}
    edits = [{"function": "gen", "search": "    yield ms\n", "code": "yield ms.copy()"}]
    w = Workspace(read=lambda f: src)
    assert apply_edits(w, [{"function": "gen", "search": "    if ms:\n        yield ms", "code":
                            "if ms:\n    yield ms.copy()"}], [ent]) == []
    assert w.text("m.py").count("yield ms.copy()") == 1
    assert propagate(w, edits, [ent]) == 1
    # both branches of the located function now copy; the function that was not located is untouched
    assert w.text("m.py").count("yield ms.copy()") == 2 and w.text("m.py").endswith("def other(ms):\n    yield ms\n")
    assert "another identical place" in w.log[-1]
    # a replacement that contains its own search lines is never re-applied to its output
    w2 = Workspace(read=lambda f: src)
    assert propagate(w2, [{"function": "gen", "search": "yield ms", "code": "print(1)\nyield ms"}], [ent]) == 0
    # a search that matches several identical places is refused, unless all_places: then all change
    one = src.split("\n\n\n")[0] + "\n"
    assert "2 places" in apply_edits(Workspace(read=lambda f: one), edits, [ent])[0]
    w3 = Workspace(read=lambda f: one)
    assert apply_edits(w3, edits, [ent], all_places=True) == []
    assert w3.text("m.py").count("yield ms.copy()") == 2 and "all changed" in w3.log[-1]


def test_edit_that_changes_nothing_is_refused_with_a_reason():
    from autose.patch import prompts
    from autose.patch.edits import apply_edits
    w = ws()
    assert apply_edits(w, [{"function": "half", "search": "return x / 2", "code": "return x / 2"}],
                       [ENTS["half"]]) == []
    assert w.diff() == ""  # sample.py and loop.py then refuse it with prompts.NO_CHANGE
    assert "change nothing" in prompts.NO_CHANGE


def test_answer_to_diff_that_git_applies(tmp_path):
    w = ws()
    answer = ("The bug is in half.\n```python\ndef half(x):\n    return x / 2.0\n```\n"
              "<<<<<<< SEARCH\n        self.h *= k\n=======\n        self.h *= k * 1\n>>>>>>> REPLACE\n")
    errors = apply_answer(w, answer, [ENTS["scale"], ENTS["half"]])
    # SEARCH/REPLACE blocks win when present; the code block is not applied then
    assert errors == [] and "k * 1" in w.text("pkg/shape.py")
    assert apply_answer(w, "```python\ndef half(x):\n    return x / 2.0\n```", [ENTS["half"]]) == []
    assert "not one of the functions" in apply_answer(w, "```python\ndef area(self):\n    return 0\n```",
                                                     [ENTS["half"]])[0]
    patch = w.diff()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/shape.py").write_text(SRC, newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "p.diff").write_text(patch, newline="\n")
    subprocess.run(["git", "apply", "--check", "p.diff"], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", "p.diff"], cwd=tmp_path, check=True)
    assert (tmp_path / "pkg/shape.py").read_text() == w.text("pkg/shape.py")


def test_def_line_as_search_is_a_rewrite_and_replacements_keep_relative_indent():
    from autose.patch.edits import apply_edits
    w = ws()
    # a common small-model answer: the def line as `search`, the whole new function as `code`
    errs = apply_edits(w, [{"function": "scale", "search": "def scale(self, k):",
                            "code": "    def scale(self, k):\n        self.w *= 2 * k\n        return self\n"}],
                       [ENTS["scale"]])
    assert errs == [] and w.log == ["rewrote pkg/shape.py:Shape.scale"]
    assert "    def scale(self, k):\n        self.w *= 2 * k\n        return self\n" in w.text("pkg/shape.py")
    # replacement indented differently from the search: placed at the matched line's indentation
    w2 = ws()
    apply_edits(w2, [{"function": "scale", "search": "self.h *= k", "code": "if k:\n    self.h *= k"}],
                [ENTS["scale"]])
    assert "        if k:\n            self.h *= k\n" in w2.text("pkg/shape.py")


def test_replacement_repeating_the_following_lines_does_not_duplicate_them():
    from autose.patch.edits import apply_edits
    w = Workspace(read=lambda f: "def f(e):\n    if e < 0:\n        return e\n    return -e\n")
    ent = {"id": "m.py:f", "file": "m.py", "name": "f", "kind": "function", "start_line": 1}
    assert apply_edits(w, [{"function": "f", "search": "if e < 0:",
                            "code": "if e.real and e < 0:\n    return e"}], [ent]) == []
    assert w.text("m.py") == "def f(e):\n    if e.real and e < 0:\n        return e\n    return -e\n"
    errs = apply_edits(Workspace(read=lambda f: "def f(e):\n    if e < 0:\n        return e\n"),
                       [{"function": "f", "search": "if e <= 0:", "code": "if e < 1:"}], [ent])
    assert "closest lines" in errs[0] and "if e < 0:" in errs[0]
