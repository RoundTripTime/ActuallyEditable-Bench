"""DocIR -> ODT (OpenDocument Text 1.3) writer, used to emit the reconstructions of experiment v3 as ODF documents.

Covered: page size / margins (first section), paragraphs (alignment, margins, indent, line spacing, size, bold,
italic, underline, colour), text with ODF white-space encoding (text:s / text:tab / text:line-break), tables (column
widths, row heights, row/column spans with covered cells, cell borders, fill, vertical alignment), text boxes
(draw:frame + draw:text-box, fixed size), pictures (inline as-char or page-anchored), lines (draw:line).
Floating objects are anchored to their page at the position the reference layout gives them (the reconstructions
place objects absolutely). Tables that float are wrapped in a page-anchored frame. Anything else is counted in
``losses``."""
from __future__ import annotations

import io
import zipfile

from lxml import etree

from ..document_ir.layout import layout_document
from ..document_ir.schema import DocIR, Para, Shape, Table

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
O, S, T, TB, D, FO, SVG, XL = (f"{{{NS[k]}}}" for k in ("office", "style", "text", "table", "draw", "fo", "svg", "xlink"))
ALIGN = {"LEFT": "start", "RIGHT": "end", "CENTER": "center", "JUSTIFY": "justify", "DISTRIBUTE": "justify"}
VALIGN = {"TOP": "top", "CENTER": "middle", "BOTTOM": "bottom"}


def pt(v: float) -> str:
    return f"{max(v, 0.0):.3f}pt"


def _el(tag, attrs=None, parent=None):
    e = etree.Element(tag, {k: str(v) for k, v in (attrs or {}).items()}) if parent is None else \
        etree.SubElement(parent, tag, {k: str(v) for k, v in (attrs or {}).items()})
    return e


def encode_text(parent, text: str, prev: str = "") -> None:
    """Append text to ``parent`` so that ODF white-space processing reproduces it exactly."""
    last = None

    def add_str(s):
        nonlocal last
        if not s:
            return
        if last is None:
            parent.text = (parent.text or "") + s
        else:
            last.tail = (last.tail or "") + s
    buf = ""
    for ch in text:
        collapsible = prev in ("", " ", "\t", "\n")
        if ch == " " and collapsible:
            add_str(buf)
            buf = ""
            if last is not None and last.tag == T + "s" and not last.tail:
                last.set(T + "c", str(int(last.get(T + "c") or 1) + 1))
            else:
                last = _el(T + "s", parent=parent)
        elif ch in "\t\n":
            add_str(buf)
            buf = ""
            last = _el(T + ("tab" if ch == "\t" else "line-break"), parent=parent)
        else:
            buf += ch
        prev = ch
    add_str(buf)


class _Styles:
    def __init__(self, auto):
        self.auto = auto
        self.cache: dict = {}
        self.n: dict = {}

    def get(self, family: str, key: tuple, build) -> str:
        if (family, key) in self.cache:
            return self.cache[(family, key)]
        self.n[family] = self.n.get(family, 0) + 1
        name = {"paragraph": "P", "text": "T", "table": "Tab", "table-column": "Col", "table-row": "Row",
                "table-cell": "Cell", "graphic": "Gr"}[family] + str(self.n[family])
        st = _el(S + "style", {S + "name": name, S + "family": family}, self.auto)
        build(st)
        self.cache[(family, key)] = name
        return name


