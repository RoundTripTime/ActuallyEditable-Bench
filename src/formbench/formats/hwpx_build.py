"""OWPML element constructors and a document builder (used by the editor backend and by reconstruction baselines).

Element shapes mirror files written by Hancom Office / hwpxlib (see tests/fixtures)."""
from __future__ import annotations

import copy
import io
import zipfile
from pathlib import Path

from lxml import etree

from .hwpx import HC, HH, HP, NS, HwpxPackage

SKELETON_DIR = Path(__file__).parent / "skeleton"
_NSDECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
LANGS = ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user")


def H(v: float) -> str:
    """points -> HWPUNIT string."""
    return str(int(round(v * 100)))


def X(xml: str):
    """Parse an OWPML fragment using the standard prefixes."""
    root = etree.fromstring(f"<w {_NSDECL}>{xml}</w>")
    el = root[0]
    root.remove(el)
    el.tail = None
    return el


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def skeleton_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        order = ["mimetype", "version.xml", "Contents/header.xml", "Contents/section0.xml", "settings.xml",
                 "META-INF/container.xml", "Contents/content.hpf", "META-INF/manifest.xml"]
        for name in order:
            zi = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            z.writestr(zi, (SKELETON_DIR / name).read_bytes())
    return buf.getvalue()


class HeaderEditor:
    """Find-or-create shared property entries in header.xml (charPr / paraPr / borderFill)."""

    def __init__(self, head):
        self.head = head
        self._lists: dict = {}
        self._sigs: dict = {}            # list name -> {signature: id}; built once per editor, kept up to date by _add

    def _list(self, name: str):
        if name not in self._lists:
            self._lists[name] = self.head.find(".//" + HH + name)
        return self._lists[name]

    def _add(self, list_name: str, el) -> str:
        lst = self._list(list_name)
        ids = [int(e.get("id")) for e in lst if e.get("id") and e.get("id").isdigit()]
        new_id = str(max(ids) + 1 if ids else 0)
        el.set("id", new_id)
        lst.append(el)
        lst.set("itemCnt", str(len([e for e in lst if isinstance(e.tag, str)])))
        if list_name in self._sigs:
            self._sigs[list_name].setdefault(self._sig(el), new_id)
        return new_id

    @staticmethod
    def _sig(el) -> bytes:
        c = copy.deepcopy(el)
        c.attrib.pop("id", None)
        return etree.tostring(c, method="c14n")

    def _find_or_add(self, list_name: str, el) -> str:
        sig = self._sig(el)
        if list_name not in self._sigs:                 # first entry in document order wins, as before
            table: dict = {}
            for e in self._list(list_name):
                if isinstance(e.tag, str):
                    table.setdefault(self._sig(e), e.get("id"))
            self._sigs[list_name] = table
        hit = self._sigs[list_name].get(sig)
        if hit is not None:
            return hit
        return self._add(list_name, el)

    def font_id(self, face: str | None = None, serif: bool = False) -> str:
        ff = self.head.find(".//" + HH + "fontface[@lang='HANGUL']")
        fonts = list(ff.iter(HH + "font")) if ff is not None else []
        if face:
            for f in fonts:
                if f.get("face") == face:
                    return f.get("id")
        for f in fonts:
            name = f.get("face", "")
            if serif == any(k in name for k in ("바탕", "명조")):
                return f.get("id")
        return fonts[0].get("id") if fonts else "0"

    def char_pr(self, base: str | None = None, size: float | None = None, bold: bool | None = None, italic: bool | None = None,
                underline: bool | None = None, color: str | None = None, serif: bool | None = None) -> str:
        lst = self._list("charProperties")
        src = next((e for e in lst if e.get("id") == (base or "0")), None)
        if src is None:
            src = next(e for e in lst if isinstance(e.tag, str))
        el = copy.deepcopy(src)
        if size is not None:
            el.set("height", H(size))
        if color is not None:
            el.set("textColor", color)
        if serif is not None:
            fid = self.font_id(serif=serif)
            fr = el.find(HH + "fontRef")
            for lang in LANGS:
                fr.set(lang, fid)
        for tag, val in (("italic", italic), ("bold", bold)):
            if val is None:
                continue
            cur = el.find(HH + tag)
            if val and cur is None:
                anchor = el.find(HH + "offset")
                new = etree.Element(HH + tag)
                if tag == "bold" and el.find(HH + "italic") is not None:
                    anchor = el.find(HH + "italic")
                anchor.addnext(new) if anchor is not None else el.append(new)
            elif not val and cur is not None:
                el.remove(cur)
        if underline is not None:
            u = el.find(HH + "underline")
            if u is not None:
                u.set("type", "BOTTOM" if underline else "NONE")
        return self._find_or_add("charProperties", el)

    def para_pr(self, base: str | None = None, align: str | None = None, line_spacing: float | None = None, left: float | None = None,
                right: float | None = None, indent: float | None = None, prev: float | None = None, next_: float | None = None) -> str:
        lst = self._list("paraProperties")
        src = next((e for e in lst if e.get("id") == (base or "0")), None)
        if src is None:
            src = next(e for e in lst if isinstance(e.tag, str))
        el = copy.deepcopy(src)
        if align is not None:
            el.find(HH + "align").set("horizontal", align)
        for m in el.iter(HH + "margin"):
            for name, v in (("intent", indent), ("left", left), ("right", right), ("prev", prev), ("next", next_)):
                if v is not None and m.find(HC + name) is not None:
                    m.find(HC + name).set("value", H(v))
                    m.find(HC + name).set("unit", "HWPUNIT")
        if line_spacing is not None:
            for ls in el.iter(HH + "lineSpacing"):
                ls.set("type", "PERCENT")
                ls.set("value", str(int(line_spacing)))
        return self._find_or_add("paraProperties", el)

    def border_fill(self, left: bool = True, right: bool = True, top: bool = True, bottom: bool = True, width_mm: float = 0.12,
                    color: str = "#000000", fill: str | None = None) -> str:
        def b(name, on):
            return f'<hh:{name}Border type="{"SOLID" if on else "NONE"}" width="{width_mm} mm" color="{color}"/>'
        fb = f'<hc:fillBrush><hc:winBrush faceColor="{fill}" hatchColor="#000000" alpha="0"/></hc:fillBrush>' if fill else ""
        el = X('<hh:borderFill id="0" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
               '<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
               f'{b("left", left)}{b("right", right)}{b("top", top)}{b("bottom", bottom)}'
               f'<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>{fb}</hh:borderFill>')
        return self._find_or_add("borderFills", el)


