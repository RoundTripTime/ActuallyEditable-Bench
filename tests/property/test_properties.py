"""Property tests: random operator sequences never corrupt a document, undo is exact, layout and save are
deterministic, and load→save→load keeps the semantic content."""
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from formbench import fixtures
from formbench.completion.editor_backends import EditorSession, OpError
from formbench.document_ir.layout import layout_document
from formbench.document_ir.normalize import build_ir, semantic_fingerprint
from formbench.formats.hwpx import HwpxPackage

FIX = {n: fixtures.build(n) for n in ("simple_form", "merged_form", "textbox_form", "repeat_form")}


def _random_op(draw, sess: EditorSession) -> dict:
    ir = sess.ir
    paras = [p.nid for p in ir.paras()]
    tables = ir.tables()
    shapes = [s for s in ir.shapes()]
    kind = draw(st.sampled_from(["focus", "type", "select", "delete", "format", "split", "row", "delrow", "col", "merge", "box", "move", "resize", "undo", "line"]))
    if kind == "focus":
        return {"op": "focus", "target": draw(st.sampled_from(paras)), "offset": draw(st.sampled_from([0, "end"]))}
    if kind == "type":
        return {"op": "type_text", "text": draw(st.text(alphabet="가나다ab \n\t", min_size=1, max_size=6))}
    if kind == "select":
        a = draw(st.integers(0, 3))
        return {"op": "select_range", "start": a, "end": a + draw(st.integers(1, 3))}
    if kind == "delete":
        return {"op": "delete_selection"}
    if kind == "format":
        return {"op": "set_char_format", "props": {"bold": draw(st.booleans()), "size": draw(st.sampled_from([8, 10, 14]))}}
    if kind == "split":
        return {"op": "split_paragraph"}
    if kind in ("row", "delrow", "col", "merge") and tables:
        t = draw(st.sampled_from(tables))
        if kind == "row":
            return {"op": "table_insert_row", "table": t.nid, "after_row": draw(st.integers(0, max(0, t.n_rows - 1)))}
        if kind == "delrow":
            return {"op": "table_delete_row", "table": t.nid, "row": draw(st.integers(0, max(0, t.n_rows - 1)))}
        if kind == "col":
            return {"op": "table_insert_column", "table": t.nid, "after_col": draw(st.integers(0, max(0, t.n_cols - 1)))}
        r, c = draw(st.integers(0, max(0, t.n_rows - 1))), draw(st.integers(0, max(0, t.n_cols - 1)))
        return {"op": "table_merge_cells", "table": t.nid, "row0": r, "col0": c, "row1": min(t.n_rows - 1, r + draw(st.integers(0, 1))),
                "col1": min(t.n_cols - 1, c + draw(st.integers(0, 1)))}
    if kind == "box":
        return {"op": "create_textbox", "page": 0, "x": draw(st.integers(60, 300)), "y": draw(st.integers(300, 600)), "w": 120, "h": 30}
    if kind in ("move", "resize") and shapes:
        s = draw(st.sampled_from(shapes))
        if kind == "move":
            return {"op": "move_object", "target": s.nid, "dx": draw(st.integers(-20, 20)), "dy": draw(st.integers(-20, 20))}
        return {"op": "resize_object", "target": s.nid, "w": draw(st.integers(20, 200)), "h": draw(st.integers(10, 80))}
    if kind == "line":
        return {"op": "create_line", "page": 0, "x0": 60, "y0": draw(st.integers(200, 700)), "x1": 300, "y1": draw(st.integers(200, 700))}
    return {"op": "undo"}


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(st.data())
def test_random_operator_sequences_keep_documents_valid_and_undo_is_exact(data):
    name = data.draw(st.sampled_from(sorted(FIX)))
    sess = EditorSession.load(FIX[name])
    snapshots = [sess.to_bytes()]
    for _ in range(data.draw(st.integers(1, 8))):
        op = _random_op(data.draw, sess)
        before = sess.to_bytes()
        try:
            sess.apply(op)
        except OpError:
            assert sess.to_bytes() == before                      # atomic failure
            continue
        if op["op"] == "undo":
            snapshots.pop()
            assert sess.to_bytes() == snapshots[-1]               # undo restores the exact previous document
        else:
            snapshots.append(sess.to_bytes())
        pkg = HwpxPackage.load(sess.to_bytes())
        assert not [i for i in pkg.validate_refs() if i["severity"] == "error"]
        lay = layout_document(build_ir(pkg))
        assert lay.n_pages >= 1
        for t in build_ir(pkg).tables():                           # table grid stays consistent: no overlapping cells, all inside the grid
            seen = set()
            for c in t.cells:
                for r in range(c.row, c.row + c.rowspan):
                    for k in range(c.col, c.col + c.colspan):
                        assert (r, k) not in seen and r < t.n_rows and k < t.n_cols
                        seen.add((r, k))
    while len(snapshots) > 1:
        sess.apply({"op": "undo"})
        snapshots.pop()
    assert sess.to_bytes() == snapshots[0]


def test_save_layout_and_fingerprint_are_deterministic_and_roundtrip_stable():
    for name, data in FIX.items():
        a, b = HwpxPackage.load(data), HwpxPackage.load(data)
        assert a.to_bytes() == b.to_bytes() == HwpxPackage.load(a.to_bytes()).to_bytes(), name
        fa = semantic_fingerprint(build_ir(a))
        assert fa == semantic_fingerprint(build_ir(HwpxPackage.load(a.to_bytes())))
        la, lb = layout_document(build_ir(a)), layout_document(build_ir(b))
        assert [(f.page, round(f.x, 3), round(f.y, 3), f.text) for f in la.frags] == [(f.page, round(f.x, 3), round(f.y, 3), f.text) for f in lb.frags]
        for (n1, d1, _), (n2, d2, _) in zip(a.entries, HwpxPackage.load(a.to_bytes()).entries):
            assert n1 == n2 and d1 == d2                           # untouched parts are byte-identical after load -> save -> load
