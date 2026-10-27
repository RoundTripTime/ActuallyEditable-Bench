"""Native HWPX editing backend: executable implementations of the atomic edit operators.

``EditorSession.apply(op)`` is atomic: a failed precondition raises ``OpError`` and leaves the document
untouched. Every successful op can be reverted with ``undo``. Targets are session node ids (``nid``), which are
deterministic for a given input file, so a plan script replays identically on a freshly loaded document.

A Hancom Office automation backend would implement the same operator names through COM; it is
``blocked_by_environment`` on non-Windows hosts (see HancomEditorBackend).
"""
from __future__ import annotations

import copy
import os
import re

from lxml import etree

from ..document_ir.layout import Layout, layout_document
from ..document_ir.normalize import NID, DocIR, build_ir, find_nid, iter_slots, para_text, slot_text, stamp, stamp_new
from ..document_ir.schema import hu
from ..formats.hwpx import HC, HP, HwpxPackage, local
from ..formats.hwpx_build import (
    H,
    HeaderEditor,
    X,
    make_cell,
    make_line,
    make_p,
    make_pic,
    make_table,
    make_text_children,
    make_textbox,
)
from ..util import Status

FORBIDDEN_OPS = {"replace_whole_document", "write_arbitrary_xml", "paste_completed_reference"}
HEADER_OPS = {"set_char_format", "set_para_format", "create_table", "create_textbox", "table_insert_row", "table_insert_column",
              "table_split_cell", "insert_image", "undo"}
OBJ_TAGS = {HP + t for t in ("tbl", "rect", "line", "ellipse", "arc", "polygon", "curve", "connectLine", "pic", "container", "ole", "equation",
                             "textart", "checkBtn", "radioBtn", "btn", "comboBox", "edit", "listBox", "scrollBar")}


class OpError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


# ----------------------------------------------------------------------------- text primitives
def _set_slot(el, which: str, value: str) -> None:
    if which == "text":
        el.text = value or None
    else:
        el.tail = value or None


def text_len(p) -> int:
    return len(para_text(p))


def _drop_lineseg(p) -> None:
    for la in p.findall(HP + "linesegarray"):
        p.remove(la)                               # cached line layout is stale after any edit


def _ensure_text_slot(p):
    runs = [r for r in p if r.tag == HP + "run"]
    if not runs:
        run = etree.SubElement(p, HP + "run")
        run.set("charPrIDRef", "0")
        runs = [run]
    for r in runs:
        t = r.find(HP + "t")
        if t is not None:
            return t
    return etree.SubElement(runs[-1], HP + "t")


def insert_text(p, off: int, text: str) -> None:
    pos = 0
    target = None
    for kind, el, payload, _run in iter_slots(p):
        if kind == "text":
            n = len(slot_text(el, payload))
            if pos <= off <= pos + n:
                target = (el, payload, off - pos)
                break
            pos += n
        elif kind == "char":
            pos += 1
    if target is None:
        if off != pos:
            raise OpError("offset_out_of_range", f"{off} > {pos}")
        t = _ensure_text_slot(p)
        last = t[-1] if len(t) else None
        target = (last, "tail", len(last.tail or "")) if last is not None else (t, "text", len(t.text or ""))
    el, payload, k = target
    old = slot_text(el, payload)
    head, tail = old[:k], old[k:]
    parts = re.split("([\n\t])", text)
    cur, cur_pl = el, payload
    _set_slot(cur, cur_pl, head + parts[0])
    i = 1
    while i < len(parts):
        new = etree.Element(HP + ("lineBreak" if parts[i] == "\n" else "tab"))
        if cur_pl == "text":
            cur.insert(0, new)
        else:
            parent = cur.getparent()
            parent.insert(parent.index(cur) + 1, new)
        new.tail = parts[i + 1] or None
        cur, cur_pl = new, "tail"
        i += 2
    _set_slot(cur, cur_pl, slot_text(cur, cur_pl) + tail)
    _drop_lineseg(p)


def delete_range(p, a: int, b: int) -> None:
    pos = 0
    doomed = []
    for kind, el, payload, _run in list(iter_slots(p)):
        if kind == "text":
            s = slot_text(el, payload)
            lo, hi = max(a, pos), min(b, pos + len(s))
            if lo < hi:
                _set_slot(el, payload, s[:lo - pos] + s[hi - pos:])
            pos += len(s)
        elif kind == "char":
            if a <= pos < b:
                doomed.append(el)
            pos += 1
    for el in doomed:
        parent, prev, tail = el.getparent(), el.getprevious(), el.tail or ""
        el.tail = None
        if prev is not None:
            prev.tail = (prev.tail or "") + tail or None
        else:
            parent.text = (parent.text or "") + tail or None
        parent.remove(el)
    _drop_lineseg(p)


