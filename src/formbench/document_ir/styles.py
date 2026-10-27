"""Effective style resolution from header.xml (charPr / paraPr / borderFill / numbering / bullets / fonts)."""
from __future__ import annotations

import re

from ..formats.hwpx import HC, HH, HP, local
from .schema import Border, BorderFill, CharStyle, ParaStyle, hu

_MM_RX = re.compile(r"([\d.]+)\s*mm")


def border_width_pt(s: str | None) -> float:
    m = _MM_RX.search(s or "")
    return (float(m.group(1)) * 72 / 25.4) if m else 0.34


class StyleTable:
    def __init__(self, header_root):
        self.root = header_root
        self.fonts: dict[str, dict[str, str]] = {}
        for ff in header_root.iter(HH + "fontface"):
            self.fonts[ff.get("lang", "")] = {f.get("id"): f.get("face", "") for f in ff.iter(HH + "font")}
        self._char: dict[str, CharStyle] = {}
        self._para: dict[str, ParaStyle] = {}
        self._bf: dict[str, BorderFill] = {}
        self._char_el = {e.get("id"): e for e in header_root.iter(HH + "charPr")}
        self._para_el = {e.get("id"): e for e in header_root.iter(HH + "paraPr")}
        self._bf_el = {e.get("id"): e for e in header_root.iter(HH + "borderFill")}
        self.numberings = {e.get("id"): e for e in header_root.iter(HH + "numbering")}
        self.bullets = {e.get("id"): e for e in header_root.iter(HH + "bullet")}
        self.styles = {e.get("id"): e for e in header_root.iter(HH + "style")}

    def font_names(self) -> dict[str, list[str]]:
        return {lang: sorted(set(m.values())) for lang, m in self.fonts.items()}

    def char(self, ref: str | None) -> CharStyle:
        if ref in self._char:
            return self._char[ref]
        el = self._char_el.get(ref)
        cs = CharStyle(ref=ref)
        if el is not None:
            cs.size = hu(el.get("height"), 10.0)
            cs.color = el.get("textColor") or "#000000"
            if cs.color.lower() == "none":
                cs.color = "#000000"
            for ch in el:
                t = local(ch)
                if t == "fontRef":
                    cs.font = self.fonts.get("HANGUL", {}).get(ch.get("hangul"), cs.font)
                    cs.latin_font = self.fonts.get("LATIN", {}).get(ch.get("latin"), cs.font)
                elif t == "ratio":
                    cs.ratio = float(ch.get("hangul") or 100)
                elif t == "spacing":
                    cs.spacing = float(_signed(ch.get("hangul")))
                elif t == "relSz":
                    cs.size = cs.size * float(ch.get("hangul") or 100) / 100.0
                elif t == "bold":
                    cs.bold = True
                elif t == "italic":
                    cs.italic = True
                elif t == "underline":
                    cs.underline = (ch.get("type") or "NONE") != "NONE"
                elif t == "strikeout":
                    cs.strike = (ch.get("shape") or "NONE") not in ("NONE", "")
        self._char[ref] = cs
        return cs

    def para(self, ref: str | None) -> ParaStyle:
        if ref in self._para:
            return self._para[ref]
        el = self._para_el.get(ref)
        ps = ParaStyle(ref=ref)
        if el is not None:
            al = el.find(HH + "align")
            if al is not None:
                ps.align = al.get("horizontal") or "JUSTIFY"
            hd = el.find(HH + "heading")
            if hd is not None:
                ps.heading = hd.get("type") or "NONE"
                ps.heading_level = int(hd.get("level") or 0)
                ps.heading_id = hd.get("idRef") or "0"
            # margins / lineSpacing live either directly under paraPr or inside hp:switch/(hp:case|hp:default)
            margin = el.find(HH + "margin")
            lsp = el.find(HH + "lineSpacing")
            sw = el.find(HP + "switch")
            if sw is not None and (margin is None or lsp is None):
                branch = sw.find(HP + "default")
                if branch is None:
                    branch = sw.find(HP + "case")
                if branch is not None:
                    margin = margin if margin is not None else branch.find(HH + "margin")
                    lsp = lsp if lsp is not None else branch.find(HH + "lineSpacing")
            if margin is not None:
                def mv(name):
                    c = margin.find(HC + name)
                    if c is None:
                        return 0.0
                    v = hu(c.get("value"))
                    return v if (c.get("unit") or "HWPUNIT") == "HWPUNIT" else v * 100 * 5.0   # CHAR unit ~ half em of 10pt
                ps.indent, ps.left, ps.right, ps.prev, ps.next = mv("intent"), mv("left"), mv("right"), mv("prev"), mv("next")
            if lsp is not None:
                ps.ls_type = lsp.get("type") or "PERCENT"
                v = float(_signed(lsp.get("value")) or 160)
                ps.ls_value = v if ps.ls_type == "PERCENT" else v / 100.0
        self._para[ref] = ps
        return ps

    def border_fill(self, ref: str | None) -> BorderFill:
        if ref in self._bf:
            return self._bf[ref]
        el = self._bf_el.get(ref)
        bf = BorderFill()
        if el is not None:
            for side in ("left", "right", "top", "bottom"):
                b = el.find(HH + side + "Border")
                if b is not None:
                    setattr(bf, side, Border(b.get("type") or "NONE", border_width_pt(b.get("width")), b.get("color") or "#000000"))
            fb = el.find(HC + "fillBrush")
            if fb is not None:
                wb = fb.find(HC + "winBrush")
                if wb is not None and (wb.get("faceColor") or "none").lower() != "none":
                    bf.fill = wb.get("faceColor")
        self._bf[ref] = bf
        return bf

    # --- numbering / bullets
    def heading_text(self, ps: ParaStyle, counters: dict) -> str:
        if ps.heading == "BULLET":
            b = self.bullets.get(ps.heading_id)
            return ((b.get("char") if b is not None else None) or "•") + " "
        if ps.heading not in ("NUMBER", "OUTLINE"):
            return ""
        num = self.numberings.get(ps.heading_id)
        if num is None:
            num = next(iter(self.numberings.values()), None)
        if num is None:
            return ""
        lvl = ps.heading_level
        key = (ps.heading, ps.heading_id)
        cnt = counters.setdefault(key, [0] * 10)
        cnt[lvl] += 1
        for i in range(lvl + 1, 10):
            cnt[i] = 0
        heads = {int(h.get("level") or 1) - 1: h for h in num.iter(HH + "paraHead")}
        h = heads.get(lvl)
        if h is None or not (h.text or "").strip():
            return ""
        fmt = h.text

        def rep(m):
            k = int(m.group(1)) - 1
            hk = heads.get(k)
            return format_number(cnt[k] if k < 10 else 1, (hk.get("numFormat") if hk is not None else "DIGIT") or "DIGIT")
        return re.sub(r"\^(\d+)", rep, fmt) + " "


def _signed(v) -> int:
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return 0
    return x - 0x100000000 if x > 0x7FFFFFFF else x


_HANGUL_SYL = "가나다라마바사아자차카타파하"
_HANGUL_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"


def format_number(n: int, fmt: str) -> str:
    n = max(n, 1)
    if fmt == "CIRCLED_DIGIT" and n <= 20:
        return chr(0x2460 + n - 1)
    if fmt == "HANGUL_SYLLABLE":
        return _HANGUL_SYL[(n - 1) % 14]
    if fmt == "HANGUL_JAMO":
        return _HANGUL_JAMO[(n - 1) % 14]
    if fmt in ("LATIN_CAPITAL", "LATIN_SMALL"):
        c = chr(ord("A") + (n - 1) % 26)
        return c if fmt == "LATIN_CAPITAL" else c.lower()
    if fmt in ("ROMAN_CAPITAL", "ROMAN_SMALL"):
        vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
        out = ""
        for v, s in vals:
            while n >= v:
                out += s
                n -= v
        return out if fmt == "ROMAN_CAPITAL" else out.lower()
    return str(n)
