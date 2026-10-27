"""ODT adapter (odfpy): text/table extraction and a text+table conversion to HWPX. Drawing objects and styles
beyond paragraph text are not converted (reported as conversion losses)."""
from __future__ import annotations

import io

from .hwpx_build import HwpxBuilder, make_p, make_table


def _text(node) -> str:
    out = []
    for ch in node.childNodes:
        if ch.nodeType == ch.TEXT_NODE:
            out.append(ch.data)
        else:
            name = ch.qname[1] if hasattr(ch, "qname") else ""
            out.append("\t" if name == "tab" else "\n" if name == "line-break" else " " * int(ch.getAttribute("c") or 1) if name == "s" else _text(ch))
    return "".join(out)


def _walk(doc):
    from odf import table as otable
    from odf import text as otext
    def rec(node):
        for ch in node.childNodes:
            if ch.nodeType != ch.ELEMENT_NODE:
                continue
            if ch.qname == otable.Table().qname:
                yield ("table", ch)
            elif ch.qname in (otext.P().qname, otext.H(outlinelevel=1).qname):
                yield ("p", ch)
            else:
                yield from rec(ch)
    yield from rec(doc.text)


def odt_info(data: bytes) -> dict:
    from odf.opendocument import load
    doc = load(io.BytesIO(data))
    items = list(_walk(doc))
    text = "\n".join(_text(n) for k, n in items if k == "p")
    return {"text": text, "paragraphs": sum(1 for k, _ in items if k == "p"), "tables": sum(1 for k, _ in items if k == "table"), "page_count": None}


def odt_to_hwpx(data: bytes) -> tuple[bytes, dict]:
    from odf import table as otable
    from odf import text as otext
    from odf.opendocument import load
    doc = load(io.BytesIO(data))
    b = HwpxBuilder()
    body_w = 595.28 - 2 * 56.7
    loss = {"styles_not_converted": 1}
    for kind, node in _walk(doc):
        if kind == "p":
            b.add_para(_text(node))
            continue
        rows = [r for r in node.getElementsByType(otable.TableRow)]
        grid = [[_text_cell(c, otext) for c in r.getElementsByType(otable.TableCell)] for r in rows]
        n_cols = max((len(r) for r in grid), default=0)
        if not n_cols:
            continue
        cells = [{"row": i, "col": j, "paras": [make_p(t)]} for i, r in enumerate(grid) for j, t in enumerate(r)]
        b.attach(make_table(b.next_id(), [body_w / n_cols] * n_cols, [20.0] * len(grid), cells, b.head.border_fill(), inline=True), b.add_para(""))
        loss["table_merges_not_converted"] = loss.get("table_merges_not_converted", 0) + 1
    return b.to_bytes(), {"conversion_losses": loss}


def _text_cell(cell, otext) -> str:
    return "\n".join(_text(p) for p in cell.getElementsByType(otext.P))