def split_run_at(p, off: int) -> None:
    """Guarantee a run boundary at text offset ``off`` (used by formatting and paragraph splitting)."""
    pos = 0
    for kind, el, payload, run in list(iter_slots(p)):
        if kind == "text":
            s = slot_text(el, payload)
            if pos <= off <= pos + len(s):
                k = off - pos
                t = el if payload == "text" else el.getparent()
                if k == 0 and payload == "text" and t.getprevious() is None:
                    return                                   # already at run start
                new_run = etree.Element(HP + "run")
                new_run.set("charPrIDRef", run.get("charPrIDRef") or "0")
                new_t = etree.SubElement(new_run, HP + "t")
                if payload == "text":
                    new_t.text, t.text = (s[k:] or None), (s[:k] or None)
                    for ch in list(t):
                        new_t.append(ch)
                else:
                    new_t.text = s[k:] or None
                    el.tail = s[:k] or None
                    sib = el.getnext()
                    while sib is not None:
                        nxt = sib.getnext()
                        new_t.append(sib)
                        sib = nxt
                sib = t.getnext()
                while sib is not None:
                    nxt = sib.getnext()
                    new_run.append(sib)
                    sib = nxt
                parent = run.getparent()
                parent.insert(parent.index(run) + 1, new_run)
                return
            pos += len(s)
        elif kind == "char":
            pos += 1


def run_ranges(p) -> list[tuple[object, int, int]]:
    out, pos, cur, start = [], 0, None, 0
    for kind, el, payload, run in iter_slots(p):
        if run is not cur:
            if cur is not None:
                out.append((cur, start, pos))
            cur, start = run, pos
        if kind == "text":
            pos += len(slot_text(el, payload))
        elif kind == "char":
            pos += 1
    if cur is not None:
        out.append((cur, start, pos))
    return out


def _signed_attr(v: float) -> str:
    x = int(round(v * 100))
    return str(x if x >= 0 else x + 0x100000000)


