"""HWPX XML -> Semantic Document IR, session node ids, semantic fingerprint, native object statistics."""
from __future__ import annotations

from collections import Counter

from ..formats.hwpx import FB, HC, HP, HwpxPackage, local
from ..util import jhash
from .schema import Border, Cell, DocIR, Inline, ObjPos, PageSetup, Para, Section, Shape, Table, hu
from .styles import StyleTable

SPECIAL_CHARS = {"tab": "\t", "lineBreak": "\n", "fwSpace": "　", "nbSpace": " ", "hyphen": "-"}
DRAW_TAGS = {"rect", "line", "ellipse", "arc", "polygon", "curve", "connectLine", "pic", "container", "ole", "equation",
             "textart", "video", "chart"}
FORM_TAGS = {"checkBtn", "radioBtn", "btn", "comboBox", "edit", "listBox", "scrollBar"}
OBJECT_TAGS = DRAW_TAGS | FORM_TAGS | {"tbl"}
KNOWN_CTRL = {"colPr", "fieldBegin", "fieldEnd", "header", "footer", "footNote", "endNote", "autoNum", "newNum", "pageNum",
              "pageHiding", "pageNumCtrl", "bookmark", "indexmark", "hiddenComment"}
KNOWN_RUN_CHILD = OBJECT_TAGS | {"t", "ctrl", "secPr", "compose", "dutmal"}
KNOWN_T_CHILD = set(SPECIAL_CHARS) | {"markpenBegin", "markpenEnd", "titleMark", "insertBegin", "insertEnd", "deleteBegin", "deleteEnd"}
STAMP_TAGS = {HP + t for t in OBJECT_TAGS | {"p", "tc", "fieldBegin"}}
NID = FB + "nid"
NEXT = FB + "next"


# ----------------------------------------------------------------------------- session ids
def stamp(pkg: HwpxPackage) -> None:
    """Assign deterministic session ids (document order). Idempotent; ids survive edits within a session."""
    for si, root in enumerate(pkg.sections):
        if root.get(NEXT) is not None:
            continue
        n = 0
        for el in root.iter():
            if el.tag in STAMP_TAGS:
                el.set(NID, f"s{si}n{n}")
                n += 1
        root.set(NEXT, str(n))


def stamp_new(root, el) -> None:
    """Give ids to a freshly created subtree (ids continue the section counter => deterministic)."""
    si = 0
    first = root.find(".//" + HP + "p")
    if first is not None and first.get(NID):
        si = int(first.get(NID)[1:].split("n")[0])
    n = int(root.get(NEXT) or 0)
    for e in el.iter():
        if e.tag in STAMP_TAGS and e.get(NID) is None:
            e.set(NID, f"s{si}n{n}")
            n += 1
    root.set(NEXT, str(n))


def find_nid(pkg: HwpxPackage, nid: str):
    si = int(nid[1:].split("n")[0])
    if si >= len(pkg.sections):
        return None
    r = pkg.sections[si].xpath(".//*[@f:nid=$v]", namespaces={"f": FB[1:-1]}, v=nid)
    return r[0] if r else None


# ----------------------------------------------------------------------------- paragraph text model
def iter_slots(p_el):
    """Yield (kind, element, payload, run). kinds: 'text' (payload='text'|'tail'), 'char' (payload=the char),
    'marker' (zero-width control), 'obj' (zero-width object)."""
    for run in p_el:
        if run.tag != HP + "run":
            continue
        for ch in run:
            if not isinstance(ch.tag, str):
                continue
            if ch.tag == HP + "t":
                yield ("text", ch, "text", run)
                for sub in ch:
                    if not isinstance(sub.tag, str):
                        continue
                    name = local(sub)
                    if name in SPECIAL_CHARS:
                        yield ("char", sub, SPECIAL_CHARS[name], run)
                    else:
                        yield ("marker", sub, name, run)
                    yield ("text", sub, "tail", run)
            elif ch.tag == HP + "ctrl":
                for c in ch:
                    if isinstance(c.tag, str):
                        yield ("marker", c, local(c), run)
            else:
                yield ("obj", ch, local(ch), run)


def slot_text(el, which: str) -> str:
    return (el.text if which == "text" else el.tail) or ""


def para_text(p_el) -> str:
    out = []
    for kind, el, payload, _ in iter_slots(p_el):
        if kind == "text":
            out.append(slot_text(el, payload))
        elif kind == "char":
            out.append(payload)
    return "".join(out)


