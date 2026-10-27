"""Native ODT editing session (no conversion of the edited document): content.xml is changed in place, every other
part of the package is copied byte for byte (mimetype first, stored). The layout / hit-testing view is derived after
each change by formats/odt_view.py with native session ids copied onto the view nodes.

Text model = ODF white-space processing of the paragraph's own content (formats/odt_view.own_atoms). Inserted text is
encoded so that it survives that processing: a space that would collapse (paragraph start, after another space / tab
/ line break) becomes text:s, '\\t' -> text:tab, '\\n' -> text:line-break. Inserted text takes the formatting of the
position (a text:span without a style inherits the enclosing span / paragraph style)."""
from __future__ import annotations

import copy
import io
import zipfile

from lxml import etree

from ..document_ir.layout import layout_document
from ..document_ir.normalize import NEXT, NID, STAMP_TAGS, build_ir
from ..formats.odt_view import PARA_TAGS, TB, D, T, OdtView, own_atoms, own_text, secure
from .editor_backends import FORBIDDEN_OPS, OpError

ID_TAGS = PARA_TAGS + (TB + "table", TB + "table-cell", D + "frame")
VIEW_ONLY_BASE = 5_000_000


class NativeOdtSession:
    backend = "native_odt_xml"
    version = "1.0"
    fmt = "odt"

    def __init__(self, data: bytes):
        self.data = data
        self.root = secure(zipfile.ZipFile(io.BytesIO(data)).read("content.xml"))
        self._next = 0
        for e in self.root.iter(*ID_TAGS):
            e.set(NID, f"s0n{self._next}")
            self._next += 1
        self.caret = self.selection = None
        self.obj_selection: tuple[str, ...] = ()
        self.history: list[dict] = []
        self._undo: list = []
        self._view = self._ir = self._layout = None
        self.keep_undo = True

    @classmethod
    def load(cls, data: bytes) -> NativeOdtSession:
        return cls(data)

    # ------------------------------------------------------------------ view
    @property
    def view(self):
        if self._view is None:
            pkg, info = OdtView(self.data, content=self.root).build()
            n = VIEW_ONLY_BASE
            for root in pkg.sections:
                for e in root.iter():
                    if e.tag in STAMP_TAGS and e.get(NID) is None:
                        e.set(NID, f"s0n{n}")
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
    def native(self, nid: str, tags=None):
        r = self.root.xpath(".//*[@f:nid=$v]", namespaces={"f": NID[1:].split("}")[0]}, v=nid)
        if not r:
            if nid.startswith("s0n") and int(nid[3:]) >= VIEW_ONLY_BASE:
                raise OpError("protected_region", f"{nid} exists only in the view (no native element)")
            raise OpError("target_not_found", nid)
        if tags and r[0].tag not in tags:
            raise OpError("target_wrong_type", f"{nid} is {etree.QName(r[0]).localname}")
        return r[0]

    def para(self, nid: str):
        return self.native(nid, PARA_TAGS)

    def text_of(self, nid: str) -> str:
        return own_text(self.para(nid))

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
            raise OpError("unsupported_operation", f"{name} (native ODT backend)")
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
    @staticmethod
    def _encode(text: str, prev: str):
        """ODF-safe content for ``text`` typed after character ``prev`` ('' = paragraph start): list of str / Element."""
        out: list = []
        buf = ""
        for ch in text:
            collapsible = prev in ("", " ", "\t", "\n")
            if ch == " " and collapsible:
                if buf:
                    out.append(buf)
                    buf = ""
                if out and isinstance(out[-1], etree._Element) and out[-1].tag == T + "s":
                    out[-1].set(T + "c", str(int(out[-1].get(T + "c") or 1) + 1))
                else:
                    out.append(etree.Element(T + "s"))
            elif ch in "\t\n":
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(etree.Element(T + ("tab" if ch == "\t" else "line-break")))
            else:
                buf += ch
            prev = ch
        if buf:
            out.append(buf)
        return out

    def _insert_at(self, p, k: int, text: str) -> None:
        atoms = own_atoms(p)
        if not 0 <= k <= len(atoms):
            raise OpError("offset_out_of_range", f"{k} not in [0,{len(atoms)}]")
        prev = atoms[k - 1][0] if k else ""
        nxt = atoms[k][0] if k < len(atoms) else ""
        if nxt == " " and text.endswith(" "):
            pass                                          # following char-data space would collapse: keep it as is
        span = etree.Element(T + "span")
        items = self._encode(text, prev)
        if k == len(atoms) and items and isinstance(items[-1], str) and items[-1].endswith(" "):
            # trailing white space at the paragraph end would collapse: keep it visible as text:s (as editors save it)
            body = items[-1].rstrip(" ")
            n = len(items[-1]) - len(body)
            sp = etree.Element(T + "s")
            if n > 1:
                sp.set(T + "c", str(n))
            items[-1:] = ([body] if body else []) + [sp]
        last = None
        for it in items:
            if isinstance(it, str):
                if last is None:
                    span.text = (span.text or "") + it
                else:
                    last.tail = (last.tail or "") + it
            else:
                span.append(it)
                last = it
        if nxt == " " and atoms[k][1] == "chars" and text and text[-1] in " \t\n":
            # the following literal space would now collapse after our trailing white space: make it explicit
            node, attr, i = atoms[k][2], atoms[k][3], atoms[k][4]
            s = getattr(node, attr)
            setattr(node, attr, s[:i] + s[i + 1:])
            sp = etree.Element(T + "s")
            span.append(sp)
        # position: before atom k (or after the last atom / at paragraph start)
        if k < len(atoms):
            ch, kind, node, attr, i = atoms[k]
            if kind == "chars":
                s = getattr(node, attr)
                if attr == "text":
                    node.text = s[:i]
                    span.tail = s[i:]
                    node.insert(0, span)
                else:
                    node.tail = s[:i]
                    span.tail = s[i:]
                    node.addnext(span)
            else:
                if kind == "s" and i > 0:                  # inside a multi-space text:s: split it
                    c = int(node.get(T + "c") or 1)
                    node.set(T + "c", str(i))
                    rest = etree.Element(T + "s")
                    rest.set(T + "c", str(c - i))
                    rest.tail, node.tail = node.tail, None
                    node.addnext(rest)
                    node = rest
                prevsib = node.getprevious()
                par = node.getparent()
                span.tail = None
                node.addprevious(span)
        elif atoms:
            ch, kind, node, attr, i = atoms[-1]
            if kind == "chars":
                s = getattr(node, attr)
                cut = i + 1
                if attr == "text":
                    node.text = s[:cut]
                    span.tail = s[cut:]
                    node.insert(0, span)
                else:
                    node.tail = s[:cut]
                    span.tail = s[cut:]
                    node.addnext(span)
            else:
                if kind == "s" and i < int(node.get(T + "c") or 1) - 1:
                    raise OpError("unsupported_operation", "insertion inside trailing text:s")
                span.tail = node.tail
                node.tail = None
                node.addnext(span)
        else:
            p.insert(0, span)
            span.tail = p.text
            p.text = None

    @staticmethod
    def _space_to_s(atom) -> None:
        """Make a literal (collapsible) space explicit as text:s, as editors do when a deletion would collapse it."""
        _, _, node, attr, i = atom
        s = getattr(node, attr)
        sp = etree.Element(T + "s")
        sp.tail = s[i + 1:]
        setattr(node, attr, s[:i])
        if attr == "text":
            node.insert(0, sp)
        else:
            node.addnext(sp)

    def _delete_range(self, p, a: int, b: int) -> None:
        atoms = own_atoms(p)
        if not 0 <= a < b <= len(atoms):
            raise OpError("offset_out_of_range")
        expected = "".join(x[0] for x in atoms[:a] + atoms[b:])
        # a literal space next to the deleted range would collapse once it starts the paragraph, follows other white
        # space or ends the paragraph: keep it visible (text:s), as an editor does
        if b < len(atoms) and atoms[b][0] == " " and atoms[b][1] == "chars" and (a == 0 or atoms[a - 1][0] in " \t\n"):
            self._space_to_s(atoms[b])
            atoms = own_atoms(p)
        if a > 0 and atoms[a - 1][0] == " " and atoms[a - 1][1] == "chars" and (b == len(atoms) or atoms[b][0] in " \t\n"):
            self._space_to_s(atoms[a - 1])
            atoms = own_atoms(p)
        # delete from the end so earlier raw indices stay valid
        for ch, kind, node, attr, i in reversed(atoms[a:b]):
            if kind == "chars":
                s = getattr(node, attr)
                setattr(node, attr, s[:i] + s[i + 1:])
            elif kind == "s":
                c = int(node.get(T + "c") or 1)
                if c > 1:
                    node.set(T + "c", str(c - 1))
                else:
                    self._remove_keep_tail(node)
            else:
                self._remove_keep_tail(node)
        if own_text(p) != expected:
            raise OpError("backend_failure", "ODF white-space processing changed the text around the deletion")

    @staticmethod
    def _remove_keep_tail(node):
        par, prev = node.getparent(), node.getprevious()
        if node.tail:
            if prev is not None:
                prev.tail = (prev.tail or "") + node.tail
            else:
                par.text = (par.text or "") + node.tail
        par.remove(node)

    def _caret_para(self):
        if self.caret is None:
            raise OpError("no_caret", "focus a text container first")
        return self.para(self.caret[0])

    # ------------------------------------------------------------------ operations
    def _op_focus(self, op):
        self.para(op["target"])
        n = len(self.text_of(op["target"]))
        off = n if op.get("offset", "end") == "end" else int(op["offset"])
        if not 0 <= off <= n:
            raise OpError("offset_out_of_range")
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
        before = self.text_of(nid)
        self._insert_at(p, off, text)
        after = self.text_of(nid)
        if after != before[:off] + text + before[off:]:
            raise OpError("backend_failure", "ODF white-space encoding did not reproduce the typed text")
        self.caret = (nid, off + len(text))

    def _op_delete_selection(self, op):
        if not self.selection:
            raise OpError("no_selection")
        nid, a, b = self.selection
        self._delete_range(self.para(nid), a, b)
        self.caret, self.selection = (nid, a), None

    def _op_split_paragraph(self, op):
        p = self._caret_para()
        nid, off = self.caret
        text = self.text_of(nid)
        tail_text = text[off:]
        if tail_text:
            self._delete_range(p, off, len(text))
        new = etree.Element(p.tag, {k: v for k, v in p.attrib.items() if k != NID})
        p.addnext(new)
        self._new_ids(new)
        if tail_text:
            self._insert_at(new, 0, tail_text)
        self.caret, self.selection = (new.get(NID), 0), None

    def _rows(self, tbl):
        return [r for r in tbl.iter(TB + "table-row") if next(r.iterancestors(TB + "table"), None) is tbl]

    def _op_table_insert_row(self, op):
        tbl = self.native(op["table"], (TB + "table",))
        rows = self._rows(tbl)
        r = int(op["after_row"])
        if not 0 <= r < len(rows):
            raise OpError("row_out_of_range")
        new = copy.deepcopy(rows[r])
        for c in new:
            if c.tag == TB + "covered-table-cell":
                c.tag = TB + "table-cell"                   # a row-spanned cell above does not extend into the new row
            if c.tag == TB + "table-cell":
                c.attrib.pop(TB + "number-rows-spanned", None)
                for ch in list(c):
                    c.remove(ch)
                c.text = None
                etree.SubElement(c, T + "p")
        rows[r].addnext(new)
        self._new_ids(new)
        self.caret = self.selection = None

    def _op_table_delete_row(self, op):
        tbl = self.native(op["table"], (TB + "table",))
        rows = self._rows(tbl)
        r = int(op["row"])
        if not 0 <= r < len(rows) or len(rows) == 1:
            raise OpError("row_out_of_range")
        if any(c.get(TB + "number-rows-spanned") for c in rows[r]):
            raise OpError("unsupported_operation", "row holds a vertically merged cell")
        rows[r].getparent().remove(rows[r])
        self.caret = self.selection = None

    def _op_select_objects(self, op):
        for nid in op["targets"]:
            self.native(nid, (D + "frame",))
        self.obj_selection = tuple(op["targets"])
        self.caret = self.selection = None

    def _op_move_selection(self, op):
        from ..formats.odt_view import SVG, length_pt
        if not self.obj_selection:
            raise OpError("no_selection")
        for nid in self.obj_selection:
            fr = self.native(nid, (D + "frame",))
            if fr.get(T + "anchor-type") == "as-char":
                raise OpError("unsupported_operation", "inline frame")
            for attr, d in ((SVG + "x", float(op.get("dx", 0))), (SVG + "y", float(op.get("dy", 0)))):
                fr.set(attr, f"{(length_pt(fr.get(attr), 0.0) or 0.0) + d:.3f}pt")

    def _op_undo(self, op):
        if not self._undo:
            raise OpError("nothing_to_undo")
        self.restore(self._undo.pop())
        self.history.append({"op": "undo"})

    # ------------------------------------------------------------------ save
    def to_bytes(self) -> bytes:
        root = copy.deepcopy(self.root)
        for e in root.iter():
            if NID in e.attrib:
                del e.attrib[NID]
        etree.cleanup_namespaces(root)
        content = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
        zin = zipfile.ZipFile(io.BytesIO(self.data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            names = zin.namelist()
            if "mimetype" in names:                         # ODF: mimetype first and stored
                zo.writestr(zipfile.ZipInfo("mimetype"), zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for info in zin.infolist():
                if info.filename == "mimetype":
                    continue
                zo.writestr(info, content if info.filename == "content.xml" else zin.read(info.filename))
        return out.getvalue()

    def save(self, dst) -> str:
        from pathlib import Path
        Path(dst).write_bytes(self.to_bytes())
        return str(dst)