class OdtWriter:
    def __init__(self, ir: DocIR, binaries: dict | None = None):
        self.ir = ir
        self.lay = layout_document(ir)
        self.boxes = {}
        for b in self.lay.boxes:
            self.boxes.setdefault(b.nid, b)
        self.bins = binaries or {}
        self.losses: dict[str, int] = {}
        self.pictures: dict[str, bytes] = {}
        self.content = etree.Element(O + "document-content", {O + "version": "1.3"}, nsmap={k: v for k, v in NS.items() if k != "manifest"})
        self.auto = _el(O + "automatic-styles", parent=self.content)
        self.st = _Styles(self.auto)
        body = _el(O + "body", parent=self.content)
        self.text = _el(O + "text", parent=body)

    def note(self, k):
        self.losses[k] = self.losses.get(k, 0) + 1

    # ------------------------------------------------------------------ styles
    def para_style(self, p: Para) -> str:
        ps = p.ps
        cs = p.default_cs or next((i.cs for i in p.inlines if i.cs is not None), None)
        size = cs.size if cs else 10.0
        bold = bool(cs and cs.bold)
        line = ps.ls_value if ps.ls_type == "PERCENT" else 100.0
        key = (ALIGN.get(ps.align, "start"), round(ps.left, 1), round(ps.right, 1), round(ps.indent, 1), round(ps.prev, 1),
               round(ps.next, 1), round(line), round(size, 1), bold)

        def build(st):
            _el(S + "paragraph-properties", {FO + "text-align": key[0], FO + "margin-left": pt(ps.left), FO + "margin-right": pt(ps.right),
                                             FO + "text-indent": f"{ps.indent:.3f}pt", FO + "margin-top": pt(ps.prev),
                                             FO + "margin-bottom": pt(ps.next), FO + "line-height": f"{max(60, min(line, 300)):.0f}%"}, st)
            tp = {FO + "font-size": pt(size), S + "font-size-asian": pt(size)}
            if bold:
                tp.update({FO + "font-weight": "bold", S + "font-weight-asian": "bold"})
            _el(S + "text-properties", tp, st)
        return self.st.get("paragraph", key, build)

    def text_style(self, cs) -> str | None:
        if cs is None:
            return None
        key = (round(cs.size, 1), cs.bold, cs.italic, cs.underline, cs.color)

        def build(st):
            tp = {FO + "font-size": pt(cs.size), S + "font-size-asian": pt(cs.size), FO + "color": cs.color or "#000000"}
            if cs.bold:
                tp.update({FO + "font-weight": "bold", S + "font-weight-asian": "bold"})
            if cs.italic:
                tp.update({FO + "font-style": "italic", S + "font-style-asian": "italic"})
            if cs.underline:
                tp.update({S + "text-underline-style": "solid", S + "text-underline-width": "auto", S + "text-underline-color": "font-color"})
            _el(S + "text-properties", tp, st)
        return self.st.get("text", key, build)

    @staticmethod
    def _border(b) -> str:
        return "none" if (b is None or b.type in ("NONE", None)) else f"{max(b.width, 0.1):.2f}pt solid {b.color or '#000000'}"

    def cell_style(self, c) -> str:
        bf = c.bf
        key = (self._border(bf.left), self._border(bf.right), self._border(bf.top), self._border(bf.bottom), bf.fill, c.valign,
               tuple(round(m, 1) for m in c.margin))

        def build(st):
            a = {FO + "border-left": key[0], FO + "border-right": key[1], FO + "border-top": key[2], FO + "border-bottom": key[3],
                 S + "vertical-align": VALIGN.get(c.valign, "middle"), FO + "padding-left": pt(c.margin[0]),
                 FO + "padding-right": pt(c.margin[1]), FO + "padding-top": pt(c.margin[2]), FO + "padding-bottom": pt(c.margin[3])}
            if bf.fill and bf.fill.lower() not in ("none", "#ffffff"):
                a[FO + "background-color"] = bf.fill
            _el(S + "table-cell-properties", a, st)
        return self.st.get("table-cell", key, build)

    def graphic_style(self, kind: str, border: bool = False, wrap: str = "run-through", fill: str | None = None,
                      behind: bool = False) -> str:
        key = (kind, border, wrap, fill, behind)

        def build(st):
            st.set(S + "parent-style-name", "Frame" if kind == "frame" else "Graphics")
            a = {S + "wrap": wrap, S + "run-through": "background" if behind else "foreground",
                 S + "vertical-pos": "from-top", S + "vertical-rel": "page", S + "horizontal-pos": "from-left", S + "horizontal-rel": "page",
                 FO + "padding": "0.5pt", FO + "border": "0.5pt solid #000000" if border else "none"}
            if kind == "line":
                a = {S + "wrap": "run-through", S + "run-through": "foreground", D + "stroke": "solid", SVG + "stroke-color": "#000000",
                     SVG + "stroke-width": "0.5pt", S + "vertical-pos": "from-top", S + "vertical-rel": "page", S + "horizontal-pos": "from-left",
                     S + "horizontal-rel": "page"}
            if fill:
                a[FO + "background-color"] = fill
            _el(S + "graphic-properties", a, st)
        return self.st.get("graphic", key, build)

    # ------------------------------------------------------------------ content
    def paragraph(self, p: Para, parent) -> list:
        """Emit one paragraph; returns objects that must follow it at body level (inline tables)."""
        el = _el(T + "p", {T + "style-name": self.para_style(p)}, parent)
        after = []
        prev = ""
        for i in p.inlines:
            if i.kind in ("text", "tab", "br"):
                tsn = self.text_style(i.cs)
                span = _el(T + "span", {T + "style-name": tsn} if tsn else {}, el)
                txt = i.text if i.kind == "text" else ("\t" if i.kind == "tab" else "\n")
                encode_text(span, txt, prev)
                prev = txt[-1:] if txt else prev
            elif i.kind == "obj" and i.obj is not None:
                o = i.obj
                if isinstance(o, Table):
                    if o.pos.treat_as_char or o.pos.wrap in ("TOP_AND_BOTTOM",) and o.pos.vert_rel == "PARA":
                        after.append(o)
                    else:
                        self.floating_table(o, el)
                elif isinstance(o, Shape):
                    self.shape(o, el)
            elif i.kind in ("field_begin", "field_end", "ctrl"):
                continue
            else:
                self.note(f"inline_{i.kind}_dropped")
        return after

    def table(self, t: Table, parent):
        tname = self.st.get("table", (round(t.width, 1),), lambda st: _el(S + "table-properties", {
            S + "width": pt(t.width or 400), TB + "align": "left"}, st))
        tb = _el(TB + "table", {TB + "name": f"Table{t.nid}", TB + "style-name": tname}, parent)
        widths = [0.0] * t.n_cols
        for c in t.cells:
            if c.colspan == 1 and c.col < t.n_cols:
                widths[c.col] = max(widths[c.col], c.width)
        rest = [c for c in t.cells if c.colspan > 1]
        for c in rest:                                   # columns only known through spans: share the span width
            known = sum(widths[k] for k in range(c.col, min(c.col + c.colspan, t.n_cols)))
            missing = [k for k in range(c.col, min(c.col + c.colspan, t.n_cols)) if widths[k] == 0]
            for k in missing:
                widths[k] = max((c.width - known) / len(missing), 4.0)
        widths = [w if w > 0 else max((t.width or 400) / max(t.n_cols, 1), 4.0) for w in widths]
        for w in widths:
            cn = self.st.get("table-column", (round(w, 1),), lambda st, w=w: _el(S + "table-column-properties", {S + "column-width": pt(w)}, st))
            _el(TB + "table-column", {TB + "style-name": cn}, tb)
        grid = {}
        for c in t.cells:
            grid[(c.row, c.col)] = c
        covered = set()
        for c in t.cells:
            for r in range(c.row, c.row + c.rowspan):
                for k in range(c.col, c.col + c.colspan):
                    if (r, k) != (c.row, c.col):
                        covered.add((r, k))
        heights = {}
        for c in t.cells:
            if c.rowspan == 1:
                heights[c.row] = max(heights.get(c.row, 0.0), c.height)
        for r in range(t.n_rows):
            rn = self.st.get("table-row", (round(heights.get(r, 12.0), 1),), lambda st, h=heights.get(r, 12.0): _el(
                S + "table-row-properties", {S + "min-row-height": pt(h)}, st))
            tr = _el(TB + "table-row", {TB + "style-name": rn}, tb)
            for k in range(t.n_cols):
                c = grid.get((r, k))
                if c is None:
                    if (r, k) in covered:
                        _el(TB + "covered-table-cell", parent=tr)
                    else:
                        tc = _el(TB + "table-cell", {O + "value-type": "string"}, tr)
                        _el(T + "p", parent=tc)
                    continue
                a = {TB + "style-name": self.cell_style(c), O + "value-type": "string"}
                if c.colspan > 1:
                    a[TB + "number-columns-spanned"] = c.colspan
                if c.rowspan > 1:
                    a[TB + "number-rows-spanned"] = c.rowspan
                tc = _el(TB + "table-cell", a, tr)
                if not c.paras:
                    _el(T + "p", parent=tc)
                for p in c.paras:
                    for nested in self.paragraph(p, tc):
                        self.table(nested, tc)
        return tb

    def _page_pos(self, nid):
        b = self.boxes.get(nid)
        if b is None:
            return None
        return b.page + 1, b.x, b.y, b.w, b.h

    def floating_table(self, t: Table, anchor_p):
        pos = self._page_pos(t.nid)
        if pos is None:
            self.note("floating_table_without_position")
            return
        pg, x, y, w, h = pos
        fr = _el(D + "frame", {D + "style-name": self.graphic_style("frame"), T + "anchor-type": "page", T + "anchor-page-number": pg,
                                SVG + "x": pt(x), SVG + "y": pt(y), SVG + "width": pt(w), FO + "min-height": pt(h), D + "z-index": 0},
                 self.page_anchor_holder())
        tbx = _el(D + "text-box", parent=fr)
        self.table(t, tbx)

    def page_anchor_holder(self):
        """Page-anchored frames are children of the first paragraph-level element of the body (ODF allows them
        directly under office:text before the first paragraph)."""
        return self.text

    def shape(self, s: Shape, anchor_p):
        pos = self._page_pos(s.nid)
        inline = s.pos.treat_as_char
        if s.is_textbox:
            if pos is None:
                self.note("textbox_without_position")
                return
            pg, x, y, w, h = pos
            fr = _el(D + "frame", {D + "style-name": self.graphic_style("frame", border=s.line.type != "NONE" if s.line else False),
                                   T + "anchor-type": "page", T + "anchor-page-number": pg, SVG + "x": pt(x), SVG + "y": pt(y),
                                   SVG + "width": pt(w), SVG + "height": pt(h), D + "z-index": max(s.pos.z, 1)}, self.text)
            tbx = _el(D + "text-box", parent=fr)
            for p in s.paras or []:
                for nested in self.paragraph(p, tbx):
                    self.table(nested, tbx)
            return
        if s.kind == "pic":
            data = self.bins.get(s.bin_ref or "")
            if data is None or pos is None:
                self.note("picture_without_data_or_position")
                return
            ext = "png" if data[:4] == b"\x89PNG" else "jpg"
            name = f"Pictures/img{len(self.pictures) + 1}.{ext}"
            self.pictures[name] = data
            pg, x, y, w, h = pos
            behind = s.pos.wrap == "BEHIND_TEXT"
            if inline:
                fr = _el(D + "frame", {D + "style-name": self.graphic_style("graphic", wrap="none"), T + "anchor-type": "as-char",
                                       SVG + "width": pt(w), SVG + "height": pt(h)}, anchor_p)
            else:
                fr = _el(D + "frame", {D + "style-name": self.graphic_style("graphic", wrap="run-through", behind=behind),
                                       T + "anchor-type": "page", T + "anchor-page-number": pg, SVG + "x": pt(x), SVG + "y": pt(y),
                                       SVG + "width": pt(w), SVG + "height": pt(h), D + "z-index": 0 if behind else max(s.pos.z, 1)}, self.text)
            _el(D + "image", {XL + "href": name, XL + "type": "simple", XL + "show": "embed", XL + "actuate": "onLoad"}, fr)
            return
        if s.kind in ("line", "connectLine") and len(s.points) == 2 and pos is not None:
            pg = pos[0]
            rule = next((r for r in self.lay.rules if r.owner == s.nid), None)
            if rule is None:
                self.note("line_without_geometry")
                return
            _el(D + "line", {D + "style-name": self.graphic_style("line"), T + "anchor-type": "page", T + "anchor-page-number": pg,
                             SVG + "x1": pt(rule.x0), SVG + "y1": pt(rule.y0), SVG + "x2": pt(rule.x1), SVG + "y2": pt(rule.y1),
                             D + "z-index": 0}, self.text)
            return
        self.note(f"shape_{s.kind}_dropped")

    def build(self) -> tuple[bytes, dict]:
        for sec in self.ir.sections:
            for p in sec.paras:
                for t in self.paragraph(p, self.text):
                    self.table(t, self.text)
        # ODF requires page-anchored frames inside a paragraph-level parent: move them into an empty first paragraph
        holders = [e for e in self.text if e.tag in (D + "frame", D + "line")]
        if holders:
            p0 = etree.Element(T + "p")
            self.text.insert(0, p0)
            for e in holders:
                p0.append(e)
        return self.package(), {"conversion_losses": self.losses}

    # ------------------------------------------------------------------ package
    def styles_xml(self) -> bytes:
        sec = self.ir.sections[0] if self.ir.sections else None
        ps = sec.page if sec is not None else None
        w, h = (ps.width, ps.height) if ps else (595.28, 841.89)
        ml, mr, mt, mb = (ps.left + ps.gutter, ps.right, ps.top + ps.header, ps.bottom + ps.footer) if ps else (56.7, 56.7, 56.7, 56.7)
        root = etree.Element(O + "document-styles", {O + "version": "1.3"}, nsmap={k: v for k, v in NS.items() if k != "manifest"})
        styles = _el(O + "styles", parent=root)
        for name, fam in (("Standard", "paragraph"), ("Frame", "graphic"), ("Graphics", "graphic")):
            _el(S + "style", {S + "name": name, S + "family": fam}, styles)
        auto = _el(O + "automatic-styles", parent=root)
        pl = _el(S + "page-layout", {S + "name": "pm1"}, auto)
        _el(S + "page-layout-properties", {FO + "page-width": pt(w), FO + "page-height": pt(h), FO + "margin-left": pt(ml),
                                           FO + "margin-right": pt(mr), FO + "margin-top": pt(mt), FO + "margin-bottom": pt(mb),
                                           S + "print-orientation": "landscape" if w > h else "portrait"}, pl)
        ms = _el(O + "master-styles", parent=root)
        _el(S + "master-page", {S + "name": "Standard", S + "page-layout-name": "pm1"}, ms)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    def package(self) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
            z.writestr("content.xml", etree.tostring(self.content, xml_declaration=True, encoding="UTF-8"))
            z.writestr("styles.xml", self.styles_xml())
            for name, data in self.pictures.items():
                z.writestr(name, data)
            man = etree.Element(f"{{{NS['manifest']}}}manifest", {f"{{{NS['manifest']}}}version": "1.3"}, nsmap={"manifest": NS["manifest"]})
            M = f"{{{NS['manifest']}}}"
            _el(M + "file-entry", {M + "full-path": "/", M + "media-type": "application/vnd.oasis.opendocument.text", M + "version": "1.3"}, man)
            for n in ("content.xml", "styles.xml"):
                _el(M + "file-entry", {M + "full-path": n, M + "media-type": "text/xml"}, man)
            for n in self.pictures:
                _el(M + "file-entry", {M + "full-path": n, M + "media-type": "image/png" if n.endswith("png") else "image/jpeg"}, man)
            z.writestr("META-INF/manifest.xml", etree.tostring(man, xml_declaration=True, encoding="UTF-8"))
        return out.getvalue()


def hwpx_to_odt(hwpx: bytes) -> tuple[bytes, dict]:
    """Write a HWPX document (e.g. a baseline's reconstruction) as ODT, through the IR and reference layout."""
    from ..document_ir.normalize import build_ir
    from .hwpx import HwpxPackage
    pkg = HwpxPackage.load(hwpx)
    ir = build_ir(pkg)
    bins = {}
    for n in pkg.names():
        if n.lower().startswith("bindata/"):
            bins[n.split("/")[-1].rsplit(".", 1)[0]] = pkg.read(n)
            bins[n.split("/")[-1]] = pkg.read(n)
    return OdtWriter(ir, bins).build()
