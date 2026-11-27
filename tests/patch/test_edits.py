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
