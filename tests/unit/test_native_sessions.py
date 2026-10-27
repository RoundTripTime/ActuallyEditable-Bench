"""Native DOCX / ODT editing sessions (no conversion of the edited file), 2026-10-16."""
import io
import zipfile

import docx
import pytest

from formbench.completion.native_docx import NativeDocxSession
from formbench.completion.native_odt import NativeOdtSession
from formbench.ui.human import HumanSession


def make_docx(label="Name:", placeholder="[your name]") -> bytes:
    d = docx.Document()
    d.add_paragraph(f"{label} {placeholder}")
    t = d.add_table(rows=2, cols=2)
    t.style = "Table Grid"
    t.cell(0, 0).text = "Date"
    t.cell(1, 0).text = "Place"
    d.add_paragraph("Signature ____")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def make_odt() -> bytes:
    from odf import table, text
    from odf.opendocument import OpenDocumentText
    doc = OpenDocumentText()
    doc.text.addElement(text.P(text="Name:"))
    t = table.Table(name="T")
    t.addElement(table.TableColumn(numbercolumnsrepeated=2))
    for r in range(2):
        tr = table.TableRow()
        for c in range(2):
            tc = table.TableCell()
            tc.addElement(text.P(text="Label" if c == 0 else ""))
            tr.addElement(tc)
        t.addElement(tr)
    doc.text.addElement(t)
    b = io.BytesIO()
    doc.save(b)
    return b.getvalue()


def _cell_para(h, r, c):
    return h.ir.tables()[0].cell_at(r, c).paras[0].nid


def _click(h, para):
    pg, x, y = h.point_in_para(para)
    return {"action": "click_text", "page": pg, "x": x, "y": y}


def test_docx_edits_stay_native_and_other_parts_are_untouched():
    src = make_docx()
    s = NativeDocxSession.load(src)
    h = HumanSession(s, {"date": "2026-10-16", "name": "Kim"})
    h.run([_click(h, _cell_para(h, 0, 1)), {"action": "input_value", "value_id": "date"}])
    out = s.to_bytes()
    d = docx.Document(io.BytesIO(out))
    assert d.tables[0].cell(0, 1).text == "2026-10-16"
    zi, zo = zipfile.ZipFile(io.BytesIO(src)), zipfile.ZipFile(io.BytesIO(out))
    for n in zi.namelist():
        if n != "word/document.xml":
            assert zi.read(n) == zo.read(n), n
    assert b"formbench" not in zo.read("word/document.xml")          # session ids are never saved


def test_docx_replace_placeholder_by_drag_and_input():
    s = NativeDocxSession.load(make_docx())
    h = HumanSession(s, {"name": "Kim"})
    p = next(p for p in h.ir.paras() if p.text.startswith("Name:"))
    a = p.text.index("[")
    pg, x0, y = next((f.page, f.x + f.xs[a - f.start], f.y + f.h / 2) for f in h.layout.frags if f.para == p.nid)
    _, x1, _ = h.point_in_para(p.nid, "end")
    h.run([{"action": "drag_select_text", "from": [pg, x0, y], "to": [pg, x1 + 0.2, y]}, {"action": "input_value", "value_id": "name"}])
    assert s.text_of(p.nid) == "Name: Kim"
    assert docx.Document(io.BytesIO(s.to_bytes())).paragraphs[0].text == "Name: Kim"


def test_docx_row_insert_enter_and_undo():
    s = NativeDocxSession.load(make_docx())
    h = HumanSession(s, {"v": "x"})
    h.apply(_click(h, _cell_para(h, 1, 1)))
    h.apply({"action": "cmd_insert_row_below"})
    assert len(docx.Document(io.BytesIO(s.to_bytes())).tables[0].rows) == 3
    h.apply({"action": "key_ctrl_z"})
    assert len(docx.Document(io.BytesIO(s.to_bytes())).tables[0].rows) == 2


def test_odt_double_spaces_survive_white_space_processing():
    s = NativeOdtSession.load(make_odt())
    h = HumanSession(s, {"v": "Kim  Lee  "})
    h.run([_click(h, _cell_para(h, 0, 1)), {"action": "input_value", "value_id": "v"}])
    s2 = NativeOdtSession.load(s.to_bytes())
    assert any(p.text == "Kim  Lee  " for p in s2.ir.paras())
    z = zipfile.ZipFile(io.BytesIO(s.to_bytes()))
    assert z.infolist()[0].filename == "mimetype" and z.infolist()[0].compress_type == zipfile.ZIP_STORED


def test_view_only_nodes_are_not_editable():
    s = NativeDocxSession.load(make_docx())
    view_only = [p.nid for p in s.ir.paras() if int(p.nid.split("n")[-1]) >= 5_000_000]
    if not view_only:
        pytest.skip("no view-only paragraph in this fixture")
    from formbench.completion.editor_backends import OpError
    with pytest.raises(OpError):
        s.apply({"op": "focus", "target": view_only[0]})
