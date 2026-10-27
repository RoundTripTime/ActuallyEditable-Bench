"""Native DOCX editing session (no conversion of the edited document).

The document that is edited, saved and re-opened is the DOCX itself (word/document.xml changed in place, every other
part copied byte for byte). For hit testing and layout the human-action layer needs the reference renderer, which
reads HWPX; a *view* is therefore derived after every change by the DOCX->HWPX converter, with each view node
carrying the session id of the native element it came from (paragraph, table, cell, drawing). Operations address
those ids and are executed on the native XML; the view is only ever read.

Text model of a paragraph = the converter's own text: w:t / w:tab ('\\t') / w:br, w:cr ('\\n') in document order,
excluding text-box content (which belongs to the text box's own paragraphs). Operations not implemented natively
raise ``OpError('unsupported_operation')`` -- never silently approximated."""
from __future__ import annotations

import copy
import io
import zipfile

from lxml import etree

from ..document_ir.layout import layout_document
from ..document_ir.normalize import NEXT, NID, STAMP_TAGS, build_ir
from ..formats.docx import W, WP, _Conv, _secure
from ..formats.docx import own_tokens as _own_tokens
from ..formats.hwpx import HwpxPackage
from .editor_backends import FORBIDDEN_OPS, OpError

TEXT_TAGS = (W + "t", W + "tab", W + "br", W + "cr")
ID_TAGS = (W + "p", W + "tbl", W + "tc", W + "drawing")
VIEW_ONLY_BASE = 5_000_000
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _run_of(e):
    r = e.getparent()
    while r is not None and r.tag != W + "r":
        r = r.getparent()
    return r