# ----------------------------------------------------------------------------- IR construction
class _Builder:
    def __init__(self, pkg: HwpxPackage):
        self.pkg = pkg
        self.st = StyleTable(pkg.header)
        self.ir = DocIR(fonts=self.st.font_names())
        self.counters: dict = {}

    def nid(self, el) -> str:
        v = el.get(NID)
        if v is None:
            raise ValueError("document not stamped")
        return v

    def obj_pos(self, el) -> ObjPos:
        pos = el.find(HP + "pos")
        om = el.find(HP + "outMargin")
        p = ObjPos()
        p.wrap = el.get("textWrap") or "TOP_AND_BOTTOM"
        try:
            p.z = int(el.get("zOrder") or 0)
        except ValueError:
            p.z = 0
        if pos is not None:
            p.treat_as_char = pos.get("treatAsChar") == "1"
            p.vert_rel = pos.get("vertRelTo") or "PARA"
            p.horz_rel = pos.get("horzRelTo") or "COLUMN"
            p.vert_align = pos.get("vertAlign") or "TOP"
            p.horz_align = pos.get("horzAlign") or "LEFT"
            p.vert_off, p.horz_off = hu(pos.get("vertOffset")), hu(pos.get("horzOffset"))
            p.flow_with_text = pos.get("flowWithText") == "1"
            p.allow_overlap = pos.get("allowOverlap") == "1"
        if om is not None:
            p.margin = (hu(om.get("left")), hu(om.get("right")), hu(om.get("top")), hu(om.get("bottom")))
        return p

    def size(self, el) -> tuple[float, float, bool]:
        sz = el.find(HP + "sz")
        if sz is not None:
            return hu(sz.get("width")), hu(sz.get("height")), sz.get("protect") == "1"
        for t in ("curSz", "orgSz"):
            c = el.find(HP + t)
            if c is not None and hu(c.get("width")) > 0:
                return hu(c.get("width")), hu(c.get("height")), False
        return 0.0, 0.0, False

    def sublist_paras(self, parent, owner) -> tuple[list[Para], str]:
        sl = parent.find(HP + "subList")
        if sl is None:
            return [], "TOP"
        return [self.para(p, owner) for p in sl if p.tag == HP + "p"], sl.get("vertAlign") or "TOP"

    def table(self, el, parent) -> Table:
        w, h, _ = self.size(el)
        t = Table(self.nid(el), el, int(el.get("rowCnt") or 0), int(el.get("colCnt") or 0), pos=self.obj_pos(el), width=w, height=h,
                  cell_spacing=hu(el.get("cellSpacing")), repeat_header=el.get("repeatHeader") == "1",
                  page_break=el.get("pageBreak") or "CELL", parent=parent)
        im = el.find(HP + "inMargin")
        if im is not None:
            t.in_margin = (hu(im.get("left")), hu(im.get("right")), hu(im.get("top")), hu(im.get("bottom")))
        for tr in el:
            if tr.tag != HP + "tr":
                continue
            for tc in tr:
                if tc.tag != HP + "tc":
                    continue
                addr, span, sz, mg = tc.find(HP + "cellAddr"), tc.find(HP + "cellSpan"), tc.find(HP + "cellSz"), tc.find(HP + "cellMargin")
                c = Cell(self.nid(tc), tc, int(addr.get("rowAddr") or 0) if addr is not None else 0,
                         int(addr.get("colAddr") or 0) if addr is not None else 0,
                         max(1, int(span.get("rowSpan") or 1)) if span is not None else 1,
                         max(1, int(span.get("colSpan") or 1)) if span is not None else 1,
                         hu(sz.get("width")) if sz is not None else 0.0, hu(sz.get("height")) if sz is not None else 0.0,
                         header=tc.get("header") == "1", protect=tc.get("protect") == "1", table=t)
                c.margin = t.in_margin
                if tc.get("hasMargin") == "1" and mg is not None:
                    c.margin = (hu(mg.get("left")), hu(mg.get("right")), hu(mg.get("top")), hu(mg.get("bottom")))
                c.bf = self.st.border_fill(tc.get("borderFillIDRef"))
                c.paras, c.valign = self.sublist_paras(tc, c)
                self.ir.nodes[c.nid] = c
                t.cells.append(c)
        if t.cells:
            t.n_rows = max(t.n_rows, max(c.row + c.rowspan for c in t.cells))
            t.n_cols = max(t.n_cols, max(c.col + c.colspan for c in t.cells))
        self.ir.nodes[t.nid] = t
        return t

    def shape(self, el, parent) -> Shape:
        kind = local(el)
        w, h, prot = self.size(el)
        s = Shape(self.nid(el), el, kind, pos=self.obj_pos(el), width=w, height=h, size_protect=prot, parent=parent)
        ls = el.find(HP + "lineShape")
        if ls is not None:
            style = ls.get("style") or "SOLID"
            s.line = Border(style, max(hu(ls.get("width")), 0.1), ls.get("color") or "#000000")
        elif kind in ("rect", "ellipse", "line", "polygon", "arc", "curve", "connectLine"):
            s.line = Border("SOLID", 0.34, "#000000")
        fb = el.find(HC + "fillBrush")
        if fb is not None:
            wb = fb.find(HC + "winBrush")
            if wb is not None and (wb.get("faceColor") or "none").lower() != "none":
                s.fill = wb.get("faceColor")
        dt = el.find(HP + "drawText")
        if dt is not None:
            s.paras, s.valign = self.sublist_paras(dt, s)
            tm = dt.find(HP + "textMargin")
            if tm is not None:
                s.text_margin = (hu(tm.get("left")), hu(tm.get("right")), hu(tm.get("top")), hu(tm.get("bottom")))
        img = el.find(HC + "img")
        if img is not None:
            s.bin_ref = img.get("binaryItemIDRef")
        if kind in ("line", "connectLine"):
            a, b = el.find(HC + "startPt"), el.find(HC + "endPt")
            if a is not None and b is not None:
                s.points = [(hu(a.get("x")), hu(a.get("y"))), (hu(b.get("x")), hu(b.get("y")))]
        elif kind in ("polygon", "curve"):
            s.points = [(hu(p.get("x")), hu(p.get("y"))) for p in el.findall(HC + "pt")]
        elif kind == "container":
            for ch in el:
                if isinstance(ch.tag, str) and local(ch) in DRAW_TAGS:
                    child = self.shape(ch, s)
                    off = ch.find(HP + "offset")
                    child.props["group_offset"] = (hu(off.get("x")), hu(off.get("y"))) if off is not None else (0.0, 0.0)
                    s.children.append(child)
        elif kind == "equation":
            sc = el.find(HP + "script")
            s.props["script"] = (sc.text or "") if sc is not None else ""
        elif kind in FORM_TAGS:
            s.props.update({"caption": el.get("caption") or "", "value": el.get("value") or "", "name": el.get("name") or "",
                            "group": el.get("radioGroupName") or el.get("groupName") or ""})
            if kind == "edit":
                tx = el.find(HP + "text")
                s.props["text"] = (tx.text or "") if tx is not None else ""
        self.ir.nodes[s.nid] = s
        return s

    def para(self, el, parent) -> Para:
        ps = self.st.para(el.get("paraPrIDRef"))
        p = Para(self.nid(el), el, ps, page_break=el.get("pageBreak") == "1", column_break=el.get("columnBreak") == "1", parent=parent)
        off = 0
        head = self.st.heading_text(ps, self.counters) if ps.heading != "NONE" else ""
        first_cs = None
        for kind, sub, payload, run in iter_slots(el):
            cs = self.st.char(run.get("charPrIDRef"))
            first_cs = first_cs or cs
            if kind == "text":
                txt = slot_text(sub, payload)
                if txt:
                    p.inlines.append(Inline("text", txt, cs, el=sub, meta={"start": off, "slot": payload}))
                    off += len(txt)
            elif kind == "char":
                p.inlines.append(Inline("tab" if payload == "\t" else "br" if payload == "\n" else "text", payload, cs, el=sub, meta={"start": off}))
                off += 1
            elif kind == "marker":
                if payload in ("fieldBegin", "fieldEnd"):
                    meta = {"start": off}
                    if payload == "fieldBegin":
                        meta.update({"type": sub.get("type"), "id": sub.get("id"), "name": sub.get("name") or "", "nid": sub.get(NID),
                                     "command": "".join(x.text or "" for x in sub.iter(HP + "stringParam"))})
                    else:
                        meta["begin"] = sub.get("beginIDRef")
                    p.inlines.append(Inline("field_begin" if payload == "fieldBegin" else "field_end", "", cs, el=sub, meta=meta))
                elif payload in KNOWN_CTRL or payload in KNOWN_T_CHILD:
                    p.inlines.append(Inline("ctrl", "", cs, el=sub, meta={"name": payload, "start": off,
                                                                         "numType": sub.get("numType"), "num": sub.get("num")}))
                else:
                    p.inlines.append(Inline("opaque", "", cs, el=sub, meta={"name": payload, "start": off}))
                    self.ir.opaque.append({"para": p.nid, "tag": payload, "where": "ctrl"})
            else:  # obj
                if payload == "tbl":
                    p.inlines.append(Inline("obj", "", cs, obj=self.table(sub, p), el=sub, meta={"start": off}))
                elif payload in DRAW_TAGS or payload in FORM_TAGS:
                    p.inlines.append(Inline("obj", "", cs, obj=self.shape(sub, p), el=sub, meta={"start": off}))
                elif payload in ("compose", "dutmal"):
                    txt = sub.get("composeText") or "".join((c.text or "") for c in sub if local(c) == "mainText")
                    p.inlines.append(Inline("ctrl", "", cs, el=sub, meta={"name": payload, "display": txt, "start": off}))
                elif payload == "secPr":
                    continue
                else:
                    p.inlines.append(Inline("opaque", "", cs, el=sub, meta={"name": payload, "start": off}))
                    self.ir.opaque.append({"para": p.nid, "tag": payload, "where": "run"})
        if first_cs is None:
            run = el.find(HP + "run")
            first_cs = self.st.char(run.get("charPrIDRef") if run is not None else "0")
        p.default_cs = first_cs
        if head:
            p.inlines.insert(0, Inline("ctrl", "", first_cs, meta={"name": "heading", "display": head, "start": 0}))
        self.ir.nodes[p.nid] = p
        return p

    def section(self, index: int, root) -> Section:
        page = PageSetup()
        sp = root.find(".//" + HP + "secPr")
        if sp is not None:
            pp = sp.find(HP + "pagePr")
            if pp is not None:
                page.width, page.height = hu(pp.get("width"), 595.28), hu(pp.get("height"), 841.88)
                page.landscape = (pp.get("landscape") or "WIDELY") == "NARROWLY"
                if page.landscape and page.height > page.width:
                    page.width, page.height = page.height, page.width
                m = pp.find(HP + "margin")
                if m is not None:
                    page.left, page.right, page.top, page.bottom = hu(m.get("left")), hu(m.get("right")), hu(m.get("top")), hu(m.get("bottom"))
                    page.header, page.footer, page.gutter = hu(m.get("header")), hu(m.get("footer")), hu(m.get("gutter"))
        cp = root.find(".//" + HP + "colPr")
        if cp is not None:
            page.col_count = max(1, int(cp.get("colCount") or 1))
            page.col_gap = hu(cp.get("sameGap"))
        sec = Section(index, root, page)
        sec.paras = [self.para(p, sec) for p in root if p.tag == HP + "p"]
        # header / footer / notes / page number controls are owned by the section
        for ctrl in root.iter(HP + "ctrl"):
            for c in ctrl:
                name = local(c)
                if name in ("header", "footer"):
                    ps, _ = self.sublist_paras(c, sec)
                    (sec.headers if name == "header" else sec.footers).append((c.get("applyPageType") or "BOTH", ps))
                elif name in ("footNote", "endNote"):
                    ps, _ = self.sublist_paras(c, sec)
                    sec.notes.append((name, ps))
                elif name == "pageNum":
                    sec.page_num = {"pos": c.get("pos") or "BOTTOM_CENTER", "format": c.get("formatType") or "DIGIT", "side": c.get("sideChar") or ""}
        return sec


