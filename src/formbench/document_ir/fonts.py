"""Font resolution and text measurement (shared by layout and the internal renderer).

The original Hancom fonts are usually not installed; substitutions are recorded and reported so that visual
comparisons across renderers/fonts are flagged (test #12)."""
from __future__ import annotations

import functools
import glob
import os

import pymupdf

SERIF_HINTS = ("바탕", "명조", "궁서", "batang", "myeongjo", "myungjo", "times", "serif", "신명", "song", "ming", "gungsuh")
_SEARCH_DIRS = [os.path.expanduser("~/.local/share/fonts"), "/usr/share/fonts", "/usr/local/share/fonts"]
_CANDIDATES = {
    "serif": ["NanumMyeongjo.ttf", "NotoSerifCJK-Regular.ttc", "NanumGothic.ttf"],
    "serif_bold": ["NanumMyeongjoBold.ttf", "NanumMyeongjo.ttf"],
    "sans": ["NanumGothic.ttf", "NotoSansCJK-Regular.ttc", "Pretendard-Regular.otf"],
    "sans_bold": ["NanumGothicBold.ttf", "Pretendard-Bold.otf", "NanumGothic.ttf"],
    "fallback1": ["DejaVuSans.ttf"],
    "fallback2": ["DroidSansFallbackFull.ttf", "DroidSansFallback.ttf"],
}


@functools.cache
def _find(name: str) -> str | None:
    for d in _SEARCH_DIRS:
        hits = glob.glob(os.path.join(d, "**", name), recursive=True)
        if hits:
            return sorted(hits)[0]
    return None


@functools.cache
def font_file(key: str) -> str | None:
    for cand in _CANDIDATES[key]:
        p = _find(cand)
        if p:
            return p
    return None


@functools.cache
def font_obj(key: str) -> pymupdf.Font:
    f = font_file(key)
    if f is None:
        return pymupdf.Font("cjk")          # built-in Droid fallback shipped with MuPDF
    return pymupdf.Font(fontfile=f)


@functools.lru_cache(maxsize=4096)
def classify(face: str, bold: bool) -> str:
    low = (face or "").lower()
    base = "serif" if any(h in low for h in SERIF_HINTS) else "sans"
    return base + ("_bold" if bold else "")


@functools.lru_cache(maxsize=200000)
def resolve_char(face: str, bold: bool, ch: str) -> tuple[str, float]:
    """Return (font key, advance at size 1) for one character, falling back when the glyph is missing."""
    cp = ord(ch)
    if ch == " ":
        # Hancom's Korean faces use a half-em space; substituted fonts are narrower, which breaks space-aligned forms
        return classify(face, bold), 0.5
    for key in (classify(face, bold), "sans", "fallback1", "fallback2"):
        f = font_obj(key)
        try:
            if f.has_glyph(cp):
                return key, f.glyph_advance(cp)
        except Exception:      # noqa: BLE001 - defensive: corrupt cmap in a system font
            continue
    return "sans", (1.0 if cp > 0x2E80 else 0.5)


def text_width(text: str, face: str, size: float, bold: bool = False, ratio: float = 100.0, spacing: float = 0.0) -> float:
    return sum(resolve_char(face, bold, c)[1] * size * ratio / 100.0 + size * spacing / 100.0 for c in text)


def substitution_report(faces: list[str]) -> dict:
    return {face: {"class": classify(face, False), "file": os.path.basename(font_file(classify(face, False)) or "builtin-cjk"),
                   "exact_match": False} for face in sorted(set(faces))}


def environment() -> dict:
    return {k: (os.path.basename(font_file(k)) if font_file(k) else None) for k in _CANDIDATES}