class NativeDocxSession:
    backend = "native_docx_xml"
    version = "1.0"
    fmt = "docx"

    def __init__(self, data: bytes):
        self.data = data
        z = zipfile.ZipFile(io.BytesIO(data))
        self.root = _secure(z.read("word/document.xml"))
        self._next = 0
        for e in self.root.iter(*ID_TAGS):
            e.set(NID, f"s0n{self._next}")
            self._next += 1
        self.caret: tuple[str, int] | None = None
        self.selection: tuple[str, int, int] | None = None
        self.obj_selection: tuple[str, ...] = ()
        self.history: list[dict] = []
        self._undo: list = []
        self._view = self._ir = self._layout = None
        self.keep_undo = True

    @classmethod
    def load(cls, data: bytes) -> NativeDocxSession:
        return cls(data)

    # ------------------------------------------------------------------ view
    @property
    def view(self) -> HwpxPackage:
        if self._view is None:
            pkg, info = _Conv(self.data, root=self.root).build()
            n = VIEW_ONLY_BASE
            for root in pkg.sections:
                for e in root.iter():
                    if e.tag in STAMP_TAGS and e.get(NID) is None:
                        e.set(NID, f"s0n{n}")                 # view-only node (e.g. a table's anchor paragraph)
                        n += 1
                root.set(NEXT, str(n))
            self._view, self.view_info = pkg, info
        return self._view

    @property
    def ir(self):
        if self._ir is None:
            self._ir = build_ir(self.view)
        return self._ir

    @property
    def layout(self):
        if self._layout is None:
            self._layout = layout_document(self.ir)
        return self._layout

    def _invalidate(self):
        self._view = self._ir = self._layout = None

    # ------------------------------------------------------------------ lookup
    def native(self, nid: str, tag: str | None = None):
        r = self.root.xpath(".//*[@f:nid=$v]", namespaces={"f": NID[1:].split("}")[0]}, v=nid)
        if not r:
            if nid.startswith("s0n") and int(nid[3:]) >= VIEW_ONLY_BASE:
                raise OpError("protected_region", f"{nid} exists only in the view (no native element)")
            raise OpError("target_not_found", nid)
        if tag and r[0].tag != W + tag:
            raise OpError("target_wrong_type", f"{nid} is {etree.QName(r[0]).localname}")
        return r[0]

    def text_of(self, nid: str) -> str:
        return "".join(t for _, _, t in _own_tokens(self.native(nid, "p")))

    def _new_ids(self, el):
        for e in el.iter(*ID_TAGS):
            e.set(NID, f"s0n{self._next}")
            self._next += 1

    # ------------------------------------------------------------------ snapshots / apply
    def snapshot(self, with_header: bool = True):
        return (copy.deepcopy(self.root), self.caret, self.selection, self.obj_selection, len(self.history), self._next)

    def restore(self, snap) -> None:
        root, self.caret, self.selection, self.obj_selection, hlen, self._next = snap
        self.root = copy.deepcopy(root)
        del self.history[hlen:]
        self._invalidate()

    def apply(self, op: dict) -> None:
        name = op.get("op")
        if name in FORBIDDEN_OPS:
            raise OpError("forbidden_operation", name)
        fn = getattr(self, "_op_" + str(name), None)
        if fn is None:
            raise OpError("unsupported_operation", f"{name} (native DOCX backend)")
        if name == "undo":
            fn(op)
            return
        snap = self.snapshot()
        try:
            fn(op)
        except OpError:
            self.restore(snap)
            raise
        except Exception as e:  # noqa: BLE001
            self.restore(snap)
            raise OpError("backend_failure", f"{type(e).__name__}: {e}") from e
        if self.keep_undo:
            self._undo.append(snap)
        self.history.append(dict(op))
        self._invalidate()

    # ------------------------------------------------------------------ text primitives
    def _split_at(self, p, k: int):
        """Make offset k a run boundary of paragraph p; return (parent, index) where a new run is inserted to
        place text at k, and the run whose formatting the new text takes (preceding run, else following)."""
        toks = _own_tokens(p)
        pos = 0
        for i, (e, n, t) in enumerate(toks):
            if pos < k < pos + n:                       # inside a w:t: split the element and its run
                cut = k - pos
                e2 = copy.deepcopy(e)
                e.text, e2.text = t[:cut], t[cut:]
                e.set(XML_SPACE, "preserve")
                e2.set(XML_SPACE, "preserve")
                e.addnext(e2)
                self._split_run_after(e)
                run = _run_of(e)
                return run.getparent(), run.getparent().index(run) + 1, run
            pos += n
            if pos == k:
                self._split_run_after(e)
                run = _run_of(e)
                return run.getparent(), run.getparent().index(run) + 1, run
        if k != 0 and k != pos:
            raise OpError("offset_out_of_range", f"{k} not in [0,{pos}]")
        if toks:                                        # k == 0: before the first run with text
            run = _run_of(toks[0][0])
            return run.getparent(), run.getparent().index(run), run
        ppr = p.find(W + "pPr")
        return p, len(p), None if ppr is None else ppr

    def _split_run_after(self, e) -> None:
        """Move everything after element e inside its run into a new run right after it."""
        run = _run_of(e)
        anc = e
        while anc.getparent() is not run:
            anc = anc.getparent()
        later = [c for c in run if run.index(c) > run.index(anc)]
        if not later:
            return
        r2 = etree.Element(W + "r")
        rpr = run.find(W + "rPr")
        if rpr is not None:
            r2.append(copy.deepcopy(rpr))
        for c in later:
            r2.append(c)
        run.addnext(r2)

    def _new_run(self, text: str, fmt_src):
        r = etree.Element(W + "r")
        rpr = None
        if fmt_src is not None:
            rpr = fmt_src.find(W + "rPr") if fmt_src.tag == W + "r" else fmt_src.find(W + "rPr")
        if rpr is not None:
            r.append(copy.deepcopy(rpr))
        buf = ""
        for ch in text:
            if ch in "\t\n":
                if buf:
                    t = etree.SubElement(r, W + "t")
                    t.text = buf
                    t.set(XML_SPACE, "preserve")
                    buf = ""
                etree.SubElement(r, W + ("tab" if ch == "\t" else "br"))
            else:
                buf += ch
        if buf:
            t = etree.SubElement(r, W + "t")
            t.text = buf
            t.set(XML_SPACE, "preserve")
        return r

    def _delete_range(self, p, a: int, b: int) -> None:
        if a >= b:
            return
        self._split_at(p, b)
        self._split_at(p, a)
        pos = 0
        for e, n, t in _own_tokens(p):
            if a <= pos and pos + n <= b:
                run = _run_of(e)
                e.getparent().remove(e)
                if run is not None and not [c for c in run if c.tag != W + "rPr"]:
                    run.getparent().remove(run)
            pos += n

    def _caret_para(self):
        if self.caret is None:
            raise OpError("no_caret", "focus a text container first")
        return self.native(self.caret[0], "p")

    # ------------------------------------------------------------------ operations (same names as the HWPX backend)
    def _op_focus(self, op):
        p = self.native(op["target"], "p")
        n = len(self.text_of(op["target"]))
        off = n if op.get("offset", "end") == "end" else int(op["offset"])
        if not 0 <= off <= n:
            raise OpError("offset_out_of_range", f"{off} not in [0,{n}]")
        self.caret, self.selection, self.obj_selection = (op["target"], off), None, ()

    def _op_move_caret(self, op):
        self._caret_para()
        off = int(op["offset"])
        if not 0 <= off <= len(self.text_of(self.caret[0])):
            raise OpError("offset_out_of_range")
        self.caret, self.selection = (self.caret[0], off), None

    def _op_select_range(self, op):
        self._caret_para()
        a, b = int(op["start"]), int(op["end"])
        if not 0 <= a < b <= len(self.text_of(self.caret[0])):
            raise OpError("offset_out_of_range")
        self.selection = (self.caret[0], a, b)
        self.caret = (self.caret[0], b)

    def _op_type_text(self, op):
        text = op["text"]
        if not text:
            raise OpError("empty_text")
        p = self._caret_para()
        nid, off = self.caret
        if self.selection:
            _, a, b = self.selection
            self._delete_range(p, a, b)
            off, self.selection = a, None
        parent, idx, fmt = self._split_at(p, off)
        if parent is p and fmt is not None and fmt.tag == W + "pPr":   # empty paragraph: format of the paragraph mark
            r = self._new_run(text, None)
            mark = fmt.find(W + "rPr")
            if mark is not None:
                rp = copy.deepcopy(mark)
                for bad in rp.findall(W + "ins") + rp.findall(W + "del"):
                    rp.remove(bad)
                r.insert(0, rp)
            p.append(r)
        else:
            parent.insert(idx, self._new_run(text, fmt))
        self.caret = (nid, off + len(text))

    def _op_delete_selection(self, op):
        if not self.selection:
            raise OpError("no_selection")
        nid, a, b = self.selection
        self._delete_range(self.native(nid, "p"), a, b)
        self.caret, self.selection = (nid, a), None

    def _op_split_paragraph(self, op):
        p = self._caret_para()
        nid, off = self.caret
        parent, idx, _ = self._split_at(p, off)
        if parent is not p:
            raise OpError("unsupported_operation", "split inside a hyperlink / content control")
        new = etree.Element(W + "p")
        ppr = p.find(W + "pPr")
        if ppr is not None:
            new.append(copy.deepcopy(ppr))
        for c in [c for c in p if p.index(c) >= idx and c.tag != W + "pPr"]:
            new.append(c)
        p.addnext(new)
        self._new_ids(new)
        self.caret, self.selection = (new.get(NID), 0), None

    def _rows(self, tbl):
        return [tr for tr in tbl if tr.tag == W + "tr"]

    def _op_table_insert_row(self, op):
        tbl = self.native(op["table"], "tbl")
        rows = self._rows(tbl)
        r = int(op["after_row"])
        if not 0 <= r < len(rows):
            raise OpError("row_out_of_range", f"{r} / {len(rows)}")
        new = copy.deepcopy(rows[r])
        for tc in new.iter(W + "tc"):
            ps = [c for c in tc if c.tag == W + "p"]
            for extra in ps[1:]:
                tc.remove(extra)
            for c in [c for c in tc if c.tag not in (W + "tcPr", W + "p")]:
                tc.remove(c)
            if ps:
                for c in [c for c in ps[0] if c.tag != W + "pPr"]:
                    ps[0].remove(c)
            vm = tc.find(W + "tcPr/" + W + "vMerge")
            if vm is not None:
                vm.getparent().remove(vm)
        rows[r].addnext(new)
        self._new_ids(new)
        self.caret = self.selection = None

    def _op_table_delete_row(self, op):
        tbl = self.native(op["table"], "tbl")
        rows = self._rows(tbl)
        r = int(op["row"])
        if not 0 <= r < len(rows) or len(rows) == 1:
            raise OpError("row_out_of_range")
        tbl.remove(rows[r])
        self.caret = self.selection = None

    def _op_set_char_format(self, op):
        if not self.selection:
            raise OpError("no_selection")
        props = op.get("props") or {}
        if set(props) - {"bold"}:
            raise OpError("unsupported_operation", "native DOCX formatting: bold only")
        nid, a, b = self.selection
        p = self.native(nid, "p")
        self._split_at(p, b)
        self._split_at(p, a)
        pos = 0
        for e, n, t in _own_tokens(p):
            if a <= pos and pos + n <= b:
                run = _run_of(e)
                rpr = run.find(W + "rPr")
                if rpr is None:
                    rpr = etree.Element(W + "rPr")
                    run.insert(0, rpr)
                bt = rpr.find(W + "b")
                if bt is None:
                    bt = etree.SubElement(rpr, W + "b")
                if not props["bold"]:
                    bt.set(W + "val", "0")
            pos += n

    def _op_select_objects(self, op):
        for nid in op["targets"]:
            self.native(nid, "drawing")
        self.obj_selection = tuple(op["targets"])
        self.caret = self.selection = None

    def _op_move_selection(self, op):
        if not self.obj_selection:
            raise OpError("no_selection")
        for nid in self.obj_selection:
            dr = self.native(nid, "drawing")
            anchor = dr.find(WP + "anchor")
            if anchor is None:
                raise OpError("unsupported_operation", "inline object cannot be dragged to a position")
            for tag, d in ((WP + "positionH", float(op.get("dx", 0))), (WP + "positionV", float(op.get("dy", 0)))):
                off = anchor.find(tag + "/" + WP + "posOffset") if anchor.find(tag) is not None else None
                if off is None:
                    raise OpError("unsupported_operation", "aligned (not offset) anchor")
                off.text = str(int(round(int(off.text) + d * 12700)))

    def _op_undo(self, op):
        if not self._undo:
            raise OpError("nothing_to_undo")
        snap = self._undo.pop()
        self.restore(snap)
        self.history.append({"op": "undo"})

    # ------------------------------------------------------------------ save
    def to_bytes(self) -> bytes:
        root = copy.deepcopy(self.root)
        for e in root.iter():
            if NID in e.attrib:
                del e.attrib[NID]
        etree.cleanup_namespaces(root)
        doc = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        zin = zipfile.ZipFile(io.BytesIO(self.data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            for info in zin.infolist():
                zo.writestr(info, doc if info.filename == "word/document.xml" else zin.read(info.filename))
        return out.getvalue()

    def save(self, dst) -> str:
        from pathlib import Path
        Path(dst).write_bytes(self.to_bytes())
        return str(dst)
