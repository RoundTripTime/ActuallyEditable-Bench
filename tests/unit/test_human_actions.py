"""Human-action executor (configs/edit_operators_v2.yaml): gestures on the rendered page, common-subset guards,
one contract value per text input (user decisions 2026-10-16)."""
import pytest

from formbench import fixtures
from formbench.completion.editor_backends import EditorSession
from formbench.ui.human import ActionError, HumanSession, keystrokes


def _session(data: bytes, values=None) -> HumanSession:
    return HumanSession(EditorSession.load(data), values or {})


def _cell_para(h: HumanSession, t: int, r: int, c: int) -> str:
    return h.ir.tables()[t].cell_at(r, c).paras[0].nid


def _click(h: HumanSession, para: str) -> dict:
    pg, x, y = h.point_in_para(para)
    return {"action": "click_text", "page": pg, "x": x, "y": y}


def test_click_into_empty_cell_then_one_input_costs_two_actions():
    h = _session(fixtures.simple_form(), {"name": "가상인"})
    target = _cell_para(h, 0, 0, 1)
    h.run([_click(h, target), {"action": "input_value", "value_id": "name"}])
    assert h.ir.nodes[target].text == "가상인"
    assert h.totals()["actions"] == 2
    assert [o["op"] for r in h.log for o in r["backend_ops"]] == ["focus", "type_text"]


def test_tab_moves_only_into_an_empty_cell():
    h = _session(fixtures.simple_form(), {"a": "x"})
    h.apply(_click(h, _cell_para(h, 0, 0, 1)))
    with pytest.raises(ActionError) as e:            # previous cell holds the label: Word would select it
        h.apply({"action": "key_shift_tab_prev_cell"})
    assert e.value.code == "destination_cell_not_empty"
    h.apply(_click(h, _cell_para(h, 0, 1, 0)))
    h.apply({"action": "key_tab_next_cell"})            # (1,1) is empty: admitted
    assert h.ed.caret[0] == _cell_para(h, 0, 1, 1)


def test_text_input_must_be_a_contract_value_in_order():
    h = _session(fixtures.simple_form(), {"addr": "서울시 가상구 1"})
    h.apply(_click(h, _cell_para(h, 0, 2, 1)))
    with pytest.raises(ActionError) as e:
        h.apply({"action": "input_value", "value_id": "not_in_contract"})
    assert e.value.code == "value_not_in_contract"
    with pytest.raises(ActionError) as e:
        h.apply({"action": "input_value", "value_id": "addr", "segment": [3, 6]})
    assert e.value.code == "segment_out_of_order"


def test_value_split_across_line_boxes_costs_one_input_and_one_click_per_box():
    value = "가" * 30 + "나" * 30
    h = _session(fixtures.fragmented_longtext_form(n_lines=4, locked=False), {"motive": value})
    boxes = [s for s in h.ir.shapes() if s.is_textbox and not s.paras[0].text][:2]
    plan = []
    for i, (s, e) in enumerate([(0, 30), (30, 60)]):
        plan += [_click(h, boxes[i].paras[0].nid), {"action": "input_value", "value_id": "motive", "segment": [s, e]}]
    h.run(plan)
    assert h.totals()["actions"] == 4
    assert "".join(b.paras[0].text for b in [h.ir.nodes[x.nid] for x in boxes]) == value


def test_text_that_would_grow_a_text_box_is_not_common():
    h = _session(fixtures.textbox_form(1), {"v": "아주 긴 값 " * 40})
    box = [s for s in h.ir.shapes() if s.is_textbox and not s.paras[0].text][0]
    h.apply(_click(h, box.paras[0].nid))
    before = h.ed.to_bytes()
    with pytest.raises(ActionError) as e:
        h.apply({"action": "input_value", "value_id": "v"})
    assert e.value.code in ("textbox_would_grow_or_clip", "text_clipped")
    assert h.ed.to_bytes() == before                     # rolled back


def test_tab_in_last_cell_adds_a_row_and_leaves_the_caret_unknown():
    h = _session(fixtures.repeat_form(rows=1, trailing=True), {"v": "2026"})
    t = h.ir.tables()[0]
    rows0 = t.n_rows
    last = sorted(t.cells, key=lambda c: (c.row, c.col))[-1]
    h.apply(_click(h, last.paras[0].nid))
    h.apply({"action": "key_tab_last_cell"})
    assert h.ir.tables()[0].n_rows == rows0 + 1
    with pytest.raises(ActionError) as e:
        h.apply({"action": "input_value", "value_id": "v"})
    assert e.value.code == "caret_unknown"


def test_click_on_a_picture_does_not_place_a_caret():
    h = _session(fixtures.picture_form(fixtures.tiny_png(), px=(10, 10)))
    pic = next(b for b in h.layout.boxes if b.kind == "pic")
    with pytest.raises(ActionError) as e:
        h.apply({"action": "click_text", "page": pic.page, "x": pic.x + pic.w / 2, "y": pic.y + pic.h / 2})
    assert e.value.code == "hit_not_text"


def test_unsupported_actions_are_refused():
    h = _session(fixtures.simple_form())
    with pytest.raises(ActionError) as e:
        h.apply({"action": "toggle_checkbox"})
    assert e.value.code == "unsupported_operation"


def test_placeholder_is_replaced_by_selecting_it_and_typing_the_value():
    h = _session(fixtures.long_text_form(placeholder="○○○"), {"v": "가상인"})
    para = next(p for p in h.ir.paras() if p.text == "○○○")
    pg, x0, y = h.point_in_para(para.nid, "start")
    _, x1, _ = h.point_in_para(para.nid, "end")
    h.run([{"action": "drag_select_text", "from": [pg, x0 - 0.3, y], "to": [pg, x1 + 0.3, y]},
           {"action": "input_value", "value_id": "v"}])
    assert h.ir.nodes[para.nid].text == "가상인" and h.totals()["actions"] == 2


def test_delete_and_enter_keys():
    h = _session(fixtures.simple_form(), {"v": "ab"})
    cell = _cell_para(h, 0, 0, 1)
    h.run([_click(h, cell), {"action": "input_value", "value_id": "v"}, {"action": "key_delete", "key": "BackSpace"}])
    assert h.ir.nodes[cell].text == "a"
    h.apply({"action": "key_enter"})
    assert len(h.ir.tables()[0].cell_at(0, 1).paras) == 2


def test_keystrokes_two_beolsik():
    assert keystrokes("가상인") == 8           # ㄱㅏ ㅅㅏㅇ ㅇㅣㄴ
    assert keystrokes("왜") == 3               # ㅇ ㅗ ㅐ
    assert keystrokes("Ab") == 3               # Shift+a, b