def build_ir(pkg: HwpxPackage) -> DocIR:
    stamp(pkg)
    b = _Builder(pkg)
    for i, root in enumerate(pkg.sections):
        b.ir.sections.append(b.section(i, root))
    b.ir.meta = {"n_sections": len(pkg.sections), "source_sha256": pkg.source_sha256}
    return b.ir


# ----------------------------------------------------------------------------- representation-invariant views
def _para_sem(p: Para) -> list:
    runs: list = []
    for i in p.inlines:
        if i.kind in ("text", "tab", "br"):
            key = i.cs.semantic_key() if i.cs else ()
            if runs and runs[-1][0] == "t" and runs[-1][1] == key:
                runs[-1][2] += i.text                       # run splitting is not semantic
            else:
                runs.append(["t", key, i.text])
        elif i.kind == "obj":
            runs.append(["o", _obj_sem(i.obj)])
        elif i.kind in ("field_begin", "field_end"):
            runs.append([i.kind, i.meta.get("type", "")])
        elif i.kind == "ctrl" and i.meta.get("name") in ("pageNum", "autoNum", "bookmark"):
            runs.append(["c", i.meta["name"]])
        elif i.kind == "opaque":
            runs.append(["x", i.meta.get("name")])
    return [p.ps.semantic_key(), p.page_break, [r for r in runs if not (r[0] == "t" and r[2] == "")]]


