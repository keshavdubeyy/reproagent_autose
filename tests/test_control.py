"""Loop controller: failure fingerprints (autose/agent/control.py)."""
from autose.agent.control import LoopController, fingerprint


def test_fingerprint_ignores_numbers_and_quoted_values():
    a = fingerprint("repair", "repro_still_fails", "failed", "assert 1980 == 2080 for '01 Jan 1980'")
    b = fingerprint("repair", "repro_still_fails", "failed", "assert 1981 == 2081 for '02 Feb 1981'")
    c = fingerprint("repair", "regression", "failed", "assert 1980 == 2080 for '01 Jan 1980'")
    assert a == b and a != c


def test_continue_switch_stop():
    ctl = LoopController()
    acts = [ctl.observe("repair", "repro_still_fails", "failed", f"assert {n} == 5").action for n in (1, 2, 3)]
    assert acts == ["continue", "switch", "stop"]
    assert ctl.switches == 1 and ctl.stops == 1


def test_a_different_failure_resets_the_count():
    ctl = LoopController()
    ctl.observe("repair", "repro_still_fails", "failed", "assert 1 == 5")
    assert ctl.observe("repair", "regression", "failed", "test_x broke").action == "continue"
    assert ctl.observe("repair", "repro_still_fails", "failed", "assert 1 == 5").action == "continue"
