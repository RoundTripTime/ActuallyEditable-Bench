"""Docling result (DoclingDocument JSON) -> ODT, written directly without LibreOffice (user request 2026-10-18: the
LibreOffice HTML import changed the results; e.g. it turned landscape pages into A4 portrait and dropped the table borders
that Docling's own HTML serializer asks for).

Rule: carry over what Docling recognised and nothing else; every other property stays at the ODF application default.
  structure   body items in Docling's reading order: section headers/titles -> text:h (bold), text/list items/captions/
              footnotes -> text:p (list markers kept as text), inline groups -> one paragraph, tables -> table:table with
              Docling's cell grid, row/column spans (table:covered-table-cell), pictures -> embedded image at Docling's size
  geometry    page size and orientation of Docling's first page; page margins from the extent of the recognised content
              (clamped to 0.5-2.5 cm); a page break where Docling's page number changes; table width, left offset and
              column widths from Docling's table and cell boxes
  formatting  only what Docling's HTML serializer itself specifies for tables: cell borders, bold header cells
              (column_header). No font family or size, spacing or alignment is invented (Docling does not recognise them).
Furniture (page headers/footers) is left out, as in Docling's HTML export. Pure function of the JSON (deterministic).
"""
from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape

MIME = "application/vnd.oasis.opendocument.text"
NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"')
VERSION = "1.4"
CM = 72 / 2.54
TEXT_BLOCKS = {"text", "paragraph", "list_item", "caption", "footnote", "reference", "code", "formula", "checkbox_selected",
               "checkbox_unselected", "handwritten_text", "empty_value", "grading_scale"}
HEADINGS = {"title", "section_header"}
_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(s: str) -> str:
    return escape(_BAD.sub("", s or ""))


def _top_left(prov: dict, page_h: float) -> tuple[float, float, float, float]:
    b = prov["bbox"]
    if b.get("coord_origin", "TOPLEFT") == "BOTTOMLEFT":
        return b["l"], page_h - b["t"], b["r"], page_h - b["b"]
    return b["l"], b["t"], b["r"], b["b"]


def _resolve(doc: dict, ref: str) -> dict:
    _, kind, idx = ref.split("/")
    return doc[kind][int(idx)]


def _text_runs(s: str) -> str:
    """Text with line breaks and runs of spaces in ODF form."""
    out = []
    for k, line in enumerate((s or "").split("\n")):
        if k:
            out.append("<text:line-break/>")
        out.append(re.sub(r" {2,}", lambda m: f' <text:s text:c="{len(m.group(0)) - 1}"/>', _x(line)))
    return "".join(out)


