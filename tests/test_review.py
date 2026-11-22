"""Review agent rules (autose/agent/review.py)."""
from autose.agent.blackboard import patch_sha
from autose.agent.loop import AgentConfig, ReproAgent
from autose.agent.models import BugReport, BugSpec, FaultCandidate, Patch, PatchCheck
from autose.agent.review import Reviewer

OLD = '''class Scaffold:
    def __init__(self, import_name, static_url_path=None):
        self.static_url_path = static_url_path

class Blueprint(Scaffold):
    def __init__(self, name, import_name, static_url_path=None):
        super().__init__(import_name=import_name, static_url_path=static_url_path)
        self.name = name
'''
GOOD = OLD.replace("        super().__init__(", "        if '.' in name:\n            raise ValueError('dot')\n        super().__init__(")
TYPO = GOOD.replace("static_url_path=static_url_path)", "static_tpath=static_url_path)")
REP, SPEC = BugReport("b", "Raise error when blueprint name contains a dot", ""), BugSpec("", "raise", "")


def repo_with(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bp.py").write_text(OLD)
    return tmp_path


def test_good_patch_is_approved(tmp_path):
    r = Reviewer().review(REP, SPEC, repo_with(tmp_path), Patch(1, "", diff="d", files={"src/bp.py": GOOD}))
    assert r.decision == "APPROVE"


def test_invented_name_is_rejected(tmp_path):
    r = Reviewer().review(REP, SPEC, repo_with(tmp_path), Patch(1, "", diff="d", files={"src/bp.py": TYPO}))
    assert r.decision == "REJECT" and "`static_tpath`" in r.reasons[0]


def test_editing_tests_is_rejected(tmp_path):
    repo = repo_with(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_bp.py").write_text("def test_x():\n    assert True\n")
    r = Reviewer().review(REP, SPEC, repo, Patch(1, "", diff="d", files={"tests/test_bp.py": "def test_x():\n    pass\n"}))
    assert r.decision == "REJECT" and "test files" in r.reasons[0]


def test_slm_needs_a_concrete_problem(tmp_path):
    class Fake:
        calls = 0
        def __init__(self, reply): self.reply = reply
        def chat(self, *a, **k): return self.reply
    repo = repo_with(tmp_path)
    p = Patch(1, "", diff="d", files={"src/bp.py": GOOD})
    vague = Reviewer(Fake('{"decision": "REJECT", "problem": "bad"}'), use_slm=True).review(REP, SPEC, repo, p)
    concrete = Reviewer(Fake('{"decision": "REJECT", "problem": "it also changes the name attribute"}'),
                        use_slm=True).review(REP, SPEC, repo, p)
    assert vague.decision == "APPROVE" and concrete.decision == "REJECT" and concrete.by == "rules+slm"


def test_rejection_goes_back_to_the_repair_loop_and_blocks_freezing(tmp_path):
    class NoLLM:
        calls = 0
    repo = repo_with(tmp_path)
    agent = ReproAgent(NoLLM(), AgentConfig(review="rules"), log=lambda _m: None)
    agent._review_ctx = (REP, SPEC, repo)
    check = PatchCheck("plausible", True, "", repro_status="passed")
    p = Patch(1, "", diff="diff-typo", files={"src/bp.py": TYPO})
    agent._record([], p, check, [FaultCandidate("src/bp.py", "bp", "Blueprint.__init__", 6, 8, "", 3.0)])
    assert not check.accepted and check.category == "review_rejected" and "static_tpath" in check.feedback
    assert agent.board.latest("REVIEW_REPORT")["decision"] == "REJECT"
    import pytest
    from autose.agent.blackboard import BlackboardError
    with pytest.raises(BlackboardError):
        agent.board.freeze("diff-typo")
