"""ODF white-space processing must not change visible text around a deletion (source fix: a literal space that would
collapse after the deletion is kept as text:s, as editors do)."""
import io
import zipfile

import pytest

from formbench.completion.native_odt import NativeOdtSession

CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3"><office:body><office:text>
<text:p>{}</text:p></office:text></office:body></office:document-content>"""


def _odt(par: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", CONTENT.format(par))
        z.writestr("styles.xml", '<?xml version="1.0"?><office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3"/>')
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
    return b.getvalue()


@pytest.mark.parametrize("text,a,b,typed,want", [
    ("example as a guide.]", 0, 10, "Create", "Create a guide.]"),       # deleted prefix: following space survives
    ("Hello world", 6, 11, None, "Hello "),                               # deleted suffix: trailing space survives
    ("a b c", 2, 3, None, "a  c"),                                         # two spaces meet
])
def test_delete_keeps_visible_spaces(text, a, b, typed, want):
    s = NativeOdtSession.load(_odt(text))
    nid = next(iter(s.ir.paras())).nid
    assert s.text_of(nid) == text
    s.selection, s.caret = (nid, a, b), (nid, b)
    if typed:
        s.apply({"op": "type_text", "text": typed})
    else:
        s.apply({"op": "delete_selection"})
    assert s.text_of(nid) == want
    s2 = NativeOdtSession.load(s.to_bytes())
    assert s2.text_of(next(iter(s2.ir.paras())).nid) == want


@pytest.mark.parametrize("text,at,typed,want", [
    ("", 0, "이도령 ", "이도령 "),                 # typed value ends with a space at the paragraph end
    ("abc", 3, "x  ", "abcx  "),
    ("abc", 1, "x ", "ax bc"),
])
def test_type_keeps_trailing_spaces(text, at, typed, want):
    s = NativeOdtSession.load(_odt(text))
    nid = next(iter(s.ir.paras())).nid
    s.caret, s.selection = (nid, at), None
    s.apply({"op": "type_text", "text": typed})
    assert s.text_of(nid) == want
    s2 = NativeOdtSession.load(s.to_bytes())
    assert s2.text_of(next(iter(s2.ir.paras())).nid) == want


FRAMES = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.3"><office:body><office:text>
{}</office:text></office:body></office:document-content>"""


def _frame(page, text):
    return (f'<draw:frame text:anchor-type="page" text:anchor-page-number="{page}" svg:x="100pt" svg:y="100pt" svg:width="200pt" '
            f'svg:height="20pt"><draw:text-box><text:p>{text}</text:p></draw:text-box></draw:frame>')


def _odt_body(body: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", FRAMES.format(body))
        z.writestr("styles.xml", '<?xml version="1.0"?><office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3"/>')
    return b.getvalue()


@pytest.mark.parametrize("body", [
    "<text:p>" + _frame(1, "one") + _frame(2, "two") + _frame(3, "three") + "</text:p>",   # in-house writer style
    _frame(1, "one") + _frame(2, "two") + _frame(3, "three") + "<text:p/>",                 # LibreOffice PDF import style
])
def test_numbered_page_frames_land_on_their_pages(body):
    from formbench.ui.human import HumanSession
    s = NativeOdtSession.load(_odt_body(body))
    lay = HumanSession(s, {}).layout
    pages = {f.text: f.page for f in lay.frags if f.text in ("one", "two", "three")}
    assert pages == {"one": 0, "two": 1, "three": 2}