class _Writer:
    def __init__(self, doc: dict):
        self.doc = doc
        self.pages = {int(k): v["size"] for k, v in (doc.get("pages") or {}).items()}
        first = self.pages.get(min(self.pages)) if self.pages else {"width": 595.276, "height": 841.89}
        self.page_w, self.page_h = first["width"], first["height"]
        self.auto: list[str] = []
        self.body: list[str] = []
        self.images: dict[str, bytes] = {}
        self.cur_page = None
        self.n_tables = 0
        self.margins = self._margins()

    # ---------------------------------------------------------------- geometry
    def _boxes(self):
        for kind in ("texts", "tables", "pictures"):
            for it in self.doc.get(kind, []):
                if it.get("content_layer", "body") != "body":
                    continue
                for p in it.get("prov", []):
                    h = self.pages.get(p["page_no"], {"height": self.page_h})["height"]
                    yield _top_left(p, h), p["page_no"]

    def _margins(self) -> dict:
        bx = [b for b, _ in self._boxes()]
        clamp = lambda v: min(max(v, 0.5 * CM), 2.5 * CM)  # noqa: E731
        if not bx:
            return {"left": 2 * CM, "right": 2 * CM, "top": 2 * CM, "bottom": 2 * CM}
        return {"left": clamp(min(b[0] for b in bx)), "right": clamp(self.page_w - max(b[2] for b in bx)),
                "top": clamp(min(b[1] for b in bx)), "bottom": clamp(self.page_h - max(b[3] for b in bx))}

    def _page_of(self, it: dict):
        return it["prov"][0]["page_no"] if it.get("prov") else None

    def _break_attr(self, it: dict) -> str:
        """Paragraph/table style suffix 'B' when this item starts a new Docling page."""
        pg = self._page_of(it)
        if pg is None:
            return ""
        brk = self.cur_page is not None and pg > self.cur_page
        self.cur_page = pg
        return "B" if brk else ""

    # ---------------------------------------------------------------- items
    def para(self, text: str, it: dict, heading: int | None = None) -> None:
        b = self._break_attr(it)
        if heading:
            self.body.append(f'<text:h text:style-name="H{b}" text:outline-level="{min(max(heading, 1), 10)}">{_text_runs(text)}</text:h>')
        else:
            self.body.append(f'<text:p text:style-name="P{b}">{_text_runs(text)}</text:p>')

    def item(self, ref: str) -> None:
        it = _resolve(self.doc, ref)
        if it.get("content_layer", "body") != "body":
            return
        kind = ref.split("/")[1]
        if kind == "texts":
            lab = it.get("label")
            if lab in HEADINGS:
                self.para(it.get("text", ""), it, heading=1 if lab == "title" else int(it.get("level") or 1) + 1)
            else:
                marker = (it.get("marker") or "").strip() if lab == "list_item" else ""
                txt = it.get("text", "")
                self.para(f"{marker} {txt}" if marker and not txt.startswith(marker) else txt, it)
            for ch in it.get("children", []):
                self.item(ch["$ref"])
        elif kind == "groups":
            if it.get("label") == "inline":
                parts = [_resolve(self.doc, c["$ref"]) for c in it.get("children", [])]
                txt = " ".join(p.get("text", "") for p in parts if "text" in p)
                first = next((p for p in parts if p.get("prov")), {})
                self.para(txt, first)
            else:
                for ch in it.get("children", []):
                    self.item(ch["$ref"])
        elif kind == "tables":
            self.table(it)
            for c in it.get("captions", []):
                self.item(c["$ref"])
        elif kind == "pictures":
            self.picture(it)
            for c in it.get("captions", []):
                self.item(c["$ref"])

    def table(self, it: dict) -> None:
        data = it.get("data") or {}
        nr, nc = int(data.get("num_rows") or 0), int(data.get("num_cols") or 0)
        cells = data.get("table_cells") or []
        if not nr or not nc:
            return
        self.n_tables += 1
        k = self.n_tables
        b = self._break_attr(it)
        pg = self._page_of(it)
        page_h = self.pages.get(pg, {"height": self.page_h})["height"] if pg else self.page_h
        tl = _top_left(it["prov"][0], page_h) if it.get("prov") else None
        avail = self.page_w - self.margins["left"] - self.margins["right"]
        width = min(tl[2] - tl[0], avail) if tl else avail
        offset = min(max((tl[0] - self.margins["left"]) if tl else 0.0, 0.0), max(avail - width, 0.0))
        # column boundaries from the cell boxes (median left edge of the cells that start in the column)
        lefts: dict[int, list[float]] = {}
        right_edge: list[float] = []
        for c in cells:
            bb = c.get("bbox")
            if not bb:
                continue
            l_, r_ = bb["l"], bb["r"]
            lefts.setdefault(c["start_col_offset_idx"], []).append(l_)
            if c["end_col_offset_idx"] == nc:
                right_edge.append(r_)
        med = lambda v: sorted(v)[len(v) // 2]  # noqa: E731
        x0 = tl[0] if tl else 0.0
        x1 = med(right_edge) if right_edge else (tl[2] if tl else x0 + width)
        bounds = [med(lefts[i]) if i in lefts else None for i in range(nc)] + [x1]
        bounds[0] = min(bounds[0], x0) if bounds[0] is not None else x0
        for i in range(1, nc):                                   # columns without a starting cell: spread evenly
            if bounds[i] is None or bounds[i] <= bounds[i - 1]:
                j = next((j for j in range(i + 1, nc + 1) if bounds[j] is not None and bounds[j] > bounds[i - 1]), None)
                if j is None:                                    # inconsistent boxes: equal columns
                    bounds = [x0 + width * q / nc for q in range(nc + 1)]
                    break
                bounds[i] = bounds[i - 1] + (bounds[j] - bounds[i - 1]) / (j - i + 1)
        raw = [max(bounds[i + 1] - bounds[i], 1.0) for i in range(nc)]
        cols = [w * width / sum(raw) for w in raw]
        self.auto.append(f'<style:style style:name="T{k}" style:family="table"><style:table-properties style:width="{width:.2f}pt" '
                         f'fo:margin-left="{offset:.2f}pt" table:align="left"{" fo:break-before=" + chr(34) + "page" + chr(34) if b else ""}/></style:style>')
        for i, w in enumerate(cols):
            self.auto.append(f'<style:style style:name="T{k}C{i}" style:family="table-column"><style:table-column-properties '
                             f'style:column-width="{w:.2f}pt"/></style:style>')
        grid: dict[tuple[int, int], dict] = {}
        covered: set[tuple[int, int]] = set()
        for c in cells:
            r0, c0 = c["start_row_offset_idx"], c["start_col_offset_idx"]
            r1, c1 = max(c["end_row_offset_idx"], r0 + 1), max(c["end_col_offset_idx"], c0 + 1)
            if (r0, c0) in grid or (r0, c0) in covered:
                continue
            grid[(r0, c0)] = c | {"_rs": r1 - r0, "_cs": c1 - c0}
            for r in range(r0, r1):
                for cc in range(c0, c1):
                    if (r, cc) != (r0, c0):
                        covered.add((r, cc))
        out = [f'<table:table table:name="Table{k}" table:style-name="T{k}">']
        out += [f'<table:table-column table:style-name="T{k}C{i}"/>' for i in range(nc)]
        for r in range(nr):
            out.append("<table:table-row>")
            for c in range(nc):
                if (r, c) in covered:
                    out.append("<table:covered-table-cell/>")
                    continue
                cell = grid.get((r, c))
                span = ""
                if cell and cell["_rs"] > 1:
                    span += f' table:number-rows-spanned="{cell["_rs"]}"'
                if cell and cell["_cs"] > 1:
                    span += f' table:number-columns-spanned="{cell["_cs"]}"'
                pstyle = "PTH" if cell and cell.get("column_header") else "PT"
                txt = _text_runs(cell.get("text", "")) if cell else ""
                out.append(f'<table:table-cell table:style-name="CB" office:value-type="string"{span}><text:p text:style-name="{pstyle}">{txt}</text:p>'
                           "</table:table-cell>")
            out.append("</table:table-row>")
        out.append("</table:table>")
        self.body.append("".join(out))

    def picture(self, it: dict) -> None:
        uri = ((it.get("image") or {}).get("uri") or "")
        m = re.match(r"data:image/(png|jpeg|jpg);base64,(.*)", uri, re.S)
        b = self._break_attr(it)
        if not m or not it.get("prov"):
            return
        n = len(self.images) + 1
        name = f"Pictures/image{n}.{'png' if m.group(1) == 'png' else 'jpg'}"
        self.images[name] = base64.b64decode(m.group(2))
        pg = self._page_of(it)
        l_, t_, r_, b_ = _top_left(it["prov"][0], self.pages.get(pg, {"height": self.page_h})["height"])
        avail = self.page_w - self.margins["left"] - self.margins["right"]
        w, h = max(r_ - l_, 1.0), max(b_ - t_, 1.0)
        if w > avail:
            w, h = avail, h * avail / w
        self.body.append(f'<text:p text:style-name="P{b}"><draw:frame draw:name="Image{n}" text:anchor-type="as-char" svg:width="{w:.2f}pt" '
                         f'svg:height="{h:.2f}pt"><draw:image xlink:href="{name}" xlink:type="simple" xlink:show="embed" '
                         'xlink:actuate="onLoad"/></draw:frame></text:p>')

    # ---------------------------------------------------------------- package
    def write(self) -> bytes:
        for ch in (self.doc.get("body") or {}).get("children", []):
            self.item(ch["$ref"])
        if not self.body:
            self.body.append('<text:p text:style-name="P"/>')
        mg = self.margins
        orient = "landscape" if self.page_w > self.page_h else "portrait"
        brk = ' fo:break-before="page"'
        auto = [f'<style:style style:name="P" style:family="paragraph"/>',
                f'<style:style style:name="PB" style:family="paragraph"><style:paragraph-properties{brk}/></style:style>',
                '<style:style style:name="H" style:family="paragraph"><style:text-properties fo:font-weight="bold" '
                'style:font-weight-asian="bold" style:font-weight-complex="bold"/></style:style>',
                f'<style:style style:name="HB" style:family="paragraph"><style:paragraph-properties{brk}/><style:text-properties '
                'fo:font-weight="bold" style:font-weight-asian="bold" style:font-weight-complex="bold"/></style:style>',
                '<style:style style:name="PT" style:family="paragraph"/>',
                '<style:style style:name="PTH" style:family="paragraph"><style:text-properties fo:font-weight="bold" '
                'style:font-weight-asian="bold" style:font-weight-complex="bold"/></style:style>',
                '<style:style style:name="CB" style:family="table-cell"><style:table-cell-properties fo:border="0.5pt solid #000000"/>'
                '</style:style>'] + self.auto
        content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {NS} office:version="{VERSION}">'
                   f'<office:automatic-styles>{"".join(auto)}</office:automatic-styles>'
                   f'<office:body><office:text>{"".join(self.body)}</office:text></office:body></office:document-content>')
        styles = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {NS} office:version="{VERSION}">'
                  '<office:automatic-styles><style:page-layout style:name="PL"><style:page-layout-properties '
                  f'fo:page-width="{self.page_w:.2f}pt" fo:page-height="{self.page_h:.2f}pt" style:print-orientation="{orient}" '
                  f'fo:margin-top="{mg["top"]:.2f}pt" fo:margin-bottom="{mg["bottom"]:.2f}pt" fo:margin-left="{mg["left"]:.2f}pt" '
                  f'fo:margin-right="{mg["right"]:.2f}pt"/></style:page-layout></office:automatic-styles>'
                  '<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="PL"/></office:master-styles>'
                  '</office:document-styles>')
        files = {"content.xml": content, "styles.xml": styles}
        man = ['<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
               f'manifest:version="{VERSION}"><manifest:file-entry manifest:full-path="/" manifest:version="{VERSION}" manifest:media-type="{MIME}"/>']
        man += [f'<manifest:file-entry manifest:full-path="{f}" manifest:media-type="text/xml"/>' for f in files]
        man += [f'<manifest:file-entry manifest:full-path="{f}" manifest:media-type="image/{"png" if f.endswith("png") else "jpeg"}"/>'
                for f in self.images]
        man.append("</manifest:manifest>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(zipfile.ZipInfo("mimetype"), MIME, compress_type=zipfile.ZIP_STORED)
            for f, s in files.items():
                z.writestr(f, s.encode("utf-8"))
            for f, data in self.images.items():
                z.writestr(f, data)
            z.writestr("META-INF/manifest.xml", "".join(man).encode("utf-8"))
        return buf.getvalue()


def docling_json_to_odt(doc: dict | str | bytes) -> bytes:
    if not isinstance(doc, dict):
        doc = json.loads(doc)
    return _Writer(doc).write()
