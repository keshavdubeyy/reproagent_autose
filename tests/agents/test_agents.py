"""Layer 3 tests: tools, the checkpoint pipeline with a scripted model, the localization metrics."""
import json
from pathlib import Path
from textwrap import dedent

import pytest

from autose.agents.pipeline import run_instance
from autose.agents.tools import Tools
from autose.evaluation.layer3_eval import entities_as_prediction, metrics, parse_patch_locations
from autose.repo_index import build_index

FILES = {
    "pkg/__init__.py": "",
    "pkg/bp.py": dedent("""\
        class Scaffold:
            def __init__(self, name):
                self.name = name


        class Blueprint(Scaffold):
            def __init__(self, name):
                super().__init__(name)

            def add_url_rule(self, endpoint):
                assert "." not in endpoint
        """),
    "tests/test_bp.py": "def test_bp():\n    pass\n",
}


@pytest.fixture()
def repo(tmp_path):
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def index(repo):
    return build_index(repo, "deadbeef")


@pytest.fixture()
def tools(index, repo):
    return Tools(index, repo)


def test_get_code_reads_snapshot_lines(tools):
    out = tools.get_code("Blueprint.__init__")
    assert out.startswith("pkg/bp.py:Blueprint.__init__ (method, lines 7-8)")
    assert "8:         super().__init__(name)" in out


def test_argument_aliases_and_single_argument(tools):
    assert "lines 7-8" in tools.call("get_code", {"id": "pkg/bp.py:Blueprint.__init__"})
    assert "lines 7-8" in tools.call("get_code", {"whatever": "Blueprint.__init__"})
    assert "pkg/bp.py lines 1-3" in tools.call("get_file_context", {"path": "pkg/bp.py", "start": 1, "end": 3})
    assert "pkg/bp.py:11 [in pkg/bp.py:Blueprint.add_url_rule]:" in tools.call("search_codebase", {"query": "assert"})


def test_bad_paths_and_names_do_not_raise(tools):
    assert tools.call("get_file_context", {"file": "../../etc/passwd"}).startswith("not found")
    assert tools.call("get_code", {"node": "nope"}).startswith("not found")
    assert tools.call("get_code", {"node": "__init__"}).startswith("ambiguous")
    assert tools.call("no_such_tool", {}).startswith("unknown tool")


def test_subgraph_has_direction(tools):
    out = tools.get_subgraph("pkg/bp.py:Blueprint")
    assert "inherits from pkg/bp.py:Scaffold" in out and "contains pkg/bp.py:Blueprint.add_url_rule" in out


class ScriptedLLM:
    """Answers each prompt by checkpoint, calling one tool where tools are offered."""

    def __init__(self):
        self.usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0}
        self.prompts = []

    def chat(self, messages, tools=None, max_tokens=None, fmt=None):
        self.usage["calls"] += 1
        text = messages[1]["content"] if len(messages) > 1 else messages[0]["content"]
        self.prompts.append(text)
        tooled = any(m["role"] == "tool" for m in messages)
        if tools and not tooled:
            return {"content": "Reading it.", "tool_calls": [{"name": "get_code", "arguments": {"id": "Blueprint"}}],
                    "prompt_tokens": 10}
        if text.startswith("Checkpoint 1"):
            out = "CHECKPOINT 1 DONE\nPRIMARY: `pkg/bp.py:Blueprint.__init__`\nBUG: no dot check"
        elif text.startswith("Checkpoints 2-3"):
            out = "CHECKPOINT 3 DONE\nNOTES:\n- pkg/bp.py:Scaffold: parent"
        elif text.startswith("Checkpoint 4"):
            out = "CHECKPOINT 4 DONE\nLOCATIONS:\n- pkg/bp.py:Blueprint.__init__: no check"
        elif fmt and '"edit"' in json.dumps(fmt):
            out = json.dumps({"edit": ["pkg/bp.py:Blueprint.add_url_rule", "pkg/nope.py:Ghost"]})
        elif fmt and "ranked" in json.dumps(fmt):
            out = json.dumps({"ranked": ["pkg/bp.py:Blueprint.__init__", "pkg/bp.py:Blueprint"]})
        elif fmt and "problem_location" in json.dumps(fmt):
            out = json.dumps({"problem_location": {"files": ["pkg/x/bp.py"], "classes": ["Blueprint"],
                                                   "methods": ["___init___", "pkg/x/bp.py: Blueprint.add_url_rule",
                                                               "check_name", "Ghost.made_up"]}})
        elif fmt and "revised_report" in json.dumps(fmt):
            out = json.dumps({"revised_report": {"Title": "t", "RootCause": "1. Blueprint.__init__"},
                              "changes": [], "evidence": ["pkg/bp.py:7"]})
        elif fmt:
            out = json.dumps({"Title": "t", "RootCause": "1. Blueprint.__init__ allows dots"})
        else:
            out = "analysis"
        return {"content": out, "tool_calls": [], "prompt_tokens": 10}


