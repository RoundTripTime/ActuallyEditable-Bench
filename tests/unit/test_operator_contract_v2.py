"""Human-action operator contract v2 must rest on verified knowledge-base facts (2026-10-16)."""
import copy

from formbench.knowledge import contract as KC


def test_contract_v2_is_consistent_with_kb():
    out = KC.check()
    assert out["errors"] == [], out["errors"]
    assert out["version"].startswith("2.")


def test_common_admission_over_a_gap_is_rejected():
    c = copy.deepcopy(KC.load_contract())
    op = next(o for o in c["operators"] if o["name"] == "cmd_merge_cells")
    op["admission"] = "common"
    assert any("cmd_merge_cells" in e for e in KC.check(c)["errors"])


def test_citing_an_unknown_fact_is_rejected():
    c = copy.deepcopy(KC.load_contract())
    c["operators"][0]["facts"] = ["word.does_not_exist"]
    assert any("unknown fact" in e for e in KC.check(c)["errors"])


def test_text_input_is_one_value_one_area():
    op = next(o for o in KC.load_contract()["operators"] if o["name"] == "input_value")
    assert op["unit_cost"]["actions"] == 1 and "value_from_contract" in op["guard"] and "no_clipping" in op["guard"]