# ----------------------------------------------------------------------------- element constructors
def make_text_children(t_el, text: str) -> None:
    """Fill an hp:t with text; '\\n' -> hp:lineBreak, '\\t' -> hp:tab."""
    t_el.text = None
    last = None
    buf = ""
    for ch in text:
        if ch in "\n\t":
            if last is None:
                t_el.text = (t_el.text or "") + buf
            else:
                last.tail = (last.tail or "") + buf
            buf = ""
            last = etree.SubElement(t_el, HP + ("lineBreak" if ch == "\n" else "tab"))
        else:
            buf += ch
    if last is None:
        t_el.text = (t_el.text or "") + buf if (buf or t_el.text) else None
    else:
        last.tail = buf or None


def make_p(text: str = "", char_id: str = "0", para_id: str = "0", style_id: str = "0", page_break: bool = False):
    p = X(f'<hp:p id="0" paraPrIDRef="{para_id}" styleIDRef="{style_id}" pageBreak="{int(page_break)}" columnBreak="0" merged="0">'
          f'<hp:run charPrIDRef="{char_id}"><hp:t/></hp:run></hp:p>')
    if text:
        make_text_children(p[0][0], text)
    return p


_POS = ('<hp:pos treatAsChar="{tac}" affectLSpacing="0" flowWithText="{fwt}" allowOverlap="{ovl}" holdAnchorAndSO="0" vertRelTo="{vr}" '
        'horzRelTo="{hr}" vertAlign="TOP" horzAlign="LEFT" vertOffset="{vo}" horzOffset="{ho}"/>')
_RENDER = ('<hp:flip horizontal="0" vertical="0"/><hp:rotationInfo angle="0" centerX="{cx}" centerY="{cy}" rotateimage="1"/>'
           '<hp:renderingInfo><hc:transMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/><hc:scaMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
           '<hc:rotMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/></hp:renderingInfo>')