def test_pipeline_runs_checkpoints_in_order(index, repo):
    llm = ScriptedLLM()
    retrieval = {"entities": [{"id": "pkg/bp.py:Blueprint"}, {"id": "pkg/bp.py:Blueprint.add_url_rule"},
                              {"id": "tests/test_bp.py:test_bp"}]}
    out = run_instance({"Description": "dots allowed in blueprint names"}, index, repo, retrieval, llm)
    assert out["primary"] == "pkg/bp.py:Blueprint.__init__" and out["primary_source"] == "model"
    assert [s for s in ("cp1", "cp23", "cp4", "cp5", "reviewer")
            if any(t["stage"] == s for t in out["trajectory"])] == ["cp1", "cp23", "cp4", "cp5", "reviewer"]
    cp4 = next(p for p in llm.prompts if p.startswith("Checkpoint 4"))
    assert "pkg/bp.py:Blueprint.add_url_rule" in cp4            # other candidates handed over
    assert 'assert "." not in endpoint' in cp4                  # ... with their code, read by the harness
    assert out["harness_read"][0] == "pkg/bp.py:Blueprint.add_url_rule"
    assert out["edit_locations"] == ["pkg/bp.py:Blueprint.add_url_rule"]  # the invented id is not allowed
    pl = out["problem_location"]  # snapped to real entities; the invented name is dropped
    assert pl["methods"] == ["pkg.bp.Blueprint.__init__", "pkg.bp.Blueprint.add_url_rule",
                             "pkg.bp.Blueprint.check_name"]           # a method the fix would add
    assert pl["new_methods"] == ["pkg/bp.py:Blueprint.check_name"]
    assert pl["files"] == ["pkg/bp.py"] and pl["dropped"] == ["Ghost.made_up"]
    assert out["primary_ranked"] == ["pkg/bp.py:Blueprint.__init__", "pkg/bp.py:Blueprint"]
    assert out["bug_report"]["RootCause"] == "1. Blueprint.__init__"
    assert all(c["chars"] > 100 for c in out["tool_calls"])    # the 'id' alias worked


def test_trace_evidence_reaches_checkpoint1(index, repo):
    llm = ScriptedLLM()
    retrieval = {"entities": [{"id": "pkg/bp.py:Scaffold.__init__"}, {"id": "pkg/bp.py:Blueprint"}]}
    trace = {"status": "ran", "statements": 2, "ok_statements": 1, "timed_out": False,
             "executed": {"pkg/bp.py:Blueprint.add_url_rule": 1, "pkg/bp.py:Blueprint.__init__": 1},
             "baseline": [], "frames": ["pkg/bp.py:Blueprint.add_url_rule"],
             "errors": [{"i": 1, "error": "AssertionError: dotted name"}, {"i": 0, "error": "NameError: x"}]}
    out = run_instance("dots in endpoint", index, repo, retrieval, llm, trace)
    cp1 = next(p for p in llm.prompts if p.startswith("Checkpoint 1"))
    assert "1. pkg/bp.py:Blueprint.add_url_rule [ran]" in cp1    # the raising frame is the first candidate
    assert "Raised: AssertionError: dotted name" in cp1 and "NameError" not in cp1
    assert out["trace_used"] and "pkg/bp.py:Blueprint.add_url_rule" in out["leads"]


def test_layer2_fallback_when_primary_is_unknown(index, repo):
    class NoPrimary(ScriptedLLM):
        def chat(self, messages, tools=None, max_tokens=None, fmt=None):
            r = super().chat(messages, tools, max_tokens, fmt)
            r["content"] = r["content"].replace("PRIMARY: `pkg/bp.py:Blueprint.__init__`", "PRIMARY: somewhere")
            if fmt and "ranked" in json.dumps(fmt):
                r["content"] = json.dumps({"ranked": []})
            return r
    out = run_instance("bug", index, repo, {"entities": [{"id": "pkg/bp.py:Blueprint"}]}, NoPrimary())
    assert (out["primary"], out["primary_source"]) == ("pkg/bp.py:Blueprint", "layer2_fallback")


def test_suggest_finds_renamed_files_and_leads_search_error_names(index, repo):
    from autose.agents.pipeline import report_leads
    tools = Tools(index, repo)
    assert tools.suggest("pkg/bpp.py")[0] == "pkg/bp.py"
    assert "Closest" in tools.get_file_context("pkg/blueprint.py")
    report = ('Calling it fails in pkg/old_bp.py:\nAssertionError: endpoint "add_url_rule" rejected\n'
              'File "x/pkg/bp.py", line 11, in add_url_rule')
    text, ids = report_leads(report, tools)
    assert ids == ["pkg/bp.py:Blueprint.add_url_rule"]
    assert "[in pkg/bp.py:Blueprint.add_url_rule]" in text and "pkg/old_bp.py: not in this version" in text
    assert tools.calls == []  # harness searches are not counted as the model's


