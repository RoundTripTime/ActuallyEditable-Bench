"""Operator contract: every operator is fully specified, executable, and passes its own declared test cases."""
import pytest

from conftest import ASSETS, resolve_selectors
from formbench import fixtures
from formbench.completion.editor_backends import FORBIDDEN_OPS, EditorSession, OpError
from formbench.document_ir.normalize import para_text
from formbench.formats.hwpx import HwpxPackage
from formbench.planning.costs import CostModel
from formbench.planning.operators import REQUIRED_KEYS, load_registry, validate_registry

REG = load_registry()
CASES = [(name, i, tc) for name, o in REG["operators"].items() for i, tc in enumerate(o.spec["test_cases"])]


def test_registry_is_complete_and_matches_backend():
    assert validate_registry() == []
    for o in REG["operators"].values():
        assert all(k in o.spec for k in REQUIRED_KEYS)


def _measure(sess):
    ir = sess.ir
    shapes = ir.shapes()
    return {"top": sum(len(s.paras) for s in ir.sections), "tables": len(ir.tables()), "textbox": sum(1 for s in shapes if s.is_textbox),
            "shape": sum(1 for s in shapes if not s.is_textbox and s.kind != "pic"), "pic": sum(1 for s in shapes if s.kind == "pic"),
            "rows": {t.nid: t.n_rows for t in ir.tables()}, "cols": {t.nid: t.n_cols for t in ir.tables()}, "cells": {t.nid: len(t.cells) for t in ir.tables()},
            "box": {b.nid: (b.x, b.y, b.w, b.h) for b in sess.layout.boxes if b.kind != "para" and b.nid}}


@pytest.mark.parametrize("name,i,tc", CASES, ids=[f"{n}-{i}" for n, i, _ in CASES])
def test_operator_declared_case(name, i, tc):
    sess = EditorSession.load(fixtures.build(tc["fixture"]), ASSETS)
    for op in resolve_selectors(tc.get("setup", []), sess):
        sess.apply(op)
    op = resolve_selectors(tc["op"], sess)
    exp = resolve_selectors(tc["expect"], sess)
    before = _measure(sess)
    lens = {p.nid: len(p.text) for p in sess.ir.paras()}
    key_before = sess.to_bytes() if "error" in exp else None
    if "error" in exp:
        with pytest.raises(OpError) as ei:
            sess.apply(op)
        assert ei.value.code == exp["error"]
        assert sess.to_bytes() == key_before, "a failed operator must leave the document untouched (atomicity)"
        return
    sess.apply(op)
    after = _measure(sess)
    if "caret" in exp:
        assert sess.caret[0] == exp["caret"]
    if "para_text" in exp:
        assert para_text(sess.el(exp["para_text"][0])) == exp["para_text"][1]
    if "para_len_delta" in exp:
        assert len(para_text(sess.el(exp["para_len_delta"][0]))) - lens[exp["para_len_delta"][0]] == exp["para_len_delta"][1]
    for k, mk in (("top_para_delta", "top"), ("table_delta", "tables"), ("textbox_delta", "textbox"), ("shape_delta", "shape"), ("pic_delta", "pic")):
        if k in exp:
            assert after[mk] - before[mk] == exp[k]
    for k, mk in (("row_delta", "rows"), ("col_delta", "cols"), ("cell_delta", "cells")):
        if k in exp:
            assert after[mk][exp[k][0]] - before[mk][exp[k][0]] == exp[k][1]
    if "box_dy" in exp:
        assert abs(after["box"][exp["box_dy"][0]][1] - before["box"][exp["box_dy"][0]][1] - exp["box_dy"][1]) < 0.05
    if "box_size" in exp:
        b = after["box"][exp["box_size"][0]]
        assert abs(b[2] - exp["box_size"][1]) < 0.05 and b[3] >= exp["box_size"][2] - 0.05
    # every successful edit must still yield a loadable, reference-consistent file
    pkg = HwpxPackage.load(sess.to_bytes())
    assert not [x for x in pkg.validate_refs() if x["severity"] == "error"]


@pytest.mark.parametrize("name", sorted(FORBIDDEN_OPS))
def test_unbounded_commands_are_forbidden(name):
    sess = EditorSession.load(fixtures.build("simple_form"))
    with pytest.raises(OpError) as ei:
        sess.apply({"op": name, "xml": "<x/>"})
    assert ei.value.code == "forbidden_operation"


def test_unknown_operator_is_unsupported_not_ignored():
    sess = EditorSession.load(fixtures.build("simple_form"))
    with pytest.raises(OpError) as ei:
        sess.apply({"op": "teleport"})
    assert ei.value.code == "unsupported_operation"


def test_undo_restores_exact_state_and_costs_are_integers():
    sess = EditorSession.load(fixtures.build("simple_form"))
    t = sess.ir.tables()[0]
    base = sess.to_bytes()
    sess.apply({"op": "table_insert_row", "table": t.nid, "after_row": 0})
    sess.apply({"op": "focus_cell", "table": t.nid, "row": 1, "col": 1})
    sess.apply({"op": "type_text", "text": "x\ny\tz"})
    for _ in range(3):
        sess.apply({"op": "undo"})
    assert sess.to_bytes() == base
    for scheme in REG["schemes"]:
        cm = CostModel(scheme)
        assert all(isinstance(cm.base(n), int) and cm.base(n) >= 0 for n in REG["operators"])
    assert CostModel("klm_ms").cost({"op": "type_text", "text": "abc"}) == 1750 + 3 * 280


def test_text_ops_handle_special_characters_and_runs():
    sess = EditorSession.load(fixtures.build("simple_form"))
    nid = sess.ir.tables()[0].cell_at(0, 1).paras[0].nid
    sess.apply({"op": "focus", "target": nid})
    sess.apply({"op": "type_text", "text": "가나\n다\t라마"})
    assert para_text(sess.el(nid)) == "가나\n다\t라마"
    sess.apply({"op": "select_range", "start": 1, "end": 5})
    sess.apply({"op": "set_char_format", "props": {"bold": True, "size": 13}})
    assert para_text(sess.el(nid)) == "가나\n다\t라마"
    sess.apply({"op": "select_range", "start": 2, "end": 4})
    sess.apply({"op": "delete_selection"})
    assert para_text(sess.el(nid)) == "가나\t라마"
    p = [x for x in HwpxPackage.load(sess.to_bytes()).sections[0].iter() if isinstance(x.tag, str) and x.tag.endswith("}p")]
    assert p
