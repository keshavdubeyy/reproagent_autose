"""Pipeline health summary (autose/evaluation/pipeline.py)."""
from autose.evaluation.pipeline import CHAIN, summarise, to_html


def bug(iid, **v):
    b = {"instance_id": iid, **{k: None for k in CHAIN}, "search_func": None}
    b.update(v)
    b["first_break"] = next((k for k in CHAIN if b.get(k) is False), None if b.get("resolved") else "unknown")
    return b


def test_stage_rates_handoffs_and_first_break():
    ok = dict(intake=True, search_file=True, repro=True, repro_valid=True, loc_file=True, loc_func=True,
              patch_applies=True, patch_gold=True, self_check=True)
    bugs = [bug("a", **ok, resolved=True),
            bug("b", **{**ok, "repro_valid": False}, resolved=False),
            bug("c", **{**ok, "loc_func": False, "patch_gold": False}, resolved=False)]
    s = summarise(bugs)
    st = {x["key"]: (x["pass"], x["n"]) for x in s["stages"]}
    assert st["repro_valid"] == (2, 3) and st["resolved"] == (1, 3)
    h = {x["label"]: (x["pass"], x["n"]) for x in s["handoffs"]}
    assert h["Own tests → Hidden tests (trust)"] == (1, 3)
    assert s["first_break"] == {"repro_valid": 1, "loc_func": 1}
    assert s["false_confidence"] == 2
    page = to_html({"run": "t", **s, "bugs": bugs})
    assert "Pipeline Health" in page and "first broken checkpoint" in page