def test_choose_primary_is_limited_to_real_ids(index, repo):
    from autose.agents.pipeline import choose_primary
    class Liar(ScriptedLLM):
        def chat(self, messages, tools=None, max_tokens=None, fmt=None):
            self.schema = fmt
            return {"content": json.dumps({"ranked": ["pkg/nope.py:Ghost", "pkg/bp.py:Scaffold"]}),
                    "tool_calls": [], "prompt_tokens": 1}
    llm, tools = Liar(), Tools(index, repo)
    ranked = choose_primary(llm, tools, "bug", "conclusion", ["pkg/bp.py:Scaffold", "tests/test_bp.py:test_bp"], None, [])
    assert ranked == ["pkg/bp.py:Scaffold"]
    assert llm.schema["properties"]["ranked"]["items"]["enum"] == ["pkg/bp.py:Scaffold"]  # tests excluded


def test_rivals_and_kept_locations(index, repo):
    from autose.agents.pipeline import finalise_locations, rivals
    tools = Tools(index, repo)
    # a class primary: the harness reads its own methods first, most report-like first
    assert rivals("pkg/bp.py:Blueprint", [], tools, "add_url_rule endpoint") == \
        ["pkg/bp.py:Blueprint.add_url_rule", "pkg/bp.py:Blueprint.__init__", "pkg/bp.py:Scaffold.__init__"]
    assert "pkg/bp.py:Blueprint.add_url_rule" not in tools.read      # harness reads are not the model's
    # extraction named the wrong thing: the primary is still reported
    pl = finalise_locations({"methods": ["Scaffold.__init__"]}, tools, "pkg/bp.py:Blueprint.add_url_rule", [], [], [])
    assert pl["entities"] == ["pkg/bp.py:Scaffold.__init__", "pkg/bp.py:Blueprint.add_url_rule"]
    # a class primary whose method was chosen for the edit: report the method, not the whole class
    pl = finalise_locations({}, tools, "pkg/bp.py:Blueprint", [], [], [], ["pkg/bp.py:Blueprint.add_url_rule"])
    assert pl["entities"] == ["pkg/bp.py:Blueprint.add_url_rule"] and pl["source"] == "ranked_fallback"


def test_patch_locations_and_metrics_match_cev2():
    patch = dedent("""\
        diff --git a/pkg/bp.py b/pkg/bp.py
        --- a/pkg/bp.py
        +++ b/pkg/bp.py
        @@ -6,4 +6,6 @@ class Blueprint(Scaffold):
             def __init__(self, name):
        +        if "." in name:
        +            raise ValueError
                 super().__init__(name)
        """)
    gold = parse_patch_locations(patch)
    assert gold == {"classes": [], "methods": ["__init__"]}
    assert metrics(["pkg.bp.Blueprint.__init__", "add_url_rule"], gold["methods"]) == \
        {"hit": 1, "coverage": 1, "precision": 0.5, "recall": 1.0, "f1": 0.6667}
    assert metrics([], gold["classes"]) is None
    kind = {"pkg/bp.py:Blueprint": "class", "pkg/bp.py:Blueprint.__init__": "method"}
    assert entities_as_prediction(["pkg/bp.py:Blueprint.__init__", "pkg/bp.py:Blueprint"], kind) == \
        {"classes": ["Blueprint"], "methods": ["__init__"]}


def test_agent_code_never_reads_evaluation_data():
    src = "\n".join(p.read_text(encoding="utf-8") for p in Path("autose/agents").glob("*.py"))
    for banned in ("private", "test_specs", "gold_patch", "autose.evaluation", "external/", "FAIL_TO_PASS"):
        assert banned not in src


def test_strict_score_needs_the_right_class_and_file():
    from autose.evaluation.layer3_eval import strict
    gold, files = {"pkg/c.py:CPrinter"}, {"pkg/c.py"}  # the fix adds a method to CPrinter
    assert strict(["pkg/octave.py:OPrinter._print_sinc"], gold, files) == \
        {"entity_hit": 0, "entity_recall": 0.0, "entity_precision": 0.0, "file_hit": 0}
    assert strict(["pkg/c.py:CPrinter._print_sinc", "pkg/c.py:other"], gold, files) == \
        {"entity_hit": 1, "entity_recall": 1.0, "entity_precision": 0.5, "file_hit": 1}
