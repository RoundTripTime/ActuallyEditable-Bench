"""ODT -> HWPX view converter (lxml), used as the layout / hit-testing view of the native ODT editor and as a better
ODT normaliser than the odfpy text+table conversion in odt.py.

Covered: page size and margins (first master page), paragraph / heading text with ODF white-space processing,
lists (paragraphs of list items), character size and bold, paragraph alignment / margins / indent (style
inheritance through automatic styles, styles.xml and default styles), tables with column widths, row heights,
spanned and covered cells, nested header-row / column groups, cell borders, text frames (draw:frame + draw:text-box)
and images. Session ids (attribute ``fb:nid``) of native elements are copied to the view nodes. Anything else is
counted in ``conversion_losses``."""
from __future__ import annotations

import io
import re
import zipfile

from lxml import etree

from ..document_ir.normalize import NID
from .hwpx import HP
from .hwpx_build import HwpxBuilder, make_p, make_pic, make_table, make_textbox

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}
O, S, T, TB, D, FO, SVG, XL = (f"{{{NS[k]}}}" for k in ("office", "style", "text", "table", "draw", "fo", "svg", "xlink"))
PARA_TAGS = (T + "p", T + "h")
ALIGN = {"center": "CENTER", "end": "RIGHT", "right": "RIGHT", "justify": "JUSTIFY", "start": "LEFT", "left": "LEFT"}
UNIT = {"pt": 1.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "in": 72.0, "pc": 12.0, "px": 0.75}


def length_pt(v, default: float | None = None) -> float | None:
    m = re.match(r"\s*(-?[\d.]+)\s*([a-z]*)", v or "")
    if not m:
        return default
    return float(m.group(1)) * UNIT.get(m.group(2) or "pt", 1.0)


SERIF_RX = re.compile(r"(times|serif(?!.*sans)|georgia|cambria|garamond|book antiqua|palatino|century|명조|바탕|batang|myeongjo|mincho|明朝|song|宋|minchō)", re.I)


def is_serif(family: str) -> bool:
    return bool(family) and bool(SERIF_RX.search(family)) and "sans" not in family.lower()


def secure(data: bytes):
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True))


# ----------------------------------------------------------------------------- text model (shared with the native editor)
def _walk_text(p):
    """Yield (kind, node, attr, raw) over a paragraph's own content in document order.
    kind: 'chars' (character data of node.text / node.tail), 's' (text:s, c spaces), 'tab', 'br'.
    Frames, notes and annotations anchored in the paragraph are skipped (their text has its own paragraphs)."""
    skip = {D + "frame", D + "a", T + "note", O + "annotation", O + "annotation-end", T + "bookmark-start", T + "bookmark-end",
            T + "bookmark", T + "soft-page-break", T + "reference-mark", T + "reference-mark-start", T + "reference-mark-end",
            D + "custom-shape", D + "rect", D + "connector", D + "line", D + "ellipse", D + "g", D + "polygon", D + "path"}

    def rec(el):
        if el.text:
            yield ("chars", el, "text", el.text)
        for ch in el:
            if not isinstance(ch.tag, str):
                pass
            elif ch.tag == T + "s":
                yield ("s", ch, None, int(ch.get(T + "c") or 1))
            elif ch.tag == T + "tab":
                yield ("tab", ch, None, 1)
            elif ch.tag == T + "line-break":
                yield ("br", ch, None, 1)
            elif ch.tag in skip:
                pass
            else:
                yield from rec(ch)
            if ch.tail:
                yield ("chars", ch, "tail", ch.tail)
    yield from rec(p)


def own_atoms(p) -> list:
    """Visible characters of a paragraph after ODF white-space processing, as atoms
    (char, kind, node, attr, raw_index): raw_index is the index in node.text/tail for 'chars' atoms, the space number
    within a text:s for 's' atoms. Collapsed white space produces no atom."""
    out = []
    prev_space = True                                   # leading white space of a paragraph is ignored
    for kind, node, attr, raw in _walk_text(p):
        if kind == "chars":
            for i, ch in enumerate(raw):
                if ch in " \t\n\r":
                    if prev_space:
                        continue
                    out.append((" ", "chars", node, attr, i))
                    prev_space = True
                else:
                    out.append((ch, "chars", node, attr, i))
                    prev_space = False
        elif kind == "s":
            for j in range(raw):
                out.append((" ", "s", node, None, j))
            prev_space = prev_space if raw == 0 else True   # text:s spaces are kept; a literal space after them collapses
        elif kind == "tab":
            out.append(("\t", "tab", node, None, 0))
            prev_space = False
        else:
            out.append(("\n", "br", node, None, 0))
            prev_space = False
    # trailing collapsible space at paragraph end is not displayed
    while out and out[-1][0] == " " and out[-1][1] == "chars":
        out.pop()
    return out


