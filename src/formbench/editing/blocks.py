"""Experiment edit: a document as a reading-order sequence of blocks (format-neutral, from the IR + reference layout).

Block kinds: P (flow paragraph), T (table), B (text box), I (picture / drawing object). Headers and footers form their
own zones. Each block carries what the cost model compares: normalised text, paragraph / character format, the
canonical table grid with cell contents and sizes, object geometry, and the block's position in the reference layout
(used for reading order and for the layout check of floating objects)."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from ..document_ir.schema import Para, Shape, Table


def norm(s: str) -> str:
    return "".join(unicodedata.normalize("NFKC", s or "").split())


def ps_key(p: Para) -> dict:
    s = p.ps
    return {"align": s.align, "left": round(s.left, 1), "right": round(s.right, 1), "indent": round(s.indent, 1), "prev": round(s.prev, 1),
            "next": round(s.next, 1), "ls": (s.ls_type, round(s.ls_value, 1))}


def runs(p: Para) -> list[tuple[str, tuple]]:
    """(text, char-format key) of each text run, consecutive runs with the same format merged."""
    out: list = []
    for i in p.inlines:
        if i.kind not in ("text", "tab", "br") or not i.text:
            continue
        cs = i.cs or p.default_cs
        key = (round(cs.size, 1), cs.bold, cs.italic, cs.underline, cs.font) if cs else (10.0, False, False, False, "")
        if out and out[-1][1] == key:
            out[-1] = (out[-1][0] + i.text, key)
        else:
            out.append((i.text, key))
    return out


@dataclass
class Block:
    kind: str                       # P | T | B | I
    nid: str
    zone: str = "body"
    page: int = 0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    text: str = ""                  # normalised text (P, B: all paragraphs)
    raw: str = ""
    runs: list = field(default_factory=list)
    ps: dict = field(default_factory=dict)
    paras: list = field(default_factory=list)       # B / table cells: list of (raw text, ps, runs)
    grid: dict = field(default_factory=dict)        # T: rows, cols, cells {(r,c): (rs, cs, text, paras)}, col_w, row_h
    obj_kind: str = ""
    gap: float = 0.0                # flow blocks: vertical distance to the previous flow block on the page (own geometry)


def _para_info(p: Para) -> tuple:
    return (p.text, ps_key(p), runs(p))


def canonical_grid(t: Table) -> dict:
    cells = [(c.row, c.col, max(1, c.rowspan), max(1, c.colspan), c) for c in t.cells]
    rb = sorted({r for r, _, rs, _, _ in cells} | {r + rs for r, _, rs, _, _ in cells})
    cb = sorted({c for _, c, _, _, _ in cells} | {c + cs for _, c, _, cs, _ in cells})
    ri, ci = {v: k for k, v in enumerate(rb)}, {v: k for k, v in enumerate(cb)}
    out, col_w, row_h = {}, {}, {}
    for r, c, rs, cs, cell in cells:
        key = (ri[r], ci[c])
        paras = [_para_info(p) for p in cell.paras]
        out[key] = (ri[r + rs] - ri[r], ci[c + cs] - ci[c], norm("".join(p[0] for p in paras)), paras)
        if ci[c + cs] - ci[c] == 1 and getattr(cell, "width", 0):
            col_w[ci[c]] = round(cell.width, 1)
        if ri[r + rs] - ri[r] == 1 and getattr(cell, "height", 0):
            row_h[ri[r]] = round(cell.height, 1)
    return {"rows": len(rb) - 1, "cols": len(cb) - 1, "cells": out, "col_w": col_w, "row_h": row_h,
            "borders": len({getattr(cell, "bf", None) and cell.bf.semantic_key() for *_, cell in cells})}


def extract(sess, layout=None) -> dict[str, list[Block]]:
    """Blocks per zone (body / header / footer), each zone in reading order (page, y, x)."""
    if layout is None:
        from ..ui.human import HumanSession
        layout = HumanSession(sess, {}).layout
    pos: dict = {}
    for b in layout.boxes:
        if b.nid and b.nid not in pos:
            pos[b.nid] = (b.page, b.x, b.y, b.w, b.h)
    zones: dict[str, list[Block]] = {"body": [], "header": [], "footer": []}

    def add(zone, blk):
        p = pos.get(blk.nid)
        if p:
            blk.page, blk.x, blk.y, blk.w, blk.h = p
        zones[zone].append(blk)

    def walk(paras, zone):
        for p in paras:
            objs = p.objects()
            if p.text.strip() or not objs:
                add(zone, Block("P", p.nid, zone, text=norm(p.text), raw=p.text, runs=runs(p), ps=ps_key(p)))
            for o in objs:
                if isinstance(o, Table):
                    add(zone, Block("T", o.nid, zone, grid=canonical_grid(o),
                                    text=norm("".join(pp.text for c in o.cells for pp in c.paras))))
                elif isinstance(o, Shape) and o.is_textbox:
                    infos = [_para_info(q) for q in o.paras]
                    add(zone, Block("B", o.nid, zone, paras=infos, text=norm("".join(i[0] for i in infos)),
                                    w=o.width, h=o.height))
                elif isinstance(o, Shape):
                    add(zone, Block("I", o.nid, zone, obj_kind=o.kind, w=o.width, h=o.height))
    for s in sess.ir.sections:
        walk(s.paras, "body")
        for _, ps in s.headers:
            walk(ps, "header")
        for _, ps in s.footers:
            walk(ps, "footer")
    for z in zones.values():
        z.sort(key=lambda b: (b.page, round(b.y, 0), b.x))
        prev = None
        for b in z:
            if b.kind in ("P", "T"):
                b.gap = b.y - (prev.y + prev.h) if prev is not None and prev.page == b.page else b.y
                prev = b
    # empty flow paragraphs carry layout (spacing) but no content: keep them, the cost model treats them as cheap
    return zones


def page_setup(sess) -> dict:
    s = sess.ir.sections[0] if sess.ir.sections else None
    if s is None:
        return {}
    p = s.page
    return {k: round(getattr(p, k), 0) for k in ("width", "height", "left", "right", "top", "bottom")} | {"landscape": p.landscape,
                                                                                                         "cols": p.col_count}