def _pos(inline: bool, x: float, y: float, rel: str = "PAPER") -> str:
    if inline:
        return _POS.format(tac=1, fwt=1, ovl=0, vr="PARA", hr="COLUMN", vo=0, ho=0)
    return _POS.format(tac=0, fwt=0, ovl=1, vr=rel, hr=rel if rel != "PARA" else "COLUMN", vo=H(y), ho=H(x))


def make_textbox(obj_id: int, x: float, y: float, w: float, h: float, paras: list | None = None, border: bool = True, fill: str | None = None,
                 inline: bool = False, wrap: str = "IN_FRONT_OF_TEXT", valign: str = "TOP", margin: float = 2.83, z: int = 0,
                 rel: str = "PAPER", protect: bool = False, char_id: str = "0", para_id: str = "0"):
    fb = f'<hc:fillBrush><hc:winBrush faceColor="{fill}" hatchColor="#000000" alpha="0"/></hc:fillBrush>' if fill else ""
    el = X(f'<hp:rect id="{obj_id}" zOrder="{z}" numberingType="PICTURE" textWrap="{"TOP_AND_BOTTOM" if inline else wrap}" textFlow="BOTH_SIDES" lock="0" '
           f'dropcapstyle="None" href="" groupLevel="0" instid="{obj_id + 1000000}" ratio="0">'
           f'<hp:offset x="0" y="0"/><hp:orgSz width="{H(w)}" height="{H(h)}"/><hp:curSz width="0" height="0"/>'
           + _RENDER.format(cx=H(w / 2), cy=H(h / 2)) +
           f'<hp:lineShape color="#000000" width="33" style="{"SOLID" if border else "NONE"}" endCap="FLAT" headStyle="NORMAL" tailStyle="NORMAL" '
           f'headfill="1" tailfill="1" headSz="MEDIUM_MEDIUM" tailSz="MEDIUM_MEDIUM" outlineStyle="NORMAL" alpha="0"/>{fb}'
           '<hp:shadow type="NONE" color="#B2B2B2" offsetX="0" offsetY="0" alpha="0"/>'
           f'<hp:drawText lastWidth="{H(w)}" name="" editable="0"><hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="{valign}" '
           'linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0"></hp:subList>'
           f'<hp:textMargin left="{H(margin)}" right="{H(margin)}" top="{H(margin)}" bottom="{H(margin)}"/></hp:drawText>'
           f'<hc:pt0 x="0" y="0"/><hc:pt1 x="{H(w)}" y="0"/><hc:pt2 x="{H(w)}" y="{H(h)}"/><hc:pt3 x="0" y="{H(h)}"/>'
           f'<hp:sz width="{H(w)}" widthRelTo="ABSOLUTE" height="{H(h)}" heightRelTo="ABSOLUTE" protect="{int(protect)}"/>'
           + _pos(inline, x, y, rel) + '<hp:outMargin left="0" right="0" top="0" bottom="0"/></hp:rect>')
    sl = el.find(HP + "drawText").find(HP + "subList")
    for p in (paras or [make_p("", char_id, para_id)]):
        sl.append(p)
    return el


def make_line(obj_id: int, x0: float, y0: float, x1: float, y1: float, width: float = 0.34, color: str = "#000000", z: int = 0, rel: str = "PAPER"):
    x, y = min(x0, x1), min(y0, y1)
    w, h = abs(x1 - x0), abs(y1 - y0)
    el = X(f'<hp:line id="{obj_id}" zOrder="{z}" numberingType="PICTURE" textWrap="IN_FRONT_OF_TEXT" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" '
           f'href="" groupLevel="0" instid="{obj_id + 1000000}" isReverseHV="0"><hp:offset x="0" y="0"/><hp:orgSz width="{H(w)}" height="{H(h)}"/>'
           '<hp:curSz width="0" height="0"/>' + _RENDER.format(cx=H(w / 2), cy=H(h / 2)) +
           f'<hp:lineShape color="{color}" width="{H(width)}" style="SOLID" endCap="FLAT" headStyle="NORMAL" tailStyle="NORMAL" headfill="1" tailfill="1" '
           'headSz="MEDIUM_MEDIUM" tailSz="MEDIUM_MEDIUM" outlineStyle="NORMAL" alpha="0"/>'
           '<hp:shadow type="NONE" color="#B2B2B2" offsetX="0" offsetY="0" alpha="0"/>'
           f'<hc:startPt x="{H(x0 - x)}" y="{H(y0 - y)}"/><hc:endPt x="{H(x1 - x)}" y="{H(y1 - y)}"/>'
           f'<hp:sz width="{H(w)}" widthRelTo="ABSOLUTE" height="{H(h)}" heightRelTo="ABSOLUTE" protect="0"/>'
           + _pos(False, x, y, rel) + '<hp:outMargin left="0" right="0" top="0" bottom="0"/></hp:line>')
    return el