# ----------------------------------------------------------------------------- session
class EditorSession:
    backend = "native_hwpx_xml"
    version = "1.0"

    def __init__(self, pkg: HwpxPackage, assets: dict[str, bytes] | None = None):
        self.pkg = pkg
        stamp(pkg)
        self.caret: tuple[str, int] | None = None
        self.selection: tuple[str, int, int] | None = None
        self.obj_selection: tuple[str, ...] = ()
        self.assets = assets or {}
        self.history: list[dict] = []
        self._undo: list = []
        self._ir: DocIR | None = None
        self._layout: Layout | None = None
        self.keep_undo = True

    @classmethod
    def load(cls, src, assets=None) -> EditorSession:
        return cls(HwpxPackage.load(src), assets)

    # --- derived views
    @property
    def ir(self) -> DocIR:
        if self._ir is None:
            self._ir = build_ir(self.pkg)
        return self._ir

    @property
    def layout(self) -> Layout:
        if self._layout is None:
            self._layout = layout_document(self.ir)
        return self._layout

    def _invalidate(self) -> None:
        self._ir = None
        self._layout = None

    def head(self) -> HeaderEditor:
        self.pkg.mark_dirty(self.pkg.header_name)
        return HeaderEditor(self.pkg.header)

    # --- snapshots
    def snapshot(self, with_header: bool = True):
        names = list(self.pkg.section_names) + ([self.pkg.header_name] if with_header else [])
        hpf = next((n for n in self.pkg.names() if n.lower().endswith("content.hpf")), None)
        trees = {n: copy.deepcopy(self.pkg.tree(n)) for n in names}
        if hpf and with_header:
            trees[hpf] = copy.deepcopy(self.pkg.tree(hpf))
        return (trees, self.caret, self.selection, self.obj_selection, list(self.pkg.entries), dict(self.pkg._index), set(self.pkg.dirty), len(self.history))

    def restore(self, snap) -> None:
        trees, self.caret, self.selection, self.obj_selection, entries, index, dirty, hlen = snap
        self.pkg.entries, self.pkg._index, self.pkg.dirty = list(entries), dict(index), set(dirty)
        for n, t in trees.items():
            self.pkg._trees[n] = copy.deepcopy(t)
        del self.history[hlen:]
        self._invalidate()

    # --- apply
    def apply(self, op: dict) -> None:
        name = op.get("op")
        if name in FORBIDDEN_OPS:
            raise OpError("forbidden_operation", f"{name} is not allowed in the main comparison contract")
        fn = getattr(self, "_op_" + str(name), None)
        if fn is None:
            raise OpError(Status.UNSUPPORTED_OPERATION, str(name))
        if name == "undo":
            fn(op)
            return
        snap = self.snapshot(with_header=name in HEADER_OPS)
        try:
            fn(op)
        except OpError:
            self.restore(snap)
            raise
        except Exception as e:      # noqa: BLE001 - any backend failure must leave the document untouched
            self.restore(snap)
            raise OpError("backend_failure", f"{type(e).__name__}: {e}") from e
        if self.keep_undo:
            self._undo.append(snap)
        self.history.append(dict(op))
        self.pkg.mark_dirty()
        self._invalidate()

    def apply_all(self, ops: list[dict]) -> None:
        for op in ops:
            self.apply(op)

    def save(self, dst) -> str:
        return self.pkg.save(dst)

    def to_bytes(self) -> bytes:
        return self.pkg.to_bytes()

    # --- lookup helpers
    def text_of(self, nid: str) -> str:
        """Text of a paragraph (the text model offsets refer to)."""
        from ..document_ir.normalize import para_text
        return para_text(self.el(nid, "p"))

    def el(self, nid: str, tag: str | None = None):
        e = find_nid(self.pkg, nid) if isinstance(nid, str) and nid.startswith("s") else None
        if e is None:
            raise OpError("target_not_found", str(nid))
        if tag and local(e) != tag:
            raise OpError("target_wrong_type", f"{nid} is {local(e)}, expected {tag}")
        return e

    def _root_of(self, el):
        return el.getroottree().getroot()

    def _editable_para(self, nid: str):
        p = self.el(nid, "p")
        anc = p.getparent()
        while anc is not None:
            if anc.tag == HP + "tc" and anc.get("protect") == "1":
                raise OpError("protected_region", f"cell {anc.get(NID)} is protected")
            anc = anc.getparent()
        return p

    def _caret_para(self):
        if self.caret is None:
            raise OpError("no_caret", "focus a text container first")
        return self._editable_para(self.caret[0])

    def _new_id(self, root) -> int:
        ids = [int(e.get("id")) for e in root.iter() if isinstance(e.tag, str) and e.tag in OBJ_TAGS and (e.get("id") or "").isdigit()]
        return (max(ids) + 1) if ids else 2000000001

    # ============================================================== navigation / selection
    def _op_focus(self, op):
        p = self._editable_para(op["target"])
        n = text_len(p)
        off = n if op.get("offset", "end") == "end" else int(op["offset"])
        if not 0 <= off <= n:
            raise OpError("offset_out_of_range", f"{off} not in [0,{n}]")
        self.caret, self.selection, self.obj_selection = (op["target"], off), None, ()

    def _op_focus_cell(self, op):
        tbl = self.el(op["table"], "tbl")
        for tc in tbl.iter(HP + "tc"):
            if tc.getparent().getparent() is not tbl:
                continue
            a, s = tc.find(HP + "cellAddr"), tc.find(HP + "cellSpan")
            r0, c0 = int(a.get("rowAddr")), int(a.get("colAddr"))
            if r0 <= int(op["row"]) < r0 + int(s.get("rowSpan") or 1) and c0 <= int(op["col"]) < c0 + int(s.get("colSpan") or 1):
                p = tc.find(HP + "subList").find(HP + "p")
                if p is None:
                    raise OpError("cell_has_no_paragraph")
                self._op_focus({"target": p.get(NID), "offset": op.get("offset", "end")})
                return
        raise OpError("target_not_found", f"cell ({op['row']},{op['col']})")

    def _op_focus_field(self, op):
        fb = self.el(op["field"], "fieldBegin")
        p = fb.getparent()
        while p is not None and p.tag != HP + "p":
            p = p.getparent()
        a, b = self.field_range(fb)
        self._editable_para(p.get(NID))
        self.caret, self.selection, self.obj_selection = (p.get(NID), a), None, ()

    def field_range(self, fb) -> tuple[int, int]:
        p = fb.getparent()
        while p.tag != HP + "p":
            p = p.getparent()
        pos, a, b = 0, None, None
        for kind, el, payload, _ in iter_slots(p):
            if kind == "text":
                pos += len(slot_text(el, payload))
            elif kind == "char":
                pos += 1
            elif el is fb:
                a = pos
            elif payload == "fieldEnd" and a is not None and el.get("beginIDRef") == fb.get("id") and b is None:
                b = pos
        if a is None:
            raise OpError("target_not_found", "field")
        return a, (b if b is not None else a)

    def _op_move_caret(self, op):
        p = self._caret_para()
        off = int(op["offset"])
        if not 0 <= off <= text_len(p):
            raise OpError("offset_out_of_range")
        self.caret, self.selection = (self.caret[0], off), None

    def _op_select_range(self, op):
        p = self._caret_para()
        a, b = int(op["start"]), int(op["end"])
        if not 0 <= a < b <= text_len(p):
            raise OpError("offset_out_of_range", f"[{a},{b}) vs len {text_len(p)}")
        self.selection, self.caret = (self.caret[0], a, b), (self.caret[0], b)

    def _op_select_objects(self, op):
        nids = []
        for t in op["targets"]:
            e = self.el(t)
            if e.tag not in OBJ_TAGS:
                raise OpError("target_wrong_type", t)
            nids.append(t)
        self.obj_selection, self.caret, self.selection = tuple(sorted(set(nids))), None, None

    def _op_select_objects_in_rect(self, op):
        page, x0, y0, x1, y1 = int(op["page"]), *[float(op[k]) for k in ("x0", "y0", "x1", "y1")]
        hits = []
        for b in self.layout.boxes:
            if b.page == page and b.kind in ("textbox", "pic", "shape", "table", "form") and b.extra.get("owner") is None \
                    and b.x >= x0 - 0.5 and b.y >= y0 - 0.5 and b.x1 <= x1 + 0.5 and b.y1 <= y1 + 0.5 and b.nid:
                e = find_nid(self.pkg, b.nid)
                pos = e.find(HP + "pos") if e is not None else None
                if pos is not None and pos.get("treatAsChar") != "1":
                    hits.append(b.nid)
        if not hits:
            raise OpError("empty_selection", "no floating object inside rectangle")
        self.obj_selection, self.caret, self.selection = tuple(sorted(set(hits))), None, None

    # ============================================================== text
    def _op_type_text(self, op):
        text = op["text"]
        if not isinstance(text, str) or text == "":
            raise OpError("empty_text")
        p = self._caret_para()
        nid, off = self.caret
        if self.selection:
            _, a, b = self.selection
            delete_range(p, a, b)
            off = a
            self.selection = None
        fld = op.get("field")
        if fld:
            fb = self.el(fld, "fieldBegin")
            owner = fb.getparent()
            while owner is not None and owner.tag != HP + "p":
                owner = owner.getparent()
            if owner is not p or self.field_range(fb)[0] != off:
                raise OpError("caret_not_in_field", "the caret must be placed at the start of this field first")
            self._insert_in_field(fb, text)
        else:
            insert_text(p, off, text)
        self.caret = (nid, off + len(text))

    def _insert_in_field(self, fb, text: str) -> None:
        ctrl = fb.getparent()
        run = ctrl.getparent()
        t = etree.Element(HP + "t")
        run.insert(run.index(ctrl) + 1, t)
        make_text_children(t, text)
        p = run.getparent()
        _drop_lineseg(p)

    def _op_delete_selection(self, op):
        p = self._caret_para()
        if not self.selection:
            raise OpError("no_selection")
        nid, a, b = self.selection
        delete_range(p, a, b)
        self.selection, self.caret = None, (nid, a)

    def _op_set_char_format(self, op):
        p = self._caret_para()
        if not self.selection:
            raise OpError("no_selection")
        _, a, b = self.selection
        props = {k: v for k, v in op.get("props", {}).items() if k in ("size", "bold", "italic", "underline", "color", "serif")}
        if not props:
            raise OpError("no_properties")
        if "size" in props and not 1.0 <= float(props["size"]) <= 400:
            raise OpError("invalid_parameter", "size")
        split_run_at(p, a)
        split_run_at(p, b)
        head = self.head()
        changed = 0
        for run, s, e in run_ranges(p):
            if a <= s and e <= b and e > s:
                run.set("charPrIDRef", head.char_pr(base=run.get("charPrIDRef"), **props))
                changed += 1
        if not changed:
            raise OpError("backend_failure", "no run isolated for formatting")
        _drop_lineseg(p)

    def _op_set_para_format(self, op):
        p = self._caret_para()
        props = {k: v for k, v in op.get("props", {}).items() if k in ("align", "line_spacing", "left", "right", "indent", "prev", "next_")}
        if not props:
            raise OpError("no_properties")
        p.set("paraPrIDRef", self.head().para_pr(base=p.get("paraPrIDRef"), **props))
        _drop_lineseg(p)

    def _op_split_paragraph(self, op):
        p = self._caret_para()
        nid, off = self.caret
        split_run_at(p, off)
        new = etree.Element(HP + "p", dict(p.attrib))
        new.attrib.pop(NID, None)
        new.set("pageBreak", "0")
        moved = False
        for run, s, _e in run_ranges(p):
            if s >= off and not (s == 0 and off == 0 and run.find(HP + "secPr") is not None):
                if off == 0 and s == 0 and any(ch.tag == HP + "secPr" for ch in run):
                    continue
                new.append(run)
                moved = True
        if not moved:
            run = etree.SubElement(new, HP + "run")
            last = [r for r in p if r.tag == HP + "run"]
            run.set("charPrIDRef", last[-1].get("charPrIDRef") if last else "0")
            etree.SubElement(run, HP + "t")
        if not [r for r in p if r.tag == HP + "run"]:
            run = etree.SubElement(p, HP + "run")
            run.set("charPrIDRef", new.find(HP + "run").get("charPrIDRef"))
            etree.SubElement(run, HP + "t")
        parent = p.getparent()
        parent.insert(parent.index(p) + 1, new)
        _drop_lineseg(p)
        stamp_new(self._root_of(p), new)
        self.caret, self.selection = (new.get(NID), 0), None

    def _op_join_paragraph(self, op):
        p = self._editable_para(op["target"])
        nxt = p.getnext()
        if nxt is None or nxt.tag != HP + "p":
            raise OpError("no_next_paragraph")
        n = text_len(p)
        for run in [r for r in nxt if r.tag == HP + "run"]:
            p.append(run)
        nxt.getparent().remove(nxt)
        _drop_lineseg(p)
        self.caret, self.selection = (op["target"], n), None

    def _op_duplicate_paragraph(self, op):
        p = self._editable_para(op["target"])
        new = copy.deepcopy(p)
        for e in new.iter():
            if isinstance(e.tag, str):
                e.attrib.pop(NID, None)
        if not op.get("keep_text", False):
            n = text_len(new)
            if n:
                delete_range(new, 0, n)
            for run in new.findall(HP + "run"):
                for ch in list(run):
                    if ch.tag in OBJ_TAGS or ch.tag in (HP + "ctrl", HP + "secPr"):
                        run.remove(ch)
        _drop_lineseg(new)
        parent = p.getparent()
        parent.insert(parent.index(p) + 1, new)
        stamp_new(self._root_of(p), new)
        self.caret, self.selection = (new.get(NID), 0), None

    # ============================================================== tables
    @staticmethod
    def _cells(tbl) -> list:
        out = []
        for tr in tbl:
            if tr.tag == HP + "tr":
                for tc in tr:
                    if tc.tag == HP + "tc":
                        a, s, z = tc.find(HP + "cellAddr"), tc.find(HP + "cellSpan"), tc.find(HP + "cellSz")
                        out.append({"tc": tc, "tr": tr, "row": int(a.get("rowAddr")), "col": int(a.get("colAddr")),
                                    "rs": int(s.get("rowSpan") or 1), "cs": int(s.get("colSpan") or 1),
                                    "w": hu(z.get("width")), "h": hu(z.get("height")), "a": a, "s": s, "z": z})
        return out

    @staticmethod
    def _blank_copy(tc):
        new = copy.deepcopy(tc)
        for e in new.iter():
            if isinstance(e.tag, str):
                e.attrib.pop(NID, None)
        sl = new.find(HP + "subList")
        paras = [p for p in sl if p.tag == HP + "p"]
        for p in paras[1:]:
            sl.remove(p)
        if paras:
            p = paras[0]
            runs = p.findall(HP + "run")
            for r in runs[1:]:
                p.remove(r)
            if runs:
                for ch in list(runs[0]):
                    runs[0].remove(ch)
                etree.SubElement(runs[0], HP + "t")
            _drop_lineseg(p)
        else:
            sl.append(make_p(""))
        return new

    def _grow_table(self, tbl, dw: float = 0.0, dh: float = 0.0):
        sz = tbl.find(HP + "sz")
        if sz is not None:
            sz.set("width", H(hu(sz.get("width")) + dw))
            sz.set("height", H(hu(sz.get("height")) + dh))

    def _op_table_insert_row(self, op):
        tbl = self.el(op["table"], "tbl")
        r = int(op["after_row"])
        cells = self._cells(tbl)
        n_rows = int(tbl.get("rowCnt") or 0)
        if not 0 <= r < n_rows:
            raise OpError("row_out_of_range", f"{r} / {n_rows}")
        trs = [tr for tr in tbl if tr.tag == HP + "tr"]
        new_tr = etree.Element(HP + "tr")
        row_h = 0.0
        ending = sorted((c for c in cells if c["row"] + c["rs"] - 1 == r), key=lambda c: c["col"])
        for c in cells:
            if c["row"] <= r < c["row"] + c["rs"] - 1:
                c["s"].set("rowSpan", str(c["rs"] + 1))             # passes through: extend the merged cell
        for c in ending:
            h = c["h"] / c["rs"]
            row_h = max(row_h, h)
            nc = self._blank_copy(c["tc"])
            nc.find(HP + "cellAddr").set("rowAddr", str(r + 1))
            nc.find(HP + "cellSpan").set("rowSpan", "1")
            nc.find(HP + "cellSz").set("height", H(h))
            nc.set("header", "0")
            new_tr.append(nc)
        for c in cells:
            if c["row"] > r:
                c["a"].set("rowAddr", str(c["row"] + 1))
        for c in cells:
            if c["row"] <= r < c["row"] + c["rs"] - 1:
                c["z"].set("height", H(c["h"] + row_h))
        anchor = trs[min(r, len(trs) - 1)]
        tbl.insert(tbl.index(anchor) + 1, new_tr)
        tbl.set("rowCnt", str(n_rows + 1))
        self._grow_table(tbl, dh=row_h)
        stamp_new(self._root_of(tbl), new_tr)

    def _op_table_delete_row(self, op):
        tbl = self.el(op["table"], "tbl")
        r = int(op["row"])
        n_rows = int(tbl.get("rowCnt") or 0)
        if not 0 <= r < n_rows or n_rows <= 1:
            raise OpError("row_out_of_range")
        cells = self._cells(tbl)
        trs = [tr for tr in tbl if tr.tag == HP + "tr"]
        row_h = max([c["h"] / c["rs"] for c in cells if c["row"] <= r < c["row"] + c["rs"]] or [0.0])
        for c in cells:
            if c["row"] == r and c["rs"] == 1:
                c["tr"].remove(c["tc"])
            elif c["row"] <= r < c["row"] + c["rs"]:
                c["s"].set("rowSpan", str(c["rs"] - 1))
                c["z"].set("height", H(max(c["h"] - row_h, 1.0)))
                if c["row"] == r and r + 1 < len(trs):               # merged cell starts here: move to the next row
                    nxt = trs[r + 1]
                    before = [x for x in nxt if x.tag == HP + "tc" and int(x.find(HP + "cellAddr").get("colAddr")) > c["col"]]
                    c["tr"].remove(c["tc"])
                    if before:
                        nxt.insert(nxt.index(before[0]), c["tc"])
                    else:
                        nxt.append(c["tc"])
            elif c["row"] > r:
                c["a"].set("rowAddr", str(c["row"] - 1))
        tbl.remove(trs[r])
        tbl.set("rowCnt", str(n_rows - 1))
        self._grow_table(tbl, dh=-row_h)
        self.caret = self.selection = None

    def _op_table_insert_column(self, op):
        tbl = self.el(op["table"], "tbl")
        k = int(op["after_col"])
        n_cols = int(tbl.get("colCnt") or 0)
        if not 0 <= k < n_cols:
            raise OpError("col_out_of_range")
        cells = self._cells(tbl)
        col_w = max([c["w"] / c["cs"] for c in cells if c["col"] + c["cs"] - 1 == k] or [40.0])
        for c in cells:
            if c["col"] > k:
                c["a"].set("colAddr", str(c["col"] + 1))
            elif c["col"] <= k < c["col"] + c["cs"] - 1:
                c["s"].set("colSpan", str(c["cs"] + 1))
                c["z"].set("width", H(c["w"] + col_w))
            elif c["col"] + c["cs"] - 1 == k:
                nc = self._blank_copy(c["tc"])
                nc.find(HP + "cellAddr").set("colAddr", str(k + 1))
                nc.find(HP + "cellSpan").set("colSpan", "1")
                nc.find(HP + "cellSz").set("width", H(col_w))
                c["tr"].insert(c["tr"].index(c["tc"]) + 1, nc)
                stamp_new(self._root_of(tbl), nc)
        tbl.set("colCnt", str(n_cols + 1))
        self._grow_table(tbl, dw=col_w)

    def _op_table_delete_column(self, op):
        tbl = self.el(op["table"], "tbl")
        k = int(op["col"])
        n_cols = int(tbl.get("colCnt") or 0)
        if not 0 <= k < n_cols or n_cols <= 1:
            raise OpError("col_out_of_range")
        cells = self._cells(tbl)
        col_w = max([c["w"] / c["cs"] for c in cells if c["col"] <= k < c["col"] + c["cs"]] or [0.0])
        for c in cells:
            if c["col"] == k and c["cs"] == 1:
                c["tr"].remove(c["tc"])
            elif c["col"] <= k < c["col"] + c["cs"]:
                c["s"].set("colSpan", str(c["cs"] - 1))
                c["z"].set("width", H(max(c["w"] - col_w, 1.0)))
            elif c["col"] > k:
                c["a"].set("colAddr", str(c["col"] - 1))
        tbl.set("colCnt", str(n_cols - 1))
        self._grow_table(tbl, dw=-col_w)
        self.caret = self.selection = None

    def _op_table_merge_cells(self, op):
        tbl = self.el(op["table"], "tbl")
        r0, c0, r1, c1 = (int(op[k]) for k in ("row0", "col0", "row1", "col1"))
        if r1 < r0 or c1 < c0 or (r0 == r1 and c0 == c1):
            raise OpError("invalid_parameter", "merge range")
        cells = self._cells(tbl)
        inside = [c for c in cells if c["row"] >= r0 and c["col"] >= c0 and c["row"] + c["rs"] - 1 <= r1 and c["col"] + c["cs"] - 1 <= c1]
        area = sum(c["rs"] * c["cs"] for c in inside)
        crossing = [c for c in cells if c not in inside and not (c["row"] > r1 or c["row"] + c["rs"] - 1 < r0 or c["col"] > c1 or c["col"] + c["cs"] - 1 < c0)]
        if crossing or area != (r1 - r0 + 1) * (c1 - c0 + 1):
            raise OpError("merge_range_not_rectangular")
        keep = next((c for c in inside if c["row"] == r0 and c["col"] == c0), None)
        if keep is None:
            raise OpError("merge_range_not_rectangular")
        width = sum(c["w"] for c in inside if c["row"] == r0)
        height = sum(c["h"] for c in inside if c["col"] == c0)
        sl = keep["tc"].find(HP + "subList")
        for c in sorted(inside, key=lambda c: (c["row"], c["col"])):
            if c is keep:
                continue
            for p in c["tc"].find(HP + "subList"):
                if p.tag == HP + "p" and (para_text(p).strip() or any(ch.tag in OBJ_TAGS for r in p for ch in r)):
                    sl.append(p)
            c["tr"].remove(c["tc"])
        keep["s"].set("rowSpan", str(r1 - r0 + 1))
        keep["s"].set("colSpan", str(c1 - c0 + 1))
        keep["z"].set("width", H(width))
        keep["z"].set("height", H(height))
        self.caret = self.selection = None

    def _op_table_split_cell(self, op):
        tbl = self.el(op["table"], "tbl")
        r, k = int(op["row"]), int(op["col"])
        cells = self._cells(tbl)
        c = next((c for c in cells if c["row"] == r and c["col"] == k), None)
        if c is None or (c["rs"] == 1 and c["cs"] == 1):
            raise OpError("cell_not_merged")
        w, h = c["w"] / c["cs"], c["h"] / c["rs"]
        trs = [tr for tr in tbl if tr.tag == HP + "tr"]
        for rr in range(r, r + c["rs"]):
            for kk in range(k, k + c["cs"]):
                if rr == r and kk == k:
                    continue
                nc = self._blank_copy(c["tc"])
                nc.find(HP + "cellAddr").set("rowAddr", str(rr))
                nc.find(HP + "cellAddr").set("colAddr", str(kk))
                nc.find(HP + "cellSpan").set("rowSpan", "1")
                nc.find(HP + "cellSpan").set("colSpan", "1")
                nc.find(HP + "cellSz").set("width", H(w))
                nc.find(HP + "cellSz").set("height", H(h))
                tr = trs[rr]
                after = [x for x in tr if x.tag == HP + "tc" and int(x.find(HP + "cellAddr").get("colAddr")) > kk]
                if after:
                    tr.insert(tr.index(after[0]), nc)
                else:
                    tr.append(nc)
                stamp_new(self._root_of(tbl), nc)
        c["s"].set("rowSpan", "1")
        c["s"].set("colSpan", "1")
        c["z"].set("width", H(w))
        c["z"].set("height", H(h))

    # ============================================================== objects
    def _anchor_para(self, page: int):
        boxes = [b for b in self.layout.boxes if b.kind == "para" and b.page == page and b.extra.get("owner") is None and b.extra.get("role") == "body"]
        if not boxes:
            raise OpError("no_anchor_on_page", f"page {page} has no body paragraph to anchor a page-relative object")
        return self.el(boxes[0].nid, "p")

    def _attach(self, p, obj) -> None:
        runs = [r for r in p if r.tag == HP + "run"]
        if not runs:
            run = etree.SubElement(p, HP + "run")
            run.set("charPrIDRef", "0")
            runs = [run]
        runs[-1].append(obj)
        _drop_lineseg(p)
        stamp_new(self._root_of(p), obj)

    def _rect_args(self, op, min_size: float = 2.0):
        x, y, w, h = (float(op[k]) for k in ("x", "y", "w", "h"))
        if w < min_size or h < min_size or x < -1 or y < -1:
            raise OpError("invalid_parameter", "geometry")
        return x, y, w, h

    def _op_create_textbox(self, op):
        x, y, w, h = self._rect_args(op)
        p = self._anchor_para(int(op["page"]))
        root = self._root_of(p)
        run0 = p.find(HP + "run")
        char_id = run0.get("charPrIDRef") if run0 is not None else "0"
        para_id = self.head().para_pr(base="0", align="LEFT", left=0, right=0, indent=0, prev=0, next_=0)
        box = make_textbox(self._new_id(root), x, y, w, h, border=bool(op.get("border", True)), wrap="IN_FRONT_OF_TEXT",
                           char_id=char_id, para_id=para_id, valign=op.get("valign", "TOP"))
        self._attach(p, box)
        inner = box.find(".//" + HP + "p")
        self.caret, self.selection, self.obj_selection = (inner.get(NID), 0), None, ()      # like a GUI: caret lands inside the new box

    def _op_create_table(self, op):
        x, y = float(op["x"]), float(op["y"])
        cw = [float(v) for v in op["col_widths"]]
        rh = [float(v) for v in op["row_heights"]]
        if not cw or not rh or min(cw + rh) < 2.0 or len(cw) > 64 or len(rh) > 256:
            raise OpError("invalid_parameter", "table geometry")
        p = self._anchor_para(int(op["page"]))
        root = self._root_of(p)
        head = self.head()
        bf = head.border_fill()
        run0 = p.find(HP + "run")
        tbl = make_table(self._new_id(root), cw, rh, [], bf, inline=False, x=x, y=y, wrap=op.get("wrap", "IN_FRONT_OF_TEXT"),
                         char_id=run0.get("charPrIDRef") if run0 is not None else "0", para_id=head.para_pr(base="0", align="LEFT"))
        self._attach(p, tbl)
        first = tbl.find(".//" + HP + "p")
        self.caret, self.selection, self.obj_selection = (first.get(NID), 0), None, ()

    def _op_create_line(self, op):
        x0, y0, x1, y1 = (float(op[k]) for k in ("x0", "y0", "x1", "y1"))
        if abs(x1 - x0) + abs(y1 - y0) < 1.0:
            raise OpError("invalid_parameter", "zero-length line")
        p = self._anchor_para(int(op["page"]))
        self._attach(p, make_line(self._new_id(self._root_of(p)), x0, y0, x1, y1, float(op.get("width", 0.34))))

    def _op_insert_image(self, op):
        x, y, w, h = self._rect_args(op)
        data = self.assets.get(op.get("asset", ""))
        if not data:
            raise OpError("asset_not_found", str(op.get("asset")))
        p = self._anchor_para(int(op["page"]))
        ext = "png" if data[:4] == b"\x89PNG" else "jpg"
        bin_id = self.pkg.add_bin(data, ext)
        self._attach(p, make_pic(self._new_id(self._root_of(p)), bin_id, x, y, w, h, wrap="IN_FRONT_OF_TEXT"))

    def _obj(self, nid: str):
        e = self.el(nid)
        if e.tag not in OBJ_TAGS:
            raise OpError("target_wrong_type", f"{nid} is not an object")
        return e

    def _move(self, e, dx: float, dy: float) -> None:
        pos = e.find(HP + "pos")
        if pos is None or pos.get("treatAsChar") == "1":
            raise OpError("inline_object_not_movable", "object is placed as a character; change its anchor first")
        pos.set("horzOffset", _signed_attr(hu(pos.get("horzOffset")) + dx))
        pos.set("vertOffset", _signed_attr(hu(pos.get("vertOffset")) + dy))

    def _op_move_object(self, op):
        self._move(self._obj(op["target"]), float(op.get("dx", 0)), float(op.get("dy", 0)))

    def _op_move_selection(self, op):
        if not self.obj_selection:
            raise OpError("no_selection", "select objects first")
        for nid in self.obj_selection:
            self._move(self._obj(nid), float(op.get("dx", 0)), float(op.get("dy", 0)))

    def _op_resize_object(self, op):
        e = self._obj(op["target"])
        if e.tag == HP + "tbl":
            raise OpError(Status.UNSUPPORTED_OPERATION, "resize a table through row/column operations")
        w, h = float(op["w"]), float(op["h"])
        if w < 1 or h < 1:
            raise OpError("invalid_parameter", "size")
        sz = e.find(HP + "sz")
        if sz is None:
            raise OpError(Status.UNSUPPORTED_OPERATION, "object has no size record")
        ow, oh = hu(sz.get("width")), hu(sz.get("height"))
        sz.set("width", H(w))
        sz.set("height", H(h))
        for tag in ("orgSz", "curSz"):
            c = e.find(HP + tag)
            if c is not None and (tag == "orgSz" or hu(c.get("width")) > 0):
                c.set("width", H(w))
                c.set("height", H(h))
        for name, (px, py) in (("pt1", (w, 0)), ("pt2", (w, h)), ("pt3", (0, h))):
            pt = e.find(HC + name)
            if pt is not None:
                pt.set("x", H(px))
                pt.set("y", H(py))
        if e.tag == HP + "line":
            a, b = e.find(HC + "startPt"), e.find(HC + "endPt")
            for pt in (a, b):
                if pt is not None:
                    pt.set("x", H(hu(pt.get("x")) * (w / ow if ow > 0.01 else 1)))
                    pt.set("y", H(hu(pt.get("y")) * (h / oh if oh > 0.01 else 1)))
        dt = e.find(HP + "drawText")
        if dt is not None:
            dt.set("lastWidth", H(w))

    def _op_set_object_anchor(self, op):
        e = self._obj(op["target"])
        pos = e.find(HP + "pos")
        if pos is None:
            raise OpError(Status.UNSUPPORTED_OPERATION, "object has no position record")
        mode = op["mode"]
        if mode == "inline":
            pos.set("treatAsChar", "1")
            pos.set("vertRelTo", "PARA")
            pos.set("horzRelTo", "COLUMN")
            pos.set("vertOffset", "0")
            pos.set("horzOffset", "0")
        elif mode in ("paper", "para"):
            box = self.layout.box_of(op["target"])
            pos.set("treatAsChar", "0")
            if mode == "paper":
                if box is None:
                    raise OpError("backend_failure", "object has no layout box")
                pos.set("vertRelTo", "PAPER")
                pos.set("horzRelTo", "PAPER")
                pos.set("vertAlign", "TOP")
                pos.set("horzAlign", "LEFT")
                pos.set("vertOffset", H(box.y))
                pos.set("horzOffset", H(box.x))
                pos.set("flowWithText", "0")
                pos.set("allowOverlap", "1")
            else:
                pos.set("vertRelTo", "PARA")
                pos.set("horzRelTo", "COLUMN")
        else:
            raise OpError("invalid_parameter", "mode")
        if op.get("wrap"):
            if op["wrap"] not in ("SQUARE", "TIGHT", "THROUGH", "TOP_AND_BOTTOM", "BEHIND_TEXT", "IN_FRONT_OF_TEXT"):
                raise OpError("invalid_parameter", "wrap")
            e.set("textWrap", op["wrap"])

    def _op_set_object_style(self, op):
        e = self._obj(op["target"])
        ls = e.find(HP + "lineShape")
        if "border" in op:
            if ls is None:
                raise OpError(Status.UNSUPPORTED_OPERATION, "object has no outline")
            if op["border"] not in ("NONE", "SOLID", "DASH", "DOT"):
                raise OpError("invalid_parameter", "border")
            ls.set("style", op["border"])
        if "fill" in op:
            fb = e.find(HC + "fillBrush")
            if fb is not None:
                e.remove(fb)
            if op["fill"]:
                anchor = ls if ls is not None else e.find(HP + "renderingInfo")
                new = X(f'<hc:fillBrush><hc:winBrush faceColor="{op["fill"]}" hatchColor="#000000" alpha="0"/></hc:fillBrush>')
                e.insert(e.index(anchor) + 1, new) if anchor is not None else e.append(new)
        if "border" not in op and "fill" not in op:
            raise OpError("no_properties")

    def _op_set_size_lock(self, op):
        e = self._obj(op["target"])
        sz = e.find(HP + "sz")
        if sz is None:
            raise OpError(Status.UNSUPPORTED_OPERATION, "object has no size record")
        sz.set("protect", "1" if op.get("locked", True) else "0")

    def _op_delete_object(self, op):
        e = self._obj(op["target"])
        e.getparent().remove(e)
        self.caret = self.selection = None
        self.obj_selection = ()

    def _op_toggle_checkbox(self, op):
        e = self.el(op["target"])
        if e.tag not in (HP + "checkBtn", HP + "radioBtn"):
            raise OpError("target_wrong_type", "not a check/radio control")
        e.set("value", "UNCHECKED" if e.get("value") == "CHECKED" else "CHECKED")

    def _op_set_page_number(self, op):
        pos = op.get("pos", "BOTTOM_CENTER")
        if pos not in ("NONE", "TOP_LEFT", "TOP_CENTER", "TOP_RIGHT", "BOTTOM_LEFT", "BOTTOM_CENTER", "BOTTOM_RIGHT"):
            raise OpError("invalid_parameter", "pos")
        sec = self.pkg.sections[int(op.get("section", 0))]
        cur = sec.find(".//" + HP + "pageNum")
        if cur is None:
            p = sec.find(HP + "p")
            run = p.find(HP + "run")
            ctrl = etree.SubElement(run, HP + "ctrl")
            cur = etree.SubElement(ctrl, HP + "pageNum")
        cur.set("pos", pos)
        cur.set("formatType", op.get("format", "DIGIT"))
        cur.set("sideChar", op.get("side", "-"))

    def _op_undo(self, op):
        if not self._undo:
            raise OpError("nothing_to_undo")
        self.restore(self._undo.pop())
        self.pkg.mark_dirty()


class HancomEditorBackend:
    """Operator execution through Hancom Office automation. Requires Windows + licensed Hancom Office."""
    backend = "hancom_automation"

    @staticmethod
    def available() -> tuple[bool, str]:
        if os.name != "nt":
            return False, "requires Windows with Hancom Office (HwpObject COM); host is not Windows"
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return False, "pywin32 missing"
        return True, ""

    def __init__(self, *_a, **_k):
        ok, why = self.available()
        if not ok:
            raise OpError(Status.BLOCKED_BY_ENVIRONMENT, why)


def copy_cell(tc):  # re-export for tests
    return EditorSession._blank_copy(tc)


__all__ = ["EditorSession", "OpError", "FORBIDDEN_OPS", "insert_text", "delete_range", "split_run_at", "text_len", "make_cell",
           "HancomEditorBackend"]