def _obj_sem(o) -> list:
    if isinstance(o, Table):
        return ["tbl", o.n_rows, o.n_cols, o.pos.treat_as_char, o.pos.wrap,
                [[c.row, c.col, c.rowspan, c.colspan, round(c.width, 1), round(c.height, 1), c.bf.semantic_key(), c.valign,
                  [_para_sem(p) for p in c.paras]] for c in sorted(o.cells, key=lambda c: (c.row, c.col))]]
    return [o.kind, round(o.width, 1), round(o.height, 1), o.pos.treat_as_char, o.pos.wrap, o.pos.vert_rel, o.pos.horz_rel,
            round(o.pos.vert_off, 1), round(o.pos.horz_off, 1), (o.line.type, round(o.line.width, 2), o.line.color.upper()),
            o.fill, bool(o.bin_ref), o.size_protect, o.props.get("value"), o.props.get("script"),
            [_para_sem(p) for p in (o.paras or [])], [_obj_sem(c) for c in o.children]]


def semantic_tree(ir: DocIR) -> list:
    """Canonical semantic content: independent of style-id numbering, run splitting, attribute order, node ids."""
    out = []
    for s in ir.sections:
        pg = s.page
        out.append([[round(pg.width, 1), round(pg.height, 1), round(pg.left, 1), round(pg.right, 1), round(pg.top, 1), round(pg.bottom, 1), pg.col_count],
                    [_para_sem(p) for p in s.paras],
                    [[k, [_para_sem(p) for p in ps]] for k, ps in s.headers + s.footers + s.notes], s.page_num])
    return out


