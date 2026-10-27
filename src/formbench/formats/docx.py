"""DOCX adapter: metadata/text extraction and DOCX -> HWPX conversion (used for corpus normalisation and as the
second leg of the intermediary-conversion baseline B3). Conversion losses are reported, never hidden."""
from __future__ import annotations

import io
import zipfile

from lxml import etree

from .hwpx import HP
from .hwpx_build import HwpxBuilder, make_p, make_pic, make_table, make_textbox

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EMU_PT = 12700.0
ALIGN = {"center": "CENTER", "right": "RIGHT", "both": "JUSTIFY", "distribute": "DISTRIBUTE", "left": "LEFT", "start": "LEFT", "end": "RIGHT"}


def own_tokens(p) -> list:
    """(element, length, text) of a paragraph's OWN text in order: w:t, w:tab ('\\t'), w:br / w:cr ('\\n') whose
    nearest w:p ancestor is p (text of text boxes anchored in p belongs to the text box's paragraphs); tab-stop
    definitions (w:tabs/w:tab) are not text. Shared by the converter (view) and the native DOCX editor."""
    out = []
    for e in p.iter(W + "t", W + "tab", W + "br", W + "cr"):
        if next(e.iterancestors(W + "p"), None) is not p:
            continue
        if e.tag == W + "t":
            out.append((e, len(e.text or ""), e.text or ""))
        elif e.tag == W + "tab":
            if e.getparent() is not None and e.getparent().tag == W + "tabs":
                continue
            out.append((e, 1, "\t"))
        else:
            out.append((e, 1, "\n"))
    return out


def _copy_id(src, dst) -> None:
    """Carry a native element's session id into the HWPX view (formbench native DOCX editing; no-op otherwise)."""
    from ..document_ir.normalize import NID
    v = src.get(NID) if src is not None else None
    if v is not None and dst is not None:
        dst.set(NID, v)


def _secure(data: bytes):
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))


