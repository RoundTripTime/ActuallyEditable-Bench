"""Goal(D, tau): structure-agnostic verification of a task contract on a rendered document.

Field values are located geometrically (anchor-relative regions in the reference renderer's coordinates), so a
native cell, an ordinary paragraph or a text box can all satisfy the same predicate (RQ3). Protected predicates
compare the edited document with *its own* blank state D0.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from ..document_ir.layout import Layout, layout_document
from ..document_ir.normalize import DocIR, build_ir
from ..document_ir.schema import Cell, Shape, Table
from ..formats.hwpx import HwpxPackage
from .schema import CHECKED_GLYPHS

PAD = 2.5
INK_THRESHOLD = 0.02


def norm(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", s or "") if not ch.isspace())


def _strip_map(s: str) -> tuple[str, list[int]]:
    out, idx = [], []
    for i, ch in enumerate(s):
        n = unicodedata.normalize("NFKC", ch)
        if ch.isspace():
            continue
        for c in n:
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _ink_runs(fr) -> list[tuple[float, float]]:
    """x-extents (relative to the fragment) of maximal runs of non-blank characters."""
    runs, start = [], None
    for i, ch in enumerate(fr.text):
        if ch.isspace():
            if start is not None:
                runs.append((fr.xs[start], fr.xs[i]))
                start = None
        elif start is None:
            start = i
    if start is not None:
        runs.append((fr.xs[start], fr.xs[len(fr.text)]))
    return runs


_STRIP_CACHE: dict[str, tuple] = {}


def _strip_cached(s: str) -> tuple[str, list[int]]:
    hit = _STRIP_CACHE.get(s)
    if hit is None:
        if len(_STRIP_CACHE) > 50000:
            _STRIP_CACHE.clear()
        hit = _STRIP_CACHE[s] = _strip_map(s)
    return hit


def _inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


class DocView:
    """IR + layout + per-paragraph geometry caches for one document state."""

    def __init__(self, pkg: HwpxPackage, ir: DocIR | None = None, layout: Layout | None = None):
        self.pkg = pkg
        self.ir = ir or build_ir(pkg)
        self.layout = layout or layout_document(self.ir)
        self.para_frags: dict[str, list] = {}
        self._strip: dict[str, tuple] = {}
        self._sites: dict[str, list] = {}
        for f in self.layout.frags:
            if f.para is not None and not f.generated:
                self.para_frags.setdefault(f.para, []).append(f)
        self.para_box = {b.nid: b for b in self.layout.boxes if b.kind == "para"}
        self.boxes = {}
        for b in self.layout.boxes:
            if b.kind != "para" and b.nid and b.nid not in self.boxes:
                self.boxes[b.nid] = b
        self.text = {p.nid: p.text for p in self.ir.paras()}
        # paragraphs whose line is occupied by an inline table / inline object are not free text containers
        self.blocked = {p.nid for p in self.ir.paras() if any(i.kind == "obj" and (isinstance(i.obj, Table) or i.obj.pos.treat_as_char or
                                                                                  (isinstance(i.obj, Table) and not i.obj.pos.treat_as_char))
                                                               for i in p.inlines) and not p.text.strip()}
        self.ntext = {k: norm(v) for k, v in self.text.items()}
        self.order = sorted(self.para_box, key=lambda n: (self.para_box[n].page, round(self.strip(n)[2], 1), self.strip(n)[1]))
        self._img: dict[str, np.ndarray | None] = {}

    @classmethod
    def of(cls, session) -> DocView:
        return cls(session.pkg, session.ir, session.layout)

    def owner(self, nid: str) -> str | None:
        b = self.para_box.get(nid)
        return b.extra.get("owner") if b else None

    def strip(self, nid: str) -> tuple:
        """First-line strip of a paragraph: (page, x0, y0, x1, y1)."""
        c = self._strip.get(nid)
        if c is None:
            c = self._strip[nid] = self._strip_uncached(nid)
        return c

    def _strip_uncached(self, nid: str) -> tuple:
        b = self.para_box[nid]
        frs = self.para_frags.get(nid)
        if frs:
            y0 = min(f.y for f in frs)
            first = [f for f in frs if f.y < y0 + 0.5 * frs[0].h]
            return (first[0].page, b.x, y0, b.x1, max(f.y1 for f in first))
        p = self.ir.nodes.get(nid)
        size = p.default_cs.size if p is not None and p.default_cs else 10.0
        return (b.page, b.x, b.y, b.x1, b.y + min(max(b.h, 1.0), size))

    def text_bbox(self, nid: str) -> tuple | None:
        frs = self.para_frags.get(nid)
        if not frs:
            return None
        return (frs[0].page, min(f.x for f in frs), min(f.y for f in frs), max(f.x1 for f in frs), max(f.y1 for f in frs))

    def container_box(self, nid: str):
        o = self.owner(nid)
        return self.boxes.get(o) if o else self.para_box.get(nid)

    def image(self, bin_id: str):
        if bin_id not in self._img:
            import cv2
            data = self.pkg.bin_data(bin_id)
            arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE) if data else None
            self._img[bin_id] = arr
        return self._img[bin_id]

    def ink_ratio(self, page: int, rect: tuple) -> float:
        """Fraction of dark image pixels under ``rect`` (only pictures count; native ink is checked via fragments)."""
        worst = 0.0
        for b in self.layout.boxes:
            if b.kind != "pic" or b.page != page or not b.extra.get("bin") or b.w <= 0 or b.h <= 0:
                continue
            if _inter((b.x, b.y, b.x1, b.y1), rect) <= 0:
                continue
            img = self.image(b.extra["bin"])
            if img is None:
                continue
            h, w = img.shape[:2]
            x0 = int(max(0, (rect[0] - b.x) / b.w * w))
            x1 = int(min(w, (rect[2] - b.x) / b.w * w))
            y0 = int(max(0, (rect[1] - b.y) / b.h * h))
            y1 = int(min(h, (rect[3] - b.y) / b.h * h))
            if x1 > x0 and y1 > y0:
                worst = max(worst, float((img[y0:y1, x0:x1] < 128).mean()))
        return worst

    def rule_along(self, page: int, x0: float, x1: float, y: float, tol: float = 3.0, cover: float = 0.8) -> bool:
        """Is there a horizontal rule (native line/border, or dark pixels in a page image) along y over [x0, x1]?"""
        segs = []
        for r in self.layout.rules:
            if r.page == page and abs(r.y0 - r.y1) < 0.6 and abs(r.y0 - y) <= tol:
                segs.append((min(r.x0, r.x1), max(r.x0, r.x1)))
        segs.sort()
        covered, cur = 0.0, x0
        for a, b in segs:
            a, b = max(a, cur), min(b, x1)
            if b > a:
                covered += b - a
                cur = b
        if x1 > x0 and covered / (x1 - x0) >= cover:
            return True
        for b in self.layout.boxes:
            if b.kind == "pic" and b.page == page and b.extra.get("bin") and b.w > 0 and b.h > 0 and b.y <= y <= b.y1:
                img = self.image(b.extra["bin"])
                if img is None:
                    continue
                h, w = img.shape[:2]
                px0, px1 = int(max(0, (x0 - b.x) / b.w * w)), int(min(w, (x1 - b.x) / b.w * w))
                py0, py1 = int(max(0, (y - tol - b.y) / b.h * h)), int(min(h, (y + tol - b.y) / b.h * h) + 1)
                if px1 > px0 and py1 > py0:
                    cols = (img[py0:py1, px0:px1] < 128).any(axis=0)
                    if cols.mean() >= cover:
                        return True
        return False


@dataclass
class Site:
    para: str
    span: tuple[int, int] | None = None      # raw char range for inline sites
    container: str | None = None             # owner nid (cell / text box) or None


@dataclass
class GoalReport:
    ok: bool
    targets: list[dict] = field(default_factory=list)
    protected: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)

    @property
    def targets_ok(self) -> bool:
        return all(t["ok"] for t in self.targets)

    def to_json(self) -> dict:
        return {"ok": self.ok, "targets": self.targets, "protected": self.protected, "violations": self.violations}


class Verifier:
    def __init__(self, contract: dict, d0: DocView):
        self.c = contract
        self.fields = {f["field_id"]: f for f in contract["fields"]}
        self.groups = {g["group_id"]: g for g in contract.get("repeat_groups", [])}
        self.d0 = d0
        self.ptypes = {p["type"] for p in contract["protected_predicates"]}
        self.target_fields = [p["field_id"] for p in contract["target_predicates"] if "field_id" in p]
        self.by_group_cell = {(f["repeat"]["group"], f["repeat"]["row"], f["repeat"]["col"]): f for f in contract["fields"] if f.get("repeat")}
        # --- protected baselines from the document's own blank state
        site_paras, inline_paras = set(), set()
        for f in contract["fields"]:
            for s in self.sites(d0, f):
                (inline_paras if s.span is not None else site_paras).add(s.para)
        self.p_texts = Counter(d0.ntext[n] for n in d0.ntext if n not in site_paras and n not in inline_paras and d0.ntext[n])
        self.p_inline = {n: d0.ntext[n] for n in inline_paras}
        self.p_merges = self._merge_sig(d0.ir)
        self.p_objects = self._object_sig(d0.ir)
        self.p_marks = self._mark_count(d0.ir)

    # ------------------------------------------------------------------ signatures
    @staticmethod
    def _merge_sig(ir: DocIR) -> tuple[Counter, int]:
        cols = Counter()
        rs = 0
        for t in ir.tables():
            for c in t.cells:
                if c.colspan > 1:
                    cols[(t.n_cols, c.col, c.colspan)] += 1
                if c.rowspan > 1:
                    rs += 1
        return cols, rs

    @staticmethod
    def _object_sig(ir: DocIR) -> Counter:
        sig = Counter()
        for s in ir.shapes():
            if s.kind == "pic":
                sig[("pic", s.bin_ref)] += 1
            elif not s.is_textbox:
                sig[(s.kind,)] += 1
        sig[("tables",)] = len(ir.tables())
        return sig

    @staticmethod
    def _mark_count(ir: DocIR) -> dict:
        shapes = ir.shapes()
        return {"bordered_textboxes": sum(1 for s in shapes if s.is_textbox and s.line.visible),
                "lines": sum(1 for s in shapes if s.kind in ("line", "connectLine", "polygon", "curve", "rect") and not s.is_textbox),
                "tables": len(ir.tables()), "cells": sum(len(t.cells) for t in ir.tables())}

    # ------------------------------------------------------------------ region resolution
    def find_anchor(self, view: DocView, a: dict | None):
        if not a or not norm(a.get("text", "")):
            return None
        target = norm(a["text"])
        cands = [n for n in view.order if view.ntext.get(n) == target]
        if not cands:
            return None
        ax, ay = a["bbox"][0], a["bbox"][1]
        if len(cands) == a.get("n_occurrences", -1) and a.get("occurrence", 0) < len(cands):
            return cands[a["occurrence"]]

        def dist(n):
            tb = view.text_bbox(n) or view.strip(n)
            return (abs(tb[0] - a.get("page", 0)) * 2000) + abs(tb[1] - ax) + abs(tb[2] - ay)
        best = min(cands, key=dist)
        return best if dist(best) <= 200 else None       # implausibly far: treat the anchor as missing (absolute region)

    def resolve(self, view: DocView, f: dict) -> tuple[int, tuple]:
        """Field region in ``view`` coordinates: (page, (x0, y0, x1, y1))."""
        x0, y0, x1, y1 = f["region"]
        page = f["page"]
        rep = f.get("repeat") or {}
        a = f.get("anchor")
        n = self.find_anchor(view, a)
        if n is not None:
            cb = view.container_box(n) if (a.get("container_bbox") and view.owner(n)) else None
            if cb is not None and cb.kind == "cell":
                dx, dy, pg = cb.x - a["container_bbox"][0], cb.y - a["container_bbox"][1], cb.page
            else:
                tb = view.text_bbox(n) or view.strip(n)
                dx, dy, pg = tb[1] - a["bbox"][0], tb[2] - a["bbox"][1], tb[0]
            if abs(dx) < 1500 and abs(dy) < 3000:
                x0, x1, y0, y1 = x0 + dx, x1 + dx, y0 + dy, y1 + dy
                page = pg + (f["page"] - a.get("page", f["page"]))
        if rep.get("new_row") and rep.get("row", 0) > 0:
            prev = self.by_group_cell.get((rep["group"], rep["row"] - 1, rep["col"]))
            if prev is not None:
                ps = self.sites(view, prev)
                if ps:
                    cb = view.container_box(ps[0].para)
                    if cb is not None:
                        h = y1 - y0
                        gap = 0.0 if cb.kind == "cell" else 2.0
                        page, y0 = cb.page, cb.y1 + gap
                        y1 = y0 + h
        return page, (x0, y0, x1, y1)

    # ------------------------------------------------------------------ sites / text
    def sites(self, view: DocView, f: dict) -> list[Site]:
        key = (id(self.c), f["field_id"])
        hit = view._sites.get(key)
        if hit is None:
            hit = view._sites[key] = self._sites_uncached(view, f)
        return hit

    def _sites_uncached(self, view: DocView, f: dict) -> list[Site]:
        page, r = self.resolve(view, f)
        if f["mode"] == "inline":
            s = self._inline_site(view, f, page, r)
            if s is not None:
                return [s]
        out = []
        rw = r[2] - r[0]
        for n in view.order:
            st = view.strip(n)
            if st[0] != page or n in view.blocked:
                continue
            ow = min(st[3], r[2] + PAD) - max(st[1], r[0] - PAD)
            pw = max(st[3] - st[1], 0.1)
            yc = (st[2] + st[4]) / 2
            if ow >= 0.5 * min(pw, rw) and ow >= 0.6 * pw and r[1] - PAD <= yc <= r[3] + PAD:
                if view.para_box[n].extra.get("role") not in ("body", None):
                    continue
                out.append(Site(n, None, view.owner(n)))
        return out

    @staticmethod
    def match_pattern(raw: str, segs: list[str]) -> list[tuple[int, int]] | None:
        """Match a paragraph against the literal segments of its blank template (whitespace-insensitive).
        Returns the raw (start, end) of every gap between consecutive segments, or None if the literals are absent."""
        s, idx = _strip_cached(raw)
        pos, cuts = 0, []
        for k, seg in enumerate(segs):
            if k == 0:
                if seg and not s.startswith(seg):
                    return None
                at = 0
            elif k == len(segs) - 1:
                if seg:
                    at = s.rfind(seg)
                    if at < pos or at + len(seg) != len(s):
                        return None
                else:
                    at = len(s)
            else:
                at = s.find(seg, pos) if seg else pos
                if at < 0:
                    return None
            cuts.append((at, at + len(seg)))
            pos = at + len(seg)
        spans = []
        for (_, e0), (s1, _) in zip(cuts, cuts[1:]):
            a = (idx[e0 - 1] + 1) if e0 > 0 else 0
            b = idx[s1] if s1 < len(idx) else len(raw)
            if raw[a:b].strip():                       # filled span: do not count surrounding blanks as value
                while a < b and raw[a].isspace():
                    a += 1
                while b > a and raw[b - 1].isspace():
                    b -= 1
            spans.append((a, max(a, b)))
        return spans

    def _inline_site(self, view: DocView, f: dict, page: int, r: tuple) -> Site | None:
        """Inline fields follow their paragraph wherever reflow moves it: the paragraph is identified by the literal
        text of the blank template around the field (occurrence-indexed), not by absolute position."""
        segs = f.get("para_pattern")
        if not segs or not any(segs):
            return None
        slot = f.get("slot", 0)
        cands = []
        for n in view.order:
            if view.para_box[n].extra.get("role") not in ("body", None):
                continue
            spans = self.match_pattern(view.text[n], segs)
            if spans is not None and slot < len(spans):
                cands.append((n, spans[slot]))
        if not cands:
            return None
        a = f["anchor"]
        if len(cands) == a.get("n_occurrences", -1) and a.get("occurrence", 0) < len(cands):
            n, (x, y) = cands[a["occurrence"]]
        else:
            def dist(c):
                st = view.strip(c[0])
                return abs(st[0] - page) * 2000 + abs(st[2] - r[1]) + abs(st[1] - a["bbox"][0]) * 0.01
            n, (x, y) = min(cands, key=dist)
            if sum(len(sg) for sg in segs) < 2 and dist((n, None)) > 40:
                return None
        if f.get("gap_lead") is not None and not view.text[n][x:y].strip():
            x = y = x + min(int(f["gap_lead"]), y - x)        # insertion-point field: keep the surrounding blanks
        return Site(n, (x, y), view.owner(n))

    def field_text(self, view: DocView, f: dict, sites: list[Site] | None = None) -> str:
        sites = self.sites(view, f) if sites is None else sites
        parts = []
        for s in sites:
            t = view.text[s.para]
            parts.append(t[s.span[0]:s.span[1]] if s.span is not None else t)
        return "".join(parts)

    # ------------------------------------------------------------------ predicate checks
    def _value_frags(self, view: DocView, sites: list[Site]):
        for s in sites:
            for fr in view.para_frags.get(s.para, []):
                if s.span is None:
                    yield s, fr, (fr.x, fr.y, fr.x1, fr.y1)
                else:
                    a, b = max(s.span[0], fr.start), min(s.span[1], fr.start + len(fr.text))
                    if b > a:
                        yield s, fr, (fr.x + fr.xs[a - fr.start], fr.y, fr.x + fr.xs[b - fr.start], fr.y1)

    def _layout_violations(self, view: DocView, f: dict, sites: list[Site]) -> list[dict]:
        out = []
        site_paras = {s.para for s in sites}
        pw_ph = {i: sz for i, sz in enumerate(view.layout.pages)}
        for s, fr, rect in self._value_frags(view, sites):
            if fr.clipped:
                out.append({"code": "clipped_text", "field": f["field_id"], "container": s.container, "para": s.para})
                continue
            pw, ph = pw_ph.get(fr.page, (595, 842))
            if rect[0] < -1 or rect[1] < -1 or rect[2] > pw + 1 or rect[3] > ph + 1:
                out.append({"code": "out_of_page", "field": f["field_id"], "container": s.container, "para": s.para})
            if f["kind"] == "check":
                continue
            area = max((rect[2] - rect[0]) * (rect[3] - rect[1]), 0.01)
            for g in view.layout.frags:
                if g.page != fr.page or g.para in site_paras or not g.text.strip():
                    continue
                if _inter(rect, (g.x, g.y, g.x1, g.y1)) <= 0:
                    continue
                ov, garea = 0.0, 0.0                     # glyph-level: blanks inside a fragment are not ink
                for (a0, a1) in _ink_runs(g):
                    ov += _inter(rect, (g.x + a0, g.y, g.x + a1, g.y1))
                    garea += (a1 - a0) * g.h
                if ov > 0.2 * min(area, max(garea, 0.01)):
                    out.append({"code": "overlap_text", "field": f["field_id"], "container": s.container, "para": s.para,
                                "other": g.owner, "other_para": g.para, "amount": round(rect[3] - g.y + 1.0, 2)})
                    break
            inner = (rect[0] + 1.0, rect[1] + 1.5, rect[2] - 1.0, rect[3] - 1.5)
            for r in view.layout.rules:
                if r.page != fr.page or r.owner == s.container or r.kind == "note_line":
                    continue
                rx0, rx1, ry0, ry1 = min(r.x0, r.x1), max(r.x0, r.x1), min(r.y0, r.y1), max(r.y0, r.y1)
                if rx1 > inner[0] and rx0 < inner[2] and ry1 > inner[1] and ry0 < inner[3] and inner[2] > inner[0] and inner[3] > inner[1]:
                    out.append({"code": "overlap_rule", "field": f["field_id"], "container": s.container, "para": s.para, "other": r.owner,
                                "amount": round(rect[3] - ry0 + 1.0, 2)})
                    break
            if view.ink_ratio(fr.page, rect) > INK_THRESHOLD:
                out.append({"code": "overlap_ink", "field": f["field_id"], "container": s.container, "para": s.para})
        uniq, seen = [], set()
        for v in out:
            k = (v["code"], v["field"], v.get("container"), v.get("other"), v.get("other_para"))
            if k not in seen:
                seen.add(k)
                uniq.append(v)
        return uniq

    def row_present(self, view: DocView, g: dict, row: int) -> tuple[bool, str]:
        for col in range(len(g["columns"])):
            f = self.by_group_cell.get((g["group_id"], row, col))
            if f is None:
                return False, f"contract has no field for row {row} col {col}"
            ss = self.sites(view, f)
            if not ss:
                return False, f"no text container at row {row} col {col}"
            if g.get("bordered"):
                page, r = self.resolve(view, f)
                cb = view.container_box(ss[0].para)
                ys = [r[3]] + ([cb.y1] if cb is not None else [])
                if not any(view.rule_along(page, r[0] + 2, r[2] - 2, y) for y in ys):
                    return False, f"row {row} col {col} lacks a bottom rule"
        return True, ""

    def check_target(self, view: DocView, p: dict) -> tuple[dict, list[dict]]:
        t = p["type"]
        if t == "table_row_count_at_least":
            g = self.groups[p["table_id"]]
            for row in range(int(p["value"])):
                ok, why = self.row_present(view, g, row)
                if not ok:
                    return {"type": t, "table_id": p["table_id"], "ok": False, "reason": why, "missing_row": row}, []
            return {"type": t, "table_id": p["table_id"], "ok": True}, []
        f = self.fields[p["field_id"]]
        res = {"type": t, "field_id": f["field_id"], "ok": False}
        if t == "image_in_region":
            page, r = self.resolve(view, f)
            base = self.p_objects
            for b in view.layout.boxes:
                if b.kind == "pic" and b.page == page and (("pic", b.extra.get("bin")) not in base):
                    cx, cy = b.x + b.w / 2, b.y + b.h / 2
                    if r[0] - PAD <= cx <= r[2] + PAD and r[1] - PAD <= cy <= r[3] + PAD and b.w <= (r[2] - r[0]) * 1.6 and b.h <= (r[3] - r[1]) * 1.6:
                        res["ok"] = True
                        return res, []
            res["reason"] = "no new picture in region"
            return res, []
        sites = self.sites(view, f)
        res["n_sites"] = len(sites)
        if t == "field_checked":
            page, r = self.resolve(view, f)
            for b in view.layout.boxes:
                if b.kind == "form" and b.page == page and b.extra.get("form") in ("checkBtn", "radioBtn") and \
                        _inter((b.x, b.y, b.x1, b.y1), (r[0] - PAD, r[1] - PAD, r[2] + PAD, r[3] + PAD)) > 0:
                    res["ok"] = (b.extra.get("value") == "CHECKED") == bool(p["value"])
                    res["got"] = b.extra.get("value")
                    return res, []
            got = norm(self.field_text(view, f, sites))
            res["got"] = got
            res["ok"] = (got in [norm(g) for g in CHECKED_GLYPHS]) == bool(p["value"]) and (bool(sites) or not p["value"])
            if not res["ok"]:
                res["reason"] = "check state differs"
                return res, []
            return res, self._layout_violations(view, f, sites)
        got = self.field_text(view, f, sites)
        res["got"] = got if len(got) <= 80 else got[:77] + "..."
        if not sites:
            res["reason"] = "no text container in field region"
            return res, []
        if norm(got) != norm(p["value"]):
            res["reason"] = "text differs"
            return res, []
        res["ok"] = True
        return res, self._layout_violations(view, f, sites)

    def check_protected(self, view: DocView) -> list[dict]:
        out = []
        if "unchanged_text" in self.ptypes:
            site_paras, inline = set(), {}
            for f in self.c["fields"]:
                for s in self.sites(view, f):
                    if s.span is not None:
                        inline.setdefault(s.para, []).append(s.span)
                    else:
                        site_paras.add(s.para)
            now = Counter(view.ntext[n] for n in view.ntext if n not in site_paras and n not in inline and view.ntext[n])
            for n, spans in inline.items():        # inline paragraphs: text outside the field spans is protected
                raw = view.text[n]
                keep, last = [], 0
                for a, b in sorted(spans):
                    keep.append(raw[last:a])
                    last = b
                keep.append(raw[last:])
                now[("inline", norm("".join(keep)))] += 1
            missing = self.p_texts - now
            # protected text of inline paragraphs in D0 (field spans removed) must still be present
            for n in self.p_inline:
                spans0 = [s.span for f in self.c["fields"] for s in self.sites(self.d0, f) if s.span is not None and s.para == n]
                raw, keep, last = self.d0.text[n], [], 0
                for a, b in sorted(spans0):
                    keep.append(raw[last:a])
                    last = b
                keep.append(raw[last:])
                key = ("inline", norm("".join(keep)))
                if now[key] > 0:
                    now[key] -= 1
                elif key[1]:
                    missing[key[1]] += 1
            total = sum(self.p_texts.values()) + len(self.p_inline)
            lost = sum(missing.values())
            out.append({"type": "unchanged_text", "ok": lost == 0, "lost": lost, "total": total,
                        "examples": [str(k)[:40] for k in list(missing)[:3]]})
        if "preserve_table_merges" in self.ptypes:
            cols, rs = self._merge_sig(view.ir)
            lost = sum((self.p_merges[0] - cols).values()) + max(0, self.p_merges[1] - rs)
            out.append({"type": "preserve_table_merges", "ok": lost == 0, "lost": lost, "total": sum(self.p_merges[0].values()) + self.p_merges[1]})
        if "preserve_objects" in self.ptypes:
            lost = sum((self.p_objects - self._object_sig(view.ir)).values())
            out.append({"type": "preserve_objects", "ok": lost == 0, "lost": lost, "total": sum(self.p_objects.values())})
        if "no_extraneous_marks" in self.ptypes:
            m = self._mark_count(view.ir)
            new_row_cells = sum(1 for p in self.c["target_predicates"] if p.get("field_id") and (self.fields[p["field_id"]].get("repeat") or {}).get("new_row")
                                and self.groups[self.fields[p["field_id"]]["repeat"]["group"]].get("bordered"))
            extra_boxes = m["bordered_textboxes"] - self.p_marks["bordered_textboxes"]
            extra_cells = m["cells"] - self.p_marks["cells"]
            extra_lines = m["lines"] - self.p_marks["lines"]
            bad = extra_boxes > new_row_cells or (new_row_cells == 0 and (extra_cells > 0 or extra_lines > 0))
            out.append({"type": "no_extraneous_marks", "ok": not bad, "extra_bordered_textboxes": extra_boxes, "extra_cells": extra_cells,
                        "extra_lines": extra_lines, "allowed_new_bordered": new_row_cells})
        return out

    def check(self, view: DocView, pred_idx: list[int] | None = None, protected: bool = True, file_ok: bool | None = None) -> GoalReport:
        targets, violations = [], []
        preds = self.c["target_predicates"]
        for i in (range(len(preds)) if pred_idx is None else pred_idx):
            res, vio = self.check_target(view, preds[i])
            res["index"] = i
            targets.append(res)
            violations.extend(vio)
        prot = self.check_protected(view) if protected else []
        if "no_overlap" in self.ptypes or not self.ptypes:
            prot.append({"type": "no_overlap", "ok": not violations, "violations": len(violations)})
        if file_ok is not None and "file_valid" in self.ptypes:
            prot.append({"type": "file_valid", "ok": bool(file_ok)})
        ok = all(t["ok"] for t in targets) and all(p["ok"] for p in prot)
        return GoalReport(ok, targets, prot, violations)


def preservation_score(report: GoalReport) -> dict:
    """non_target_preservation: share of protected units kept (per predicate and pooled)."""
    kept = total = 0
    per = {}
    for p in report.protected:
        if "total" in p:
            t, lost = p["total"], p.get("lost", 0)
            kept += t - min(lost, t)
            total += t
            per[p["type"]] = 1.0 if t == 0 else (t - min(lost, t)) / t
        else:
            per[p["type"]] = 1.0 if p["ok"] else 0.0
    return {"pooled": 1.0 if total == 0 else kept / total, "by_predicate": per, "all_ok": all(p["ok"] for p in report.protected)}


__all__ = ["DocView", "Verifier", "GoalReport", "Site", "norm", "preservation_score", "Cell", "Shape", "Table"]