def semantic_fingerprint(ir: DocIR) -> str:
    return jhash(semantic_tree(ir))


def structure_fingerprint(ir: DocIR) -> str:
    """Layout-skeleton fingerprint (no text): used for template-family dedupe."""
    sk = []
    for t in ir.tables():
        sk.append(["tbl", t.n_rows, t.n_cols, sorted((c.row, c.col, c.rowspan, c.colspan) for c in t.cells)])
    for s in ir.shapes():
        sk.append([s.kind, round(s.width), round(s.height)])
    sk.append(["paras", sum(1 for _ in ir.paras(deep=False))])
    return jhash(sk)


def native_object_stats(ir: DocIR, layout=None) -> dict:
    """Explanatory variables only (never an editability score)."""
    c: Counter = Counter()
    text_chars = 0
    for p in ir.paras():
        c["paragraphs"] += 1
        t = p.text
        text_chars += len(t)
        c["empty_paragraphs"] += int(not t.strip())
        c["runs"] += sum(1 for i in p.inlines if i.kind == "text")
        c["fields"] += sum(1 for i in p.inlines if i.kind == "field_begin")
        c["opaque_inlines"] += sum(1 for i in p.inlines if i.kind == "opaque")
    for t in ir.tables():
        c["tables"] += 1
        c["cells"] += len(t.cells)
        c["merged_cells"] += len(t.merged_cells())
        c["nested_tables"] += int(isinstance(getattr(t.parent, "parent", None), Cell))
        c["tables_inline"] += int(t.pos.treat_as_char)
    img_area = 0.0
    for s in ir.shapes():
        c["shape_" + s.kind] += 1
        c["textboxes"] += int(s.is_textbox)
        c["floating_objects"] += int(not s.pos.treat_as_char)
        c["absolute_objects"] += int(not s.pos.treat_as_char and s.pos.vert_rel in ("PAPER", "PAGE"))
        if s.kind == "pic":
            img_area += s.width * s.height
    page_area = sum(s.page.width * s.page.height for s in ir.sections) or 1.0
    n_pages = layout.n_pages if layout is not None else None
    out = dict(sorted(c.items()))
    out.update({"text_chars": text_chars, "image_area_ratio_per_section_page": round(img_area / page_area, 4),
                "sections": len(ir.sections), "pages": n_pages, "opaque_nodes": len(ir.opaque)})
    return out
