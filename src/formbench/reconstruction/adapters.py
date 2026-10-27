"""Page analysis shared by reconstruction baselines: ruling lines, table grids, text units (vector text or OCR).

Baselines only ever receive a ``ReconInput`` (PDF bytes and/or page rasters). Nothing here can see the original
HWPX, the completed document or the task contract.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field

import numpy as np
import pymupdf

from ..util import Status

ANALYSIS_DPI = 150


@dataclass(frozen=True)
class ReconInput:
    kind: str                                   # vector_pdf | scanned_pdf | image
    pdf: bytes | None = None
    images: tuple = ()                          # page rasters (PNG/JPEG bytes) for kind == image
    dpi: int = 150                              # declared scan resolution of ``images``
    label: str = ""
    lang: str = "ko"                            # document language: picks the OCR model (ko -> Korean, else CH)


@dataclass
class TextUnit:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool = False
    score: float = 1.0


@dataclass
class PageAnalysis:
    width: float
    height: float
    image: bytes                                # page raster (PNG) at ANALYSIS_DPI
    hlines: list = field(default_factory=list)  # (x0, y, x1)
    vlines: list = field(default_factory=list)  # (x, y0, y1)
    units: list = field(default_factory=list)   # TextUnit
    text_source: str = "none"                   # vector | ocr | none
    notes: list = field(default_factory=list)


# ----------------------------------------------------------------------------- OCR adapter
_ENGINES: dict = {}


def ocr_engine(lang: str = "ko"):
    """Cached OCR engine per script: Korean PP-OCRv5 for Korean documents, the PP-OCRv5 CH model (Chinese, Japanese,
    Latin) for every other language (2026-10-16: observed corpus is mostly ja / zh / en)."""
    key = "ko" if lang in ("ko", "ko+en") else "ch"
    if key not in _ENGINES:
        _ENGINES[key] = _make_engine(key)
    return _ENGINES[key]


def _make_engine(key: str):
    try:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    except ImportError:
        return None
    try:
        import logging
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
        import cv2
        cv2.setNumThreads(1)
        threads = int(os.environ.get("FORMBENCH_OCR_THREADS", "2"))        # several OCR workers run side by side: cap per-process threads
        return RapidOCR(params={"Rec.lang_type": LangRec.KOREAN if key == "ko" else LangRec.CH, "Rec.ocr_version": OCRVersion.PPOCRV5,
                                "Rec.model_type": ModelType.MOBILE,
                                "Global.log_level": "error", "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                                "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    except Exception:      # noqa: BLE001 - model download or runtime failure => adapter unavailable
        return None


def ocr_available() -> tuple[bool, str]:
    return (ocr_engine() is not None, "" if ocr_engine() is not None else "rapidocr (onnxruntime, Korean PP-OCRv5 model) not available")


def ocr_version() -> dict:
    try:
        import onnxruntime
        import rapidocr
        return {"engine": "rapidocr", "version": getattr(rapidocr, "__version__", "3.x"), "model": "PP-OCRv5 mobile rec (korean for ko documents, ch for others) + det",
                "onnxruntime": onnxruntime.__version__}
    except ImportError:
        return {"engine": "rapidocr", "status": Status.BLOCKED_BY_ENVIRONMENT}


def run_ocr(img: np.ndarray, px_to_pt: float, lang: str = "ko") -> list[TextUnit]:
    eng = ocr_engine(lang)
    if eng is None:
        return []
    res = eng(img)
    out = []
    if res is None or res.txts is None:
        return out
    for box, txt, score in zip(res.boxes, res.txts, res.scores):
        if not txt.strip() or score < 0.5:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        x0, x1, y0, y1 = min(xs) * px_to_pt, max(xs) * px_to_pt, min(ys) * px_to_pt, max(ys) * px_to_pt
        out.append(TextUnit(x0, y0, x1, y1, txt.strip(), max(6.0, min(28.0, round((y1 - y0) * 0.78, 1))), score=float(score)))
    return out


# ----------------------------------------------------------------------------- lines
def raster_lines(gray: np.ndarray, px_to_pt: float) -> tuple[list, list]:
    import cv2
    h, w = gray.shape[:2]
    bw = (gray < 150).astype(np.uint8) * 255
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(w / 45), 12), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(int(h / 70), 10)))
    hl, vl = [], []
    for kern, store, horiz in ((hk, hl, True), (vk, vl, False)):
        m = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kern)
        n, _lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        for i in range(1, n):
            x, y, ww, hh, _a = stats[i]
            if horiz and ww >= 12 and hh <= max(6, ww * 0.05):
                store.append((x * px_to_pt, (y + hh / 2) * px_to_pt, (x + ww) * px_to_pt))
            elif not horiz and hh >= 10 and ww <= max(6, hh * 0.05):
                store.append(((x + ww / 2) * px_to_pt, y * px_to_pt, (y + hh) * px_to_pt))
    return hl, vl


def vector_lines(page: pymupdf.Page) -> tuple[list, list]:
    hl, vl = [], []

    def add(p, q):
        if abs(p.y - q.y) < 0.8 and abs(p.x - q.x) > 6:
            hl.append((min(p.x, q.x), (p.y + q.y) / 2, max(p.x, q.x)))
        elif abs(p.x - q.x) < 0.8 and abs(p.y - q.y) > 6:
            vl.append(((p.x + q.x) / 2, min(p.y, q.y), max(p.y, q.y)))
    for d in page.get_drawings():
        stroked = d.get("color") is not None
        for it in d["items"]:
            if it[0] == "l":
                add(it[1], it[2])
            elif it[0] == "re":
                r = it[1]
                if r.width < 2.0 and r.height > 6:
                    vl.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                elif r.height < 2.0 and r.width > 6:
                    hl.append((r.x0, (r.y0 + r.y1) / 2, r.x1))
                elif stroked and r.width > 6 and r.height > 6:
                    add(r.tl, r.tr)
                    add(r.bl, r.br)
                    add(r.tl, r.bl)
                    add(r.tr, r.br)
            elif it[0] == "qu":
                q = it[1]
                if stroked:
                    add(q.ul, q.ur)
                    add(q.ll, q.lr)
                    add(q.ul, q.ll)
                    add(q.ur, q.lr)
    return hl, vl


def merge_collinear(segs: list, horiz: bool, tol: float = 1.5, gap: float = 3.0) -> list:
    """Merge segments on (almost) the same line that touch or overlap."""
    if horiz:
        items = sorted(((y, x0, x1) for x0, y, x1 in segs))
    else:
        items = sorted(((x, y0, y1) for x, y0, y1 in segs))
    out: list[list] = []
    for c, a, b in items:
        hit = None
        for o in reversed(out[-40:]):
            if abs(o[0] - c) <= tol and a <= o[2] + gap and b >= o[1] - gap:
                hit = o
                break
        if hit:
            n = hit[3]
            hit[0] = (hit[0] * n + c) / (n + 1)
            hit[1], hit[2], hit[3] = min(hit[1], a), max(hit[2], b), n + 1
        else:
            out.append([c, a, b, 1])
    return [((a, c, b) if horiz else (c, a, b)) for c, a, b, _ in out]


# ----------------------------------------------------------------------------- page analysis
_ANALYSIS_CACHE: dict = {}


def analyze(inp: ReconInput) -> list[PageAnalysis]:
    """Page analysis of one input, memoised per process (several baselines read the same input; OCR is the cost)."""
    import hashlib
    h = hashlib.sha256()
    h.update(inp.kind.encode())
    h.update(inp.pdf or b"")
    for im in inp.images:
        h.update(im)
    key = (h.hexdigest(), inp.dpi, inp.lang)
    if key not in _ANALYSIS_CACHE:
        if len(_ANALYSIS_CACHE) > 8:
            _ANALYSIS_CACHE.clear()
        _ANALYSIS_CACHE[key] = _analyze(inp)
    import copy
    return copy.deepcopy(_ANALYSIS_CACHE[key])


def _analyze(inp: ReconInput) -> list[PageAnalysis]:
    import cv2
    pages: list[PageAnalysis] = []
    if inp.kind in ("vector_pdf", "scanned_pdf"):
        doc = pymupdf.open(stream=inp.pdf, filetype="pdf")
        for page in doc:
            png = page.get_pixmap(dpi=ANALYSIS_DPI, alpha=False).tobytes("png")
            pa = PageAnalysis(page.rect.width, page.rect.height, png)
            words = page.get_text("words")
            if inp.kind == "vector_pdf" and words:
                spans = [(s["bbox"], s["size"], bool(s["flags"] & 16) or "bold" in s["font"].lower())
                         for b in page.get_text("dict")["blocks"] if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]]
                for w in words:
                    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
                    size, bold = next(((sz, bd) for bb, sz, bd in spans if bb[0] - 1 <= cx <= bb[2] + 1 and bb[1] - 1 <= cy <= bb[3] + 1), (w[3] - w[1], False))
                    # glyph box of height ``size`` anchored at the baseline region of the extracted word box
                    pa.units.append(TextUnit(w[0], w[3] - size * 1.18, w[2], w[3] - size * 0.18, w[4], round(size, 2), bold))
                pa.text_source = "vector"
                pa.hlines, pa.vlines = vector_lines(page)
            else:
                gray = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
                k = 72.0 / ANALYSIS_DPI
                pa.hlines, pa.vlines = raster_lines(gray, k)
                ok, why = ocr_available()
                if ok:
                    pa.units = run_ocr(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), k, inp.lang)
                    pa.text_source = "ocr"
                else:
                    pa.notes.append(f"{Status.BLOCKED_BY_ENVIRONMENT}: {why}")
            pa.hlines, pa.vlines = merge_collinear(pa.hlines, True), merge_collinear(pa.vlines, False)
            pages.append(pa)
        doc.close()
        return pages
    for img_bytes in inp.images:
        arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise ValueError("undecodable page image")
        k = 72.0 / inp.dpi
        h, w = arr.shape[:2]
        scale = ANALYSIS_DPI / inp.dpi
        work = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC) if abs(scale - 1) > 0.02 else arr
        ok_png, enc = cv2.imencode(".png", arr)
        pa = PageAnalysis(w * k, h * k, enc.tobytes())
        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        kk = 72.0 / ANALYSIS_DPI
        pa.hlines, pa.vlines = raster_lines(gray, kk)
        ok, why = ocr_available()
        if ok:
            pa.units = run_ocr(work, kk, inp.lang)
            pa.text_source = "ocr"
        else:
            pa.notes.append(f"{Status.BLOCKED_BY_ENVIRONMENT}: {why}")
        pa.hlines, pa.vlines = merge_collinear(pa.hlines, True), merge_collinear(pa.vlines, False)
        pages.append(pa)
    return pages


# ----------------------------------------------------------------------------- table grids
@dataclass
class GridCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    x0: float
    y0: float
    x1: float
    y1: float
    borders: dict = field(default_factory=dict)          # left/right/top/bottom -> bool
    units: list = field(default_factory=list)


@dataclass
class Grid:
    xs: list
    ys: list
    cells: list
    bbox: tuple

    @property
    def n_rows(self) -> int:
        return len(self.ys) - 1

    @property
    def n_cols(self) -> int:
        return len(self.xs) - 1


def _cluster(vals: list[float], tol: float) -> list[float]:
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _covered(segs: list[tuple[float, float]], a: float, b: float) -> float:
    if b <= a:
        return 0.0
    cov, cur = 0.0, a
    for s0, s1 in sorted(segs):
        s0, s1 = max(s0, cur), min(s1, b)
        if s1 > s0:
            cov += s1 - s0
            cur = s1
    return cov / (b - a)


def build_grids(pa: PageAnalysis, tol: float = 2.5, min_w: float = 40.0, min_h: float = 14.0) -> tuple[list[Grid], list, list]:
    """Group ruling lines into table grids with merged cells. Returns (grids, leftover hlines, leftover vlines)."""
    hl, vl = list(pa.hlines), list(pa.vlines)
    n = len(hl) + len(vl)
    if not hl:
        return [], hl, vl
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, (x0, y, x1) in enumerate(hl):
        for j, (x, y0, y1) in enumerate(vl):
            if x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol:
                parent[find(i)] = find(len(hl) + j)
    # horizontal lines stacked with the same x extent (tables without vertical rules) belong together
    order = sorted(range(len(hl)), key=lambda i: hl[i][1])
    for a, b in zip(order, order[1:]):
        (ax0, ay, ax1), (bx0, by, bx1) = hl[a], hl[b]
        if abs(ax0 - bx0) <= 6 and abs(ax1 - bx1) <= 6 and by - ay <= 140 and (ax1 - ax0) > 150:
            parent[find(a)] = find(b)
    comps: dict[int, list[int]] = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    grids, used = [], set()
    for members in comps.values():
        hs = [hl[i] for i in members if i < len(hl)]
        vs = [vl[i - len(hl)] for i in members if i >= len(hl)]
        if len(hs) < 2:
            continue
        x_lo, x_hi = min(h[0] for h in hs), max(h[2] for h in hs)
        y_lo, y_hi = min(h[1] for h in hs), max(h[1] for h in hs)
        if vs:
            y_lo, y_hi = min(y_lo, min(v[1] for v in vs)), max(y_hi, max(v[2] for v in vs))
        if x_hi - x_lo < min_w or y_hi - y_lo < min_h:
            continue
        xs = _cluster([v[0] for v in vs] + [x_lo, x_hi], tol * 1.6)
        ys = _cluster([h[1] for h in hs] + [y_lo, y_hi], tol * 1.6)
        if len(xs) < 2 or len(ys) < 2 or (len(xs) - 1) * (len(ys) - 1) > 4000:
            continue
        nr, nc = len(ys) - 1, len(xs) - 1

        def has_h(i, j, hs=hs, xs=xs, ys=ys):
            return _covered([(h[0], h[2]) for h in hs if abs(h[1] - ys[i]) <= tol * 1.6], xs[j] + tol, xs[j + 1] - tol) >= 0.75

        def has_v(i, j, vs=vs, xs=xs, ys=ys):
            return _covered([(v[1], v[2]) for v in vs if abs(v[0] - xs[j]) <= tol * 1.6], ys[i] + tol, ys[i + 1] - tol) >= 0.75
        par = list(range(nr * nc))

        def f2(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i
        for i in range(nr):
            for j in range(nc):
                if j + 1 < nc and not has_v(i, j + 1):
                    par[f2(i * nc + j)] = f2(i * nc + j + 1)
                if i + 1 < nr and not has_h(i + 1, j):
                    par[f2(i * nc + j)] = f2((i + 1) * nc + j)
        groups: dict[int, list[tuple[int, int]]] = {}
        for i in range(nr):
            for j in range(nc):
                groups.setdefault(f2(i * nc + j), []).append((i, j))
        cells = []
        for cellset in groups.values():
            r0, r1 = min(c[0] for c in cellset), max(c[0] for c in cellset)
            c0, c1 = min(c[1] for c in cellset), max(c[1] for c in cellset)
            if len(cellset) == (r1 - r0 + 1) * (c1 - c0 + 1):
                rects = [(r0, c0, r1, c1)]
            else:                                   # non-rectangular union: keep the unit cells
                rects = [(i, j, i, j) for i, j in cellset]
            for a0, b0, a1, b1 in rects:
                cells.append(GridCell(a0, b0, a1 - a0 + 1, b1 - b0 + 1, xs[b0], ys[a0], xs[b1 + 1], ys[a1 + 1],
                                      {"top": all(has_h(a0, j) for j in range(b0, b1 + 1)), "bottom": all(has_h(a1 + 1, j) for j in range(b0, b1 + 1)),
                                       "left": all(has_v(i, b0) for i in range(a0, a1 + 1)), "right": all(has_v(i, b1 + 1) for i in range(a0, a1 + 1))}))
        cells.sort(key=lambda c: (c.row, c.col))
        grids.append(Grid(xs, ys, cells, (x_lo, y_lo, x_hi, y_hi)))
        used.update(members)
    grids.sort(key=lambda g: (g.bbox[1], g.bbox[0]))
    # nested / overlapping grids: keep the larger one
    kept: list[Grid] = []
    for g in sorted(grids, key=lambda g: -(g.bbox[2] - g.bbox[0]) * (g.bbox[3] - g.bbox[1])):
        if not any(k.bbox[0] - 2 <= g.bbox[0] and k.bbox[1] - 2 <= g.bbox[1] and k.bbox[2] + 2 >= g.bbox[2] and k.bbox[3] + 2 >= g.bbox[3] for k in kept):
            kept.append(g)
    kept.sort(key=lambda g: (g.bbox[1], g.bbox[0]))
    rest_h = [hl[i] for i in range(len(hl)) if i not in used]
    rest_v = [vl[i] for i in range(len(vl)) if (i + len(hl)) not in used]
    return kept, rest_h, rest_v


def assign_units(grids: list[Grid], units: list[TextUnit]) -> list[TextUnit]:
    """Put each text unit into the grid cell containing its centre; return the units outside all tables."""
    free = []
    for u in units:
        cx, cy = (u.x0 + u.x1) / 2, (u.y0 + u.y1) / 2
        placed = False
        for g in grids:
            if g.bbox[0] - 1 <= cx <= g.bbox[2] + 1 and g.bbox[1] - 1 <= cy <= g.bbox[3] + 1:
                for c in g.cells:
                    if c.x0 - 0.5 <= cx <= c.x1 + 0.5 and c.y0 - 0.5 <= cy <= c.y1 + 0.5:
                        c.units.append(u)
                        placed = True
                        break
            if placed:
                break
        if not placed:
            free.append(u)
    return free


def group_lines(units: list[TextUnit]) -> list[list[TextUnit]]:
    """Group text units into visual lines (same baseline band), each sorted left to right."""
    lines: list[list[TextUnit]] = []
    for u in sorted(units, key=lambda u: ((u.y0 + u.y1) / 2, u.x0)):
        cy = (u.y0 + u.y1) / 2
        for ln in reversed(lines[-6:]):
            ly0, ly1 = min(x.y0 for x in ln), max(x.y1 for x in ln)
            if ly0 + (ly1 - ly0) * 0.25 <= cy <= ly1 - (ly1 - ly0) * 0.25:
                ln.append(u)
                break
        else:
            lines.append([u])
    for ln in lines:
        ln.sort(key=lambda u: u.x0)
    lines.sort(key=lambda ln: (min(u.y0 for u in ln), ln[0].x0))
    return lines


def line_text(ln: list[TextUnit]) -> str:
    """Join units of a visual line; wide gaps become runs of spaces (half-em each) so alignment survives."""
    out = ln[0].text
    for a, b in zip(ln, ln[1:]):
        gap = b.x0 - a.x1
        size = max(a.size, 6.0)
        out += " " * max(1, int(round(gap / (size * 0.5)))) + b.text
    return out


def cell_is_blank(pa: PageAnalysis, x0: float, y0: float, x1: float, y1: float, gray_cache: dict) -> bool:
    """True if the raster shows (almost) no ink inside the rectangle (used by the overlay baseline)."""
    import cv2
    if "g" not in gray_cache:
        gray_cache["g"] = cv2.imdecode(np.frombuffer(pa.image, np.uint8), cv2.IMREAD_GRAYSCALE)
    g = gray_cache["g"]
    k = g.shape[1] / pa.width
    a, b, c, d = int((x0 + 2.5) * k), int((y0 + 2.5) * k), int((x1 - 2.5) * k), int((y1 - 2.5) * k)
    if c - a < 4 or d - b < 4:
        return False
    return float((g[b:d, a:c] < 140).mean()) < 0.004
