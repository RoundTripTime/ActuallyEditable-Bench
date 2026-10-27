"""Experiment edit "same document" judge: is document D the example E? (text + structure + layout, user decision 2026-10-17)

Both documents are rendered by their own application (E and an original-format D: Word / Hangul / LibreOffice; an ODT
reconstruction: LibreOffice, with the host's Windows and Hancom fonts).

* text + layout — glyph correspondence: every visible glyph of E must have a glyph with the same character on the same
  page of D within TOL_PT (one line height), one to one, and D must have no glyph left over. Reading order is not
  needed (it is ambiguous between tables, text boxes and flow), and text and position are judged together.
* structure — tables: the tables of E and D, in document order, must have the same canonical grid (only the row/column
  boundaries some cell actually uses), the same merged-cell spans and the same cell texts (white space ignored).

The verdict is deterministic and lists what differs (missing / extra / misplaced glyphs, table differences)."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

TOL_PT = 12.0
JUDGE_VERSION = "jd-1.0"


def _norm_char(c: str) -> str:
    return unicodedata.normalize("NFKC", c)


def glyphs(pdf: bytes) -> list[tuple]:
    """(page, char, x, y) of every visible, non-space glyph (centre point), NFKC-normalised."""
    import pymupdf
    out = []
    for pno, page in enumerate(pymupdf.open(stream=pdf, filetype="pdf")):
        for b in page.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    for ch in sp["chars"]:
                        c = _norm_char(ch["c"])
                        for k, cc in enumerate(c):           # NFKC may expand a glyph (e.g. ㎡ -> m2)
                            if cc.strip():
                                x0, y0, x1, y1 = ch["bbox"]
                                out.append((pno, cc, (x0 + x1) / 2 + k * 0.01, (y0 + y1) / 2))
    return out


def match_glyphs(e: list[tuple], d: list[tuple], tol: float = TOL_PT) -> tuple[list, list, list]:
    """Greedy one-to-one matching by increasing distance among same (page, char) pairs within tol (Chebyshev).
    Returns (pairs, unmatched_e, unmatched_d)."""
    import bisect
    by_key: dict = {}
    for j, g in enumerate(d):
        by_key.setdefault((g[0], g[1]), []).append((g[3], g[2], j))
    for v in by_key.values():
        v.sort()
    cand = []
    for i, g in enumerate(e):
        lst = by_key.get((g[0], g[1]))
        if not lst:
            continue
        ys = [t[0] for t in lst]
        lo, hi = bisect.bisect_left(ys, g[3] - tol), bisect.bisect_right(ys, g[3] + tol)
        for y, x, j in lst[lo:hi]:
            dist = max(abs(x - g[2]), abs(y - g[3]))
            if dist <= tol:
                cand.append((dist, i, j))
    cand.sort()
    used_e, used_d, pairs = set(), set(), []
    for dist, i, j in cand:
        if i in used_e or j in used_d:
            continue
        used_e.add(i)
        used_d.add(j)
        pairs.append((i, j, dist))
    return pairs, [g for i, g in enumerate(e) if i not in used_e], [g for j, g in enumerate(d) if j not in used_d]


def _ws(s: str) -> str:
    return "".join(unicodedata.normalize("NFKC", s or "").split())


def canonical_table(t) -> dict:
    """Grid of the boundaries cells actually use; spans and texts on that grid."""
    cells = [(c.row, c.col, max(1, c.rowspan), max(1, c.colspan), _ws(c.text if isinstance(c.text, str) else "".join(p.text for p in c.paras)))
             for c in t.cells]
    rb = sorted({r for r, _, rs, _, _ in cells} | {r + rs for r, _, rs, _, _ in cells})
    cb = sorted({c for _, c, _, _, _ in cells} | {c + cs for _, c, _, cs, _ in cells})
    ri, ci = {v: k for k, v in enumerate(rb)}, {v: k for k, v in enumerate(cb)}
    canon = sorted((ri[r], ci[c], ri[r + rs] - ri[r], ci[c + cs] - ci[c], txt) for r, c, rs, cs, txt in cells)
    return {"rows": len(rb) - 1, "cols": len(cb) - 1, "cells": canon}


def tables_of(sess) -> list[dict]:
    return [canonical_table(t) for t in sess.ir.tables()]


def compare_tables(te: list[dict], td: list[dict]) -> list[dict]:
    diffs = []
    if len(te) != len(td):
        diffs.append({"kind": "table_count", "example": len(te), "document": len(td)})
    for k, (a, b) in enumerate(zip(te, td)):
        if (a["rows"], a["cols"]) != (b["rows"], b["cols"]):
            diffs.append({"kind": "grid", "table": k, "example": [a["rows"], a["cols"]], "document": [b["rows"], b["cols"]]})
            continue
        sa = {(r, c, rs, cs) for r, c, rs, cs, _ in a["cells"]}
        sb = {(r, c, rs, cs) for r, c, rs, cs, _ in b["cells"]}
        if sa != sb:
            diffs.append({"kind": "merges", "table": k, "missing": sorted(sa - sb)[:20], "extra": sorted(sb - sa)[:20]})
            continue
        ta = {(r, c): t for r, c, _, _, t in a["cells"]}
        tb = {(r, c): t for r, c, _, _, t in b["cells"]}
        bad = [rc for rc in ta if ta[rc] != tb.get(rc)]
        if bad:
            diffs.append({"kind": "cell_text", "table": k, "cells": [list(x) for x in bad[:20]], "n": len(bad)})
    return diffs


@dataclass
class Verdict:
    same: bool
    text_layout_ok: bool
    structure_ok: bool
    pages_ok: bool
    missing: list = field(default_factory=list)       # E glyphs without a counterpart in D
    extra: list = field(default_factory=list)         # D glyphs left over
    table_diffs: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"same": self.same, "text_layout_ok": self.text_layout_ok, "structure_ok": self.structure_ok, "pages_ok": self.pages_ok,
                "missing": len(self.missing), "extra": len(self.extra), "table_diffs": self.table_diffs[:10], **self.stats}


def judge(example_pdf: bytes, example_sess, doc_pdf: bytes, doc_sess, tol: float = TOL_PT) -> Verdict:
    import pymupdf
    ge, gd = glyphs(example_pdf), glyphs(doc_pdf)
    pairs, me, md = match_glyphs(ge, gd, tol)
    ne, nd = len(pymupdf.open(stream=example_pdf, filetype="pdf")), len(pymupdf.open(stream=doc_pdf, filetype="pdf"))
    tdiff = compare_tables(tables_of(example_sess), tables_of(doc_sess)) if example_sess is not None and doc_sess is not None else []
    v = Verdict(same=False, text_layout_ok=not me and not md, structure_ok=not tdiff, pages_ok=ne == nd, missing=me, extra=md, table_diffs=tdiff,
                stats={"glyphs_example": len(ge), "glyphs_document": len(gd), "matched": len(pairs), "pages_example": ne, "pages_document": nd,
                       "mean_offset_pt": round(sum(p[2] for p in pairs) / len(pairs), 2) if pairs else None, "tol_pt": tol, "version": JUDGE_VERSION})
    v.same = v.text_layout_ok and v.structure_ok and v.pages_ok
    return v