def docx_info(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    root = _secure(z.read("word/document.xml"))
    paras = list(root.iter(W + "p"))
    text = "\n".join("".join(t.text or "" for t in p.iter(W + "t")) for p in paras)
    pages = None
    if "docProps/app.xml" in z.namelist():
        app = _secure(z.read("docProps/app.xml"))
        for el in app:
            if el.tag.endswith("}Pages") and (el.text or "").isdigit():
                pages = int(el.text)
    return {"text": text, "paragraphs": len(paras), "tables": len(list(root.iter(W + "tbl"))), "images": len(list(root.iter(A + "blip"))),
            "textboxes": len(list(root.iter(W + "txbxContent"))), "page_count": pages}


def _twips(el, attr, default=0.0) -> float:
    v = el.get(W + attr) if el is not None else None
    try:
        return float(v) / 20.0
    except (TypeError, ValueError):
        return default


class _Conv:
    def __init__(self, data: bytes, root=None):
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.root = root if root is not None else _secure(self.z.read("word/document.xml"))
        self.rels = {}
        if "word/_rels/document.xml.rels" in self.z.namelist():
            for r in _secure(self.z.read("word/_rels/document.xml.rels")):
                self.rels[r.get("Id")] = r.get("Target")
        body = self.root.find(W + "body")
        sect = body.find(W + "sectPr") if body is not None else None
        pg, mar = (sect.find(W + "pgSz"), sect.find(W + "pgMar")) if sect is not None else (None, None)
        pw, ph = _twips(pg, "w", 595.3), _twips(pg, "h", 841.9)
        ml, mr, mt, mb = _twips(mar, "left", 56.7), _twips(mar, "right", 56.7), _twips(mar, "top", 56.7), _twips(mar, "bottom", 56.7)
        self.b = HwpxBuilder(pw, ph, (ml, mr, mt, mb, 0.0, 0.0))
        self.body_w = pw - ml - mr
        self.origin = (ml, mt)
        self.loss: dict[str, int] = {}
        self.body = body
        self.default_face = ""                         # docDefaults run fonts (theme fonts -> sans unless named serif)
        if "word/styles.xml" in self.z.namelist():
            st = _secure(self.z.read("word/styles.xml"))
            rf = st.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr/{W}rFonts")
            if rf is not None:
                self.default_face = rf.get(W + "eastAsia") or rf.get(W + "ascii") or rf.get(W + "hAnsi") or ""

    def note(self, k: str) -> None:
        self.loss[k] = self.loss.get(k, 0) + 1

    def run_props(self, p) -> dict:
        size, bold, face = None, False, None
        for r in p.iter(W + "r"):
            rp = r.find(W + "rPr")
            if rp is not None:
                rf = rp.find(W + "rFonts")
                if rf is not None and face is None:
                    face = rf.get(W + "eastAsia") or rf.get(W + "ascii") or rf.get(W + "hAnsi")
                sz = rp.find(W + "sz")
                if sz is not None and size is None:
                    try:
                        size = float(sz.get(W + "val")) / 2
                    except (TypeError, ValueError):
                        pass
                b = rp.find(W + "b")
                if b is not None and b.get(W + "val") not in ("0", "false"):
                    bold = True
            if "".join(t.text or "" for t in r.iter(W + "t")).strip():
                break
        from .odt_view import is_serif
        return {"size": size or 10.0, "bold": bold, "serif": is_serif(face or self.default_face)}

    def make_para(self, p, in_cell: bool = False):
        ppr = p.find(W + "pPr")
        jc = ppr.find(W + "jc") if ppr is not None else None
        ind = ppr.find(W + "ind") if ppr is not None else None
        sp = ppr.find(W + "spacing") if ppr is not None else None
        rp = self.run_props(p)
        line = None
        if sp is not None and sp.get(W + "line") and (sp.get(W + "lineRule") in (None, "auto")):
            try:
                line = float(sp.get(W + "line")) / 240.0 * 100.0
            except ValueError:
                line = None
        para_id = self.b.head.para_pr(align=ALIGN.get(jc.get(W + "val") if jc is not None else "left", "LEFT"),
                                      line_spacing=max(60.0, min(line or 120.0, 300.0)),
                                      left=_twips(ind, "left") or _twips(ind, "start"), indent=_twips(ind, "firstLine") - _twips(ind, "hanging"),
                                      prev=_twips(sp, "before"), next_=_twips(sp, "after"))
        char_id = self.b.head.char_pr(size=rp["size"], bold=rp["bold"], serif=rp["serif"])
        # text that lives in text boxes anchored in this paragraph is emitted as text boxes, not as body text
        own_text = "".join(t for _, _, t in own_tokens(p))
        hp_p = make_p(own_text, char_id, para_id)
        _copy_id(p, hp_p)
        return hp_p, char_id, para_id

    def objects(self, p, hp_p) -> None:
        for dr in p.iter(W + "drawing"):
            if any(a.tag == W + "txbxContent" for a in dr.iterancestors()):
                continue
            anchor = dr.find(WP + "anchor")
            holder = anchor if anchor is not None else dr.find(WP + "inline")
            if holder is None:
                continue
            ext = holder.find(WP + "extent")
            w = float(ext.get("cx", 0)) / EMU_PT if ext is not None else 50.0
            h = float(ext.get("cy", 0)) / EMU_PT if ext is not None else 50.0
            x = y = 0.0
            if anchor is not None:
                for tag, axis in ((WP + "positionH", "x"), (WP + "positionV", "y")):
                    pos = anchor.find(tag)
                    off = pos.find(WP + "posOffset") if pos is not None else None
                    val = float(off.text) / EMU_PT if off is not None and off.text and off.text.lstrip("-").isdigit() else 0.0
                    rel = pos.get("relativeFrom") if pos is not None else "page"
                    base = 0.0 if rel == "page" else (self.origin[0] if axis == "x" else self.origin[1])
                    if axis == "x":
                        x = base + val
                    else:
                        y = base + val
            blip = next(iter(holder.iter(A + "blip")), None)
            txbx = next(iter(holder.iter(W + "txbxContent")), None)
            if txbx is not None:
                inner = [self.make_para(q)[0] for q in txbx.iter(W + "p")] or None
                tb = make_textbox(self.b.next_id(), x, y, max(w, 4), max(h, 4), inner, border=False, inline=anchor is None, margin=0.5)
                _copy_id(dr, tb)
                self.b.attach(tb, hp_p)
            elif blip is not None:
                target = self.rels.get(blip.get(R + "embed"))
                name = "word/" + target if target and not target.startswith("/") else (target or "").lstrip("/")
                if name in self.z.namelist():
                    data = self.z.read(name)
                    ext_name = name.rsplit(".", 1)[-1].lower()
                    if ext_name in ("png", "jpg", "jpeg", "bmp"):
                        bid = self.b.add_bin(data, ext_name)
                        pic = make_pic(self.b.next_id(), bid, x, y, max(w, 2), max(h, 2), inline=anchor is None, wrap="BEHIND_TEXT")
                        _copy_id(dr, pic)
                        self.b.attach(pic, hp_p)
                    else:
                        self.note(f"image_format_unsupported:{ext_name}")
                else:
                    self.note("image_part_missing")
            else:
                self.note("drawing_shape_dropped")
        for _ in p.iter(W + "pict"):
            self.note("vml_object_dropped")

    def table(self, tbl):
        grid = [_twips(g, "w", 60.0) for g in tbl.iter(W + "gridCol") if g.getparent().getparent() is tbl]
        rows = [tr for tr in tbl if tr.tag == W + "tr"]
        if not grid:
            grid = [self.body_w / max(1, max(len([c for c in tr if c.tag == W + "tc"]) for tr in rows))] * max(1, max(len([c for c in tr if c.tag == W + "tc"]) for tr in rows))
        cells, heights, open_merge = [], [], {}
        for ri, tr in enumerate(rows):
            trp = tr.find(W + "trPr")
            th = trp.find(W + "trHeight") if trp is not None else None
            heights.append(max(_twips(th, "val", 18.0), 8.0))
            col = 0
            for tc in tr:
                if tc.tag != W + "tc":
                    continue
                tcp = tc.find(W + "tcPr")
                span = 1
                vm = None
                bord = {}
                if tcp is not None:
                    gs = tcp.find(W + "gridSpan")
                    if gs is not None and (gs.get(W + "val") or "1").isdigit():
                        span = int(gs.get(W + "val"))
                    v = tcp.find(W + "vMerge")
                    if v is not None:
                        vm = v.get(W + "val") or "continue"
                    tb = tcp.find(W + "tcBorders")
                    if tb is not None:
                        for side in ("left", "right", "top", "bottom"):
                            e = tb.find(W + side)
                            if e is None and side in ("left", "right"):
                                e = tb.find(W + ("start" if side == "left" else "end"))
                            if e is not None:
                                bord[side] = e.get(W + "val") not in ("nil", "none")
                if vm == "continue" and col in open_merge:
                    open_merge[col]["rowspan"] += 1
                else:
                    paras = [self.make_para(q, True)[0] for q in tc if q.tag == W + "p"]
                    if any(ch.tag == W + "tbl" for ch in tc):
                        self.note("nested_table_flattened")
                        for nt in (ch for ch in tc if ch.tag == W + "tbl"):
                            paras += [self.make_para(q, True)[0] for q in nt.iter(W + "p")]
                    c = {"row": ri, "col": col, "colspan": span, "rowspan": 1, "paras": paras or None, "valign": "CENTER", "src": tc,
                         "bf": self.b.head.border_fill(bord.get("left", True), bord.get("right", True), bord.get("top", True), bord.get("bottom", True))}
                    cells.append(c)
                    if vm == "restart":
                        open_merge[col] = c
                    else:
                        open_merge.pop(col, None)
                col += span
        n_cols = len(grid)
        cells = [c for c in cells if c["col"] < n_cols]
        for c in cells:
            c["colspan"] = min(c["colspan"], n_cols - c["col"])
        out = make_table(self.b.next_id(), grid, heights, cells, self.b.head.border_fill(), inline=True)
        _copy_id(tbl, out)
        by_addr = {(c["row"], c["col"]): c["src"] for c in cells}
        for tc_el in out.iter(HP + "tc"):
            a = tc_el.find(HP + "cellAddr")
            src = by_addr.get((int(a.get("rowAddr")), int(a.get("colAddr")))) if a is not None else None
            _copy_id(src, tc_el)
        return out

    def run(self) -> tuple[bytes, dict]:
        pkg, info = self.build()
        return pkg.to_bytes(), info

    def build(self):
        """Convert into an in-memory HwpxPackage (session id attributes kept; to_bytes would strip them)."""
        if self.body is not None:
            for el in self.body:
                if el.tag == W + "p":
                    hp_p, char_id, para_id = self.make_para(el)
                    text = hp_p[0][0]
                    target = self.b.add_para("", char_id, para_id)
                    _copy_id(el, target)
                    run = [r for r in target if r.tag == HP + "run"][-1]
                    t = run.find(HP + "t")
                    t.text = text.text
                    for ch in list(text):
                        t.append(ch)
                    self.objects(el, target)
                elif el.tag == W + "tbl":
                    self.b.attach(self.table(el), self.b.add_para("", "0", self.b.head.para_pr(align="LEFT", line_spacing=100)))
                elif el.tag != W + "sectPr":
                    self.note("body_element_dropped:" + el.tag.rsplit("}", 1)[-1])
        return self.b.package(), {"conversion_losses": self.loss}


def docx_to_hwpx(data: bytes) -> tuple[bytes, dict]:
    return _Conv(data).run()