def make_pic(obj_id: int, bin_id: str, x: float, y: float, w: float, h: float, px: tuple[int, int] = (0, 0), wrap: str = "BEHIND_TEXT",
             inline: bool = False, z: int = 0, rel: str = "PAPER"):
    dw, dh = (px[0] * 75, px[1] * 75) if px[0] else (int(w * 100), int(h * 100))
    el = X(f'<hp:pic id="{obj_id}" zOrder="{z}" numberingType="PICTURE" textWrap="{"TOP_AND_BOTTOM" if inline else wrap}" textFlow="BOTH_SIDES" lock="0" '
           f'dropcapstyle="None" href="" groupLevel="0" instid="{obj_id + 1000000}" reverse="0"><hp:offset x="0" y="0"/>'
           f'<hp:orgSz width="{H(w)}" height="{H(h)}"/><hp:curSz width="{H(w)}" height="{H(h)}"/>' + _RENDER.format(cx=H(w / 2), cy=H(h / 2)) +
           f'<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="{H(w)}" y="0"/><hc:pt2 x="{H(w)}" y="{H(h)}"/><hc:pt3 x="0" y="{H(h)}"/></hp:imgRect>'
           f'<hp:imgClip left="0" right="{dw}" top="0" bottom="{dh}"/><hp:inMargin left="0" right="0" top="0" bottom="0"/>'
           f'<hp:imgDim dimwidth="{dw}" dimheight="{dh}"/><hc:img binaryItemIDRef="{bin_id}" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/>'
           f'<hp:sz width="{H(w)}" widthRelTo="ABSOLUTE" height="{H(h)}" heightRelTo="ABSOLUTE" protect="0"/>'
           + _pos(inline, x, y, rel) + '<hp:outMargin left="0" right="0" top="0" bottom="0"/></hp:pic>')
    return el


def make_cell(row: int, col: int, w: float, h: float, bf_id: str, paras: list | None = None, rowspan: int = 1, colspan: int = 1,
              valign: str = "CENTER", char_id: str = "0", para_id: str = "0", header: bool = False):
    tc = X(f'<hp:tc name="" header="{int(header)}" hasMargin="0" protect="0" editable="0" dirty="0" borderFillIDRef="{bf_id}">'
           f'<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="{valign}" linkListIDRef="0" linkListNextIDRef="0" '
           'textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0"></hp:subList>'
           f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="{colspan}" rowSpan="{rowspan}"/>'
           f'<hp:cellSz width="{H(w)}" height="{H(h)}"/><hp:cellMargin left="510" right="510" top="141" bottom="141"/></hp:tc>')
    sl = tc.find(HP + "subList")
    for p in (paras or [make_p("", char_id, para_id)]):
        sl.append(p)
    return tc