def own_text(p) -> str:
    return "".join(a[0] for a in own_atoms(p))


# ----------------------------------------------------------------------------- styles
class Styles:
    def __init__(self, content, styles_root):
        self.by_family: dict[tuple, etree._Element] = {}
        self.default: dict[str, etree._Element] = {}
        for root in (styles_root, content):
            if root is None:
                continue
            for st in root.iter(S + "style"):
                self.by_family[(st.get(S + "family"), st.get(S + "name"))] = st
            for st in root.iter(S + "default-style"):
                self.default[st.get(S + "family")] = st

    def chain(self, family: str, name: str | None):
        seen = set()
        while name and (family, name) not in seen:
            seen.add((family, name))
            st = self.by_family.get((family, name))
            if st is None:
                break
            yield st
            name = st.get(S + "parent-style-name")
        if family in self.default:
            yield self.default[family]

    def prop(self, family: str, name: str | None, props_tag: str, attr: str):
        for st in self.chain(family, name):
            pr = st.find(S + props_tag)
            if pr is not None and pr.get(attr) is not None:
                return pr.get(attr)
        return None


# ----------------------------------------------------------------------------- converter
class OdtView:
    def __init__(self, data: bytes, content=None):
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.content = content if content is not None else secure(self.z.read("content.xml"))
        self.styles_root = secure(self.z.read("styles.xml")) if "styles.xml" in self.z.namelist() else None
        self.st = Styles(self.content, self.styles_root)
        self.loss: dict[str, int] = {}
        pw, ph, mar = 595.28, 841.89, [56.7, 56.7, 56.7, 56.7]
        if self.styles_root is not None:
            mp = next(self.styles_root.iter(S + "master-page"), None)
            pl_name = mp.get(S + "page-layout-name") if mp is not None else None
            for pl in self.styles_root.iter(S + "page-layout"):
                if pl_name and pl.get(S + "name") != pl_name:
                    continue
                pp = pl.find(S + "page-layout-properties")
                if pp is not None:
                    pw = length_pt(pp.get(FO + "page-width"), pw)
                    ph = length_pt(pp.get(FO + "page-height"), ph)
                    mar = [length_pt(pp.get(FO + f"margin-{k}"), d) for k, d in zip(("left", "right", "top", "bottom"), mar)]
                break
        self.b = HwpxBuilder(pw, ph, (mar[0], mar[1], mar[2], mar[3], 0.0, 0.0))
        self.body_w = pw - mar[0] - mar[1]
        self.origin = (mar[0], mar[2])

    def note(self, k: str) -> None:
        self.loss[k] = self.loss.get(k, 0) + 1

    def _char_props(self, p) -> dict:
        size = bold = None
        names = [p.get(T + "style-name")]
        for sp in p.iter(T + "span"):
            if "".join(sp.itertext()).strip():
                names.insert(0, sp.get(T + "style-name"))
                break
        for nm in names:
            fam = "text" if nm != p.get(T + "style-name") else "paragraph"
            if size is None:
                v = self.st.prop(fam, nm, "text-properties", FO + "font-size")
                if v and not v.endswith("%"):
                    size = length_pt(v)
            if bold is None:
                v = self.st.prop(fam, nm, "text-properties", FO + "font-weight")
                if v:
                    bold = v == "bold" or (v.isdigit() and int(v) >= 600)
        face = None
        for nm in names:
            fam = "text" if nm != p.get(T + "style-name") else "paragraph"
            face = face or self.st.prop(fam, nm, "text-properties", S + "font-name") or \
                self.st.prop(fam, nm, "text-properties", FO + "font-family")
        return {"size": size or 10.0, "bold": bool(bold), "serif": is_serif(self.font_family(face))}

    def font_family(self, name: str | None) -> str:
        if not name:
            return ""
        for root in (self.content, self.styles_root):
            if root is None:
                continue
            for ff in root.iter(S + "font-face"):
                if ff.get(S + "name") == name:
                    return ff.get(SVG + "font-family") or name
        return name

    def make_para(self, p):
        nm = p.get(T + "style-name")
        g = lambda a: self.st.prop("paragraph", nm, "paragraph-properties", a)  # noqa: E731
        cp = self._char_props(p)
        lh = g(FO + "line-height")
        line = float(lh[:-1]) if lh and lh.endswith("%") else 120.0
        para_id = self.b.head.para_pr(align=ALIGN.get(g(FO + "text-align") or "start", "LEFT"), line_spacing=max(60.0, min(line, 300.0)),
                                      left=length_pt(g(FO + "margin-left"), 0.0), indent=length_pt(g(FO + "text-indent"), 0.0),
                                      prev=length_pt(g(FO + "margin-top"), 0.0), next_=length_pt(g(FO + "margin-bottom"), 0.0))
        char_id = self.b.head.char_pr(size=cp["size"], bold=cp["bold"], serif=cp["serif"])
        hp_p = make_p(own_text(p), char_id, para_id)
        if p.get(NID):
            hp_p.set(NID, p.get(NID))
        return hp_p

    def _starts_page(self, p) -> bool:
        """ODF: a paragraph whose style names a master page, or breaks before with 'page', starts a new page."""
        nm = p.get(T + "style-name")
        for st in self.st.chain("paragraph", nm):
            if st.get(S + "master-page-name"):
                return True
            pp = st.find(S + "paragraph-properties")
            if pp is not None and pp.get(FO + "break-before") == "page":
                return True
        return False

    @staticmethod
    def _numbered_page_frame(fr) -> bool:
        return (fr.get(T + "anchor-type") or "") == "page" and bool(fr.get(T + "anchor-page-number"))

    def _frames_of(self, p):
        for fr in p.iter(D + "frame"):
            if next(fr.iterancestors(*PARA_TAGS), None) is p and not self._numbered_page_frame(fr):
                yield fr

    def objects(self, p, hp_p, frames=None) -> None:
        for fr in (self._frames_of(p) if frames is None else frames):
            w = length_pt(fr.get(SVG + "width"), None) or length_pt(fr.get(FO + "min-width"), 60.0)
            h = length_pt(fr.get(SVG + "height"), None) or length_pt(fr.get(FO + "min-height"), 20.0)
            anchor = fr.get(T + "anchor-type") or "paragraph"
            x = length_pt(fr.get(SVG + "x"), 0.0)
            y = length_pt(fr.get(SVG + "y"), 0.0)
            gst = fr.get(D + "style-name")
            hrel = self.st.prop("graphic", gst, "graphic-properties", S + "horizontal-rel") or ""
            vrel = self.st.prop("graphic", gst, "graphic-properties", S + "vertical-rel") or ""
            if anchor != "page":                     # positions relative to the page itself need no margin offset
                x += 0.0 if hrel == "page" else self.origin[0]
                y += 0.0 if vrel == "page" else self.origin[1]
            inline = anchor == "as-char"
            tb, img = fr.find(D + "text-box"), fr.find(D + "image")
            if tb is not None:
                qs = [q for q in tb.iter(*PARA_TAGS) if next(q.iterancestors(D + "text-box"), None) is tb]
                inner = [self.make_para(q) for q in qs] or None
                gs = fr.get(D + "style-name")
                gp = lambda a: self.st.prop("graphic", gs, "graphic-properties", a)  # noqa: E731
                pad = length_pt(gp(FO + "padding-left") or gp(FO + "padding"), 0.5)
                if (gp(D + "auto-grow-width") or "") == "true" and qs:
                    # the box is as wide as its text in the producing application (e.g. LibreOffice's PDF import makes
                    # one auto-growing box per text line): keep it unwrapped with the reference fonts
                    from ..document_ir.fonts import text_width
                    need = 0.0
                    for q in qs:
                        cp = self._char_props(q)
                        need = max(need, text_width(own_text(q), "바탕" if cp["serif"] else "sans", cp["size"], cp["bold"]))
                    if need + 2 * pad > w:
                        self.note("auto_grow_width_box_widened")
                        w = need + 2 * pad + 0.5
                el = make_textbox(self.b.next_id(), x, y, max(w, 4), max(h, 4), inner, border=False, inline=inline, margin=pad)
            elif img is not None:
                href = img.get(XL + "href") or ""
                ext = href.rsplit(".", 1)[-1].lower()
                if href in self.z.namelist() and ext in ("png", "jpg", "jpeg", "bmp"):
                    el = make_pic(self.b.next_id(), self.b.add_bin(self.z.read(href), ext), x, y, max(w, 2), max(h, 2), inline=inline,
                                  wrap="BEHIND_TEXT")
                else:
                    self.note(f"image_not_converted:{ext}")
                    continue
            else:
                self.note("frame_kind_dropped")
                continue
            if fr.get(NID):
                el.set(NID, fr.get(NID))
            self.b.attach(el, hp_p)
        if frames is not None:
            return
        for tag in ("custom-shape", "rect", "connector", "line", "ellipse", "g", "polygon", "path"):
            for _ in p.iter(D + tag):
                self.note(f"drawing_{tag}_dropped")

    def _expand_cols(self, tbl) -> list[float]:
        widths = []
        for col in tbl.iter(TB + "table-column"):
            if next(col.iterancestors(TB + "table"), None) is not tbl:
                continue
            w = length_pt(self.st.prop("table-column", col.get(TB + "style-name"), "table-column-properties", S + "column-width"), None)
            widths += [w] * int(col.get(TB + "number-columns-repeated") or 1)
        if not widths or any(w is None for w in widths):
            n = max(1, len(widths))
            known = [w for w in widths if w]
            fill = (self.body_w - sum(known)) / max(1, n - len(known)) if len(known) < n else 0
            widths = [w or max(fill, 20.0) for w in widths] or [self.body_w]
        return widths

    def _border(self, cell) -> dict:
        nm = cell.get(TB + "style-name")
        out = {}
        allb = self.st.prop("table-cell", nm, "table-cell-properties", FO + "border")
        for side in ("left", "right", "top", "bottom"):
            v = self.st.prop("table-cell", nm, "table-cell-properties", FO + f"border-{side}") or allb
            out[side] = bool(v) and v.split()[0] not in ("none", "hidden") and (length_pt(v.split()[0], 1.0) or 0) > 0
        return out

    def table(self, tbl):
        grid = self._expand_cols(tbl)
        rows = [r for r in tbl.iter(TB + "table-row") if next(r.iterancestors(TB + "table"), None) is tbl]
        cells, heights = [], []
        for ri, tr in enumerate(rows):
            rh = self.st.prop("table-row", tr.get(TB + "style-name"), "table-row-properties", S + "row-height") or \
                self.st.prop("table-row", tr.get(TB + "style-name"), "table-row-properties", S + "min-row-height")
            heights.append(max(length_pt(rh, 16.0), 8.0))
            col = 0
            for tc in tr:
                if tc.tag == TB + "covered-table-cell":
                    col += int(tc.get(TB + "number-columns-repeated") or 1)
                    continue
                if tc.tag != TB + "table-cell":
                    continue
                rep = int(tc.get(TB + "number-columns-repeated") or 1)
                for _ in range(rep):
                    paras = [self.make_para(q) for q in tc if q.tag in PARA_TAGS]
                    for lst in tc.iter(T + "list"):
                        if next(lst.iterancestors(TB + "table-cell"), None) is tc:
                            paras += [self.make_para(q) for q in lst.iter(*PARA_TAGS)]
                    if any(ch.tag == TB + "table" for ch in tc.iter(TB + "table") if ch is not tbl):
                        self.note("nested_table_flattened")
                    bd = self._border(tc)
                    c = {"row": ri, "col": col, "colspan": int(tc.get(TB + "number-columns-spanned") or 1),
                         "rowspan": int(tc.get(TB + "number-rows-spanned") or 1), "paras": paras or None, "valign": "CENTER",
                         "bf": self.b.head.border_fill(bd["left"], bd["right"], bd["top"], bd["bottom"]), "src": tc if rep == 1 else None}
                    cells.append(c)
                    col += 1                             # the spanned columns follow as covered-table-cell elements
        n_cols = len(grid)
        if any(c["col"] >= n_cols for c in cells):
            extra = max(c["col"] + c["colspan"] for c in cells) - n_cols
            grid += [20.0] * extra
            self.note("columns_added_for_cells")
            n_cols = len(grid)
        for c in cells:
            c["colspan"] = min(c["colspan"], n_cols - c["col"])
            c["rowspan"] = min(c["rowspan"], len(rows) - c["row"])
        out = make_table(self.b.next_id(), grid, heights, cells, self.b.head.border_fill(), inline=True)
        if tbl.get(NID):
            out.set(NID, tbl.get(NID))
        by_addr = {(c["row"], c["col"]): c["src"] for c in cells}
        for tc_el in out.iter(HP + "tc"):
            a = tc_el.find(HP + "cellAddr")
            src = by_addr.get((int(a.get("rowAddr")), int(a.get("colAddr"))))
            if src is not None and src.get(NID):
                tc_el.set(NID, src.get(NID))
        return out

    def _body_items(self, node):
        for ch in node:
            if not isinstance(ch.tag, str):
                continue
            if ch.tag in PARA_TAGS:
                yield ("p", ch)
            elif ch.tag == TB + "table":
                yield ("table", ch)
            elif ch.tag in (T + "list", T + "list-item", T + "list-header", T + "section", T + "index-body") or ch.tag.endswith("index"):
                yield from self._body_items(ch)
            elif ch.tag in (T + "sequence-decls", T + "variable-decls", O + "forms", T + "user-field-decls", T + "tracked-changes"):
                continue
            elif ch.tag == D + "frame" and (ch.get(T + "anchor-type") or "") == "page":
                yield ("pageframe", ch)
            elif ch.tag.startswith(D):
                self.note("page_anchored_drawing_dropped")
            else:
                self.note("body_element_dropped:" + etree.QName(ch).localname)

    def build(self):
        body = self.content.find(O + "body/" + O + "text")
        if body is not None:
            items = list(self._body_items(body))
            # page-anchored frames with a page number (body-level draw:frame as LibreOffice's PDF import writes them, or
            # inside a paragraph as the in-house ODT writer does) are attached to the body paragraph that starts that page
            # (pages counted by explicit page breaks); pages beyond the body get empty holder paragraphs at the end.
            # Positions are relative to the paper, so the holder's own position does not matter.
            pages: dict[int, list] = {}
            for kind, el in items:
                if kind == "pageframe":
                    pages.setdefault(max(1, int(el.get(T + "anchor-page-number") or 1)), []).append(el)
                elif kind == "p":                    # frames inside a body paragraph anchored to an explicit page number
                    for fr in el.iter(D + "frame"):
                        if next(fr.iterancestors(*PARA_TAGS), None) is el and self._numbered_page_frame(fr):
                            pages.setdefault(max(1, int(fr.get(T + "anchor-page-number"))), []).append(fr)
            if pages:
                self.note("page_anchored_frames_placed_by_page_number")
            first, page = True, 1
            for kind, el in items:
                if kind == "pageframe":
                    continue
                if kind == "p":
                    hp = self.make_para(el)
                    brk = not first and self._starts_page(el)
                    page += int(brk)
                    target = self.b.add_para("", hp.get("charPrIDRef", "0"), hp.get("paraPrIDRef"), page_break=brk)
                    run = [r for r in target if r.tag == HP + "run"][-1]
                    run.set("charPrIDRef", hp[0].get("charPrIDRef"))
                    t = run.find(HP + "t")
                    src_t = hp[0][0]
                    t.text = src_t.text
                    for ch in list(src_t):
                        t.append(ch)
                    if el.get(NID):
                        target.set(NID, el.get(NID))
                    self.objects(el, target)
                else:
                    target = self.b.add_para("", "0", self.b.head.para_pr(align="LEFT", line_spacing=100))
                    self.b.attach(self.table(el), target)
                if first or (kind == "p" and brk):
                    self.objects(None, target, frames=pages.pop(page, []))
                first = False
            if first and pages:                       # no body item at all
                target = self.b.add_para("", "0", self.b.head.para_pr(align="LEFT", line_spacing=100))
                self.objects(None, target, frames=pages.pop(1, []))
                first = False
            for k in sorted(pages):
                if k < page:                          # a page the flow reached without a break-starting paragraph
                    self.note("page_frame_attached_to_last_paragraph")
                    self.objects(None, target, frames=pages[k])
                    continue
                while page < k:
                    page += 1
                    target = self.b.add_para("", "0", self.b.head.para_pr(align="LEFT", line_spacing=100), page_break=True)
                self.objects(None, target, frames=pages[k])
        return self.b.package(), {"conversion_losses": self.loss}


def odt_to_hwpx_view(data: bytes) -> tuple[bytes, dict]:
    pkg, info = OdtView(data).build()
    return pkg.to_bytes(), info