def make_table(obj_id: int, col_widths: list[float], row_heights: list[float], cells: list[dict], bf_id: str, inline: bool = True,
               x: float = 0.0, y: float = 0.0, rel: str = "PAPER", z: int = 0, wrap: str = "TOP_AND_BOTTOM", char_id: str = "0", para_id: str = "0"):
    """cells: dicts with row, col and optional rowspan, colspan, paras, bf, valign, header. Missing grid cells are created empty."""
    n_r, n_c = len(row_heights), len(col_widths)
    covered = set()
    by_pos = {}
    for c in cells:
        by_pos[(c["row"], c["col"])] = c
        for r in range(c["row"], c["row"] + c.get("rowspan", 1)):
            for k in range(c["col"], c["col"] + c.get("colspan", 1)):
                covered.add((r, k))
    tbl = X(f'<hp:tbl id="{obj_id}" zOrder="{z}" numberingType="TABLE" textWrap="{wrap}" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" '
            f'pageBreak="CELL" repeatHeader="0" rowCnt="{n_r}" colCnt="{n_c}" cellSpacing="0" borderFillIDRef="{bf_id}" noAdjust="0">'
            f'<hp:sz width="{H(sum(col_widths))}" widthRelTo="ABSOLUTE" height="{H(sum(row_heights))}" heightRelTo="ABSOLUTE" protect="0"/>'
            + _pos(inline, x, y, rel) + '<hp:outMargin left="0" right="0" top="0" bottom="0"/><hp:inMargin left="510" right="510" top="141" bottom="141"/></hp:tbl>')
    for r in range(n_r):
        tr = etree.SubElement(tbl, HP + "tr")
        for k in range(n_c):
            c = by_pos.get((r, k))
            if c is None:
                if (r, k) in covered:
                    continue
                c = {"row": r, "col": k}
            rs, cs_ = c.get("rowspan", 1), c.get("colspan", 1)
            tr.append(make_cell(r, k, sum(col_widths[k:k + cs_]), sum(row_heights[r:r + rs]), c.get("bf", bf_id), c.get("paras"), rs, cs_,
                                c.get("valign", "CENTER"), char_id, para_id, c.get("header", False)))
    return tbl


class HwpxBuilder:
    """Create a new HWPX document from the blank skeleton."""

    def __init__(self, page_w: float = 595.28, page_h: float = 841.88, margins: tuple = (56.7, 56.7, 42.5, 42.5, 28.3, 28.3)):
        self.pkg = HwpxPackage.load(skeleton_bytes())
        self.head = HeaderEditor(self.pkg.header)
        self.sec = self.pkg.sections[0]
        self.first = self.sec.find(HP + "p")
        pp = self.sec.find(".//" + HP + "pagePr")
        pp.set("width", H(min(page_w, page_h) if page_w > page_h else page_w))
        pp.set("height", H(max(page_w, page_h) if page_w > page_h else page_h))
        pp.set("landscape", "NARROWLY" if page_w > page_h else "WIDELY")
        m = pp.find(HP + "margin")
        for name, v in zip(("left", "right", "top", "bottom", "header", "footer"), margins):
            m.set(name, H(v))
        m.set("gutter", "0")
        la = self.first.find(HP + "linesegarray")
        if la is not None:
            self.first.remove(la)
        self._id = 1000
        self._first_used = False
        self.pkg.mark_dirty(self.pkg.header_name)
        self.pkg.mark_dirty()

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def add_para(self, text: str = "", char_id: str = "0", para_id: str = "0", page_break: bool = False):
        if not self._first_used:
            # the first paragraph owns secPr; put text into an extra run after it
            self._first_used = True
            p = self.first
            p.set("paraPrIDRef", para_id)
            run = etree.SubElement(p, HP + "run")
            run.set("charPrIDRef", char_id)
            t = etree.SubElement(run, HP + "t")
            if text:
                make_text_children(t, text)
            return p
        p = make_p(text, char_id, para_id, page_break=page_break)
        self.sec.append(p)
        return p

    def attach(self, obj_el, p=None, char_id: str = "0"):
        """Anchor an object in paragraph ``p`` (a new empty paragraph when omitted)."""
        if p is None:
            p = self.add_para("", char_id)
        run = [r for r in p if r.tag == HP + "run"][-1]
        t = run.find(HP + "t")
        if t is not None and not (t.text or len(t)):
            t.addprevious(obj_el)
        else:
            run.append(obj_el)
        return p

    def add_bin(self, data: bytes, ext: str = "png") -> str:
        return self.pkg.add_bin(data, ext)

    def package(self) -> HwpxPackage:
        self.pkg.mark_dirty(self.pkg.header_name)
        self.pkg.mark_dirty()
        return self.pkg

    def to_bytes(self) -> bytes:
        return self.package().to_bytes()
