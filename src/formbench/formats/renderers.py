"""Renderer plugins. Different renderers are never assumed to produce the same layout (AGENTS.md §5).

* ``internal``    – reference renderer: formbench layout engine + PyMuPDF drawing (vector PDF, PNG).
* ``libreoffice`` – LibreOffice headless + H2Orestart HWP/HWPX import filter (independent cross-check).
* ``hancom``      – Hancom Office automation (Windows COM); ``blocked_by_environment`` elsewhere.
"""
from __future__ import annotations

import glob
import io
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pymupdf

from ..document_ir import fonts
from ..document_ir.layout import Layout, layout_document
from ..document_ir.normalize import build_ir
from ..util import Status, run_cmd
from .hwpx import HwpxPackage

BASELINE = 0.86


def _rgb(c: str | None) -> tuple | None:
    if not c or not c.startswith("#") or len(c) < 7:
        return (0, 0, 0) if c else None
    try:
        return tuple(int(c[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    except ValueError:
        return (0, 0, 0)


def _dashes(style: str) -> str | None:
    return {"DASH": "[3 2] 0", "DOT": "[1 1.5] 0", "DASH_DOT": "[3 1.5 1 1.5] 0", "DASH_DOT_DOT": "[3 1.5 1 1.5 1 1.5] 0",
            "LONG_DASH": "[6 2] 0"}.get(style)


def draw_layout(layout: Layout, pkg: HwpxPackage | None) -> pymupdf.Document:
    doc = pymupdf.open()
    for pno, (w, h) in enumerate(layout.pages):
        page = doc.new_page(width=w, height=h)
        boxes = [b for b in layout.boxes if b.page == pno]
        sh = page.new_shape()
        for b in boxes:
            if b.fill and b.kind in ("cell", "textbox", "shape") and b.w > 0 and b.h > 0:
                sh.draw_rect(pymupdf.Rect(b.x, b.y, b.x1, b.y1))
                sh.finish(fill=_rgb(b.fill), color=None, width=0)
        sh.commit()
        for b in sorted((b for b in boxes if b.kind == "pic"), key=lambda b: b.extra.get("z", 0)):
            data = pkg.bin_data(b.extra.get("bin")) if (pkg is not None and b.extra.get("bin")) else None
            rect = pymupdf.Rect(b.x, b.y, b.x1, b.y1)
            drawn = False
            if data and rect.width > 1 and rect.height > 1:
                try:
                    page.insert_image(rect, stream=data, keep_proportion=False)
                    drawn = True
                except Exception:      # noqa: BLE001 - unsupported image codec (wmf/emf/ole): placeholder below
                    drawn = False
            if not drawn:
                page.draw_rect(rect, color=(0.6, 0.6, 0.6), width=0.5, dashes="[2 2] 0")
        sh = page.new_shape()
        for b in boxes:
            if b.kind == "shape" and b.extra.get("shape") in ("ellipse", "arc") and b.w > 0 and b.h > 0:
                sh.draw_oval(pymupdf.Rect(b.x, b.y, b.x1, b.y1))
                sh.finish(color=(0, 0, 0), width=0.5, fill=_rgb(b.fill) if b.fill else None)
            elif b.kind == "shape" and b.extra.get("placeholder") and b.w > 0 and b.h > 0:
                sh.draw_rect(pymupdf.Rect(b.x, b.y, b.x1, b.y1))
                sh.finish(color=(0.6, 0.6, 0.6), width=0.5, dashes="[2 2] 0")
            elif b.kind == "form":
                if b.extra.get("form") in ("checkBtn", "radioBtn"):
                    mx, my, side = b.extra["mark"]
                    r = pymupdf.Rect(mx, my, mx + side, my + side)
                    (sh.draw_oval if b.extra["form"] == "radioBtn" else sh.draw_rect)(r)
                    sh.finish(color=(0, 0, 0), width=0.6)
                    if b.extra.get("value") == "CHECKED":
                        sh.draw_line((mx + 1.5, my + side / 2), (mx + side / 2.3, my + side - 1.8))
                        sh.draw_line((mx + side / 2.3, my + side - 1.8), (mx + side - 1.2, my + 1.5))
                        sh.finish(color=(0, 0, 0), width=1.1)
                elif b.w > 0 and b.h > 0:
                    sh.draw_rect(pymupdf.Rect(b.x, b.y, b.x1, b.y1))
                    sh.finish(color=(0.3, 0.3, 0.3), width=0.5)
        for r in layout.rules:
            if r.page != pno or (abs(r.x1 - r.x0) < 0.01 and abs(r.y1 - r.y0) < 0.01):
                continue
            sh.draw_line((r.x0, r.y0), (r.x1, r.y1))
            sh.finish(color=_rgb(r.color) or (0, 0, 0), width=max(r.width, 0.2), dashes=_dashes(r.style),
                      closePath=False)
        sh.commit()
        writers: dict[str, pymupdf.TextWriter] = {}
        for f in layout.frags:
            if f.page != pno or f.clipped or not f.text.strip():
                continue
            tw = writers.setdefault(f.color.upper(), pymupdf.TextWriter(page.rect, color=_rgb(f.color)))
            font = fonts.font_obj(f.font)
            base = f.y + f.size * BASELINE
            nosp = f.text.replace(" ", "")
            natural = sum(font.glyph_advance(ord(c)) for c in f.text) * f.size if " " not in f.text else -1.0
            glyph_w = sum(font.glyph_advance(ord(c)) for c in nosp) * f.size + 0.5 * f.size * (len(f.text) - len(nosp))
            per_char = abs(glyph_w - f.w) > 0.05          # condensed/expanded or letter-spaced text
            try:
                if abs(natural - f.w) < 0.05:
                    tw.append((f.x, base), f.text, font=font, fontsize=f.size)
                elif per_char and glyph_w > 0:
                    # condensed / expanded text: natural advances inside a horizontally scaled writer
                    sx = f.w / glyph_w
                    tws = pymupdf.TextWriter(page.rect, color=_rgb(f.color))
                    i, n = 0, len(f.text)
                    while i < n:
                        j = i
                        while j < n and f.text[j] != " ":
                            j += 1
                        while j < n and f.text[j] == " ":
                            j += 1
                        tws.append((f.x + f.xs[i] / sx, base), f.text[i:j], font=font, fontsize=f.size)
                        i = j
                    tws.write_text(page, morph=(pymupdf.Point(f.x, base), pymupdf.Matrix(sx, 1)))
                else:
                    # word chunks (each keeps its trailing spaces so extracted text has real spaces) at exact x
                    i, n = 0, len(f.text)
                    while i < n:
                        j = i
                        while j < n and f.text[j] != " ":
                            j += 1
                        while j < n and f.text[j] == " ":
                            j += 1
                        tw.append((f.x + f.xs[i], base), f.text[i:j], font=font, fontsize=f.size)
                        i = j
            except Exception:      # noqa: BLE001 - glyph outside font despite cmap check
                continue
            if f.underline:
                page.draw_line((f.x, base + 1.2), (f.x + f.w, base + 1.2), color=_rgb(f.color), width=0.5)
        for tw in writers.values():
            tw.write_text(page)
    return doc


def pdf_bytes(doc: pymupdf.Document) -> bytes:
    try:
        doc.subset_fonts()
    except Exception:      # noqa: BLE001 - fontTools missing: keep full fonts
        pass
    doc.set_metadata({})
    return doc.tobytes(deflate=True, garbage=3, no_new_id=True)


def pdf_to_pngs(pdf: bytes, dpi: int = 150) -> list[bytes]:
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return [p.get_pixmap(dpi=dpi, alpha=False).tobytes("png") for p in d]


def png_to_array(png: bytes) -> np.ndarray:
    import cv2
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return arr


def degrade_image(png: bytes, skew_deg: float = 0.0, jpeg_quality: int | None = None, noise_sigma: float = 0.0,
                  blur: float = 0.0, scale: float = 1.0, seed: int = 0, out_format: str = "png") -> bytes:
    """Deterministic scan simulation: rescale, skew, blur, sensor noise, JPEG artefacts."""
    import cv2
    img = png_to_array(png)
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    if skew_deg:
        h, w = img.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), skew_deg, 1.0)
        img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if noise_sigma:
        rng = np.random.default_rng(seed)
        img = np.clip(img.astype(np.float32) + rng.normal(0, noise_sigma, img.shape), 0, 255).astype(np.uint8)
    if jpeg_quality is not None or out_format == "jpeg":
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality or 90)])
        if out_format == "jpeg":
            return enc.tobytes()
        img = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    ok, enc = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    return enc.tobytes()


def images_to_pdf(images: list[bytes], page_sizes: list[tuple[float, float]]) -> bytes:
    """Image-only ("scanned") PDF: no text layer, no vector lines."""
    doc = pymupdf.open()
    for img, (w, h) in zip(images, page_sizes):
        page = doc.new_page(width=w, height=h)
        page.insert_image(page.rect, stream=img, keep_proportion=False)
    doc.set_metadata({})
    return doc.tobytes(deflate=True, garbage=3, no_new_id=True)


class RenderResult:
    def __init__(self, renderer: str, status: str, pdf: bytes | None = None, layout: Layout | None = None, reason: str = "",
                 info: dict | None = None):
        self.renderer, self.status, self.pdf, self.layout, self.reason = renderer, status, pdf, layout, reason
        self.info = info or {}

    @property
    def ok(self) -> bool:
        return self.status == Status.OK and self.pdf is not None


class InternalRenderer:
    name = "internal"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def version(self) -> dict:
        return {"renderer": "formbench-internal", "layout_engine": "1.0", "pymupdf": pymupdf.__doc__.split(":")[0].strip(),
                "fonts": fonts.environment()}

    def render(self, pkg: HwpxPackage) -> RenderResult:
        ir = build_ir(pkg)
        lay = layout_document(ir)
        pdf = pdf_bytes(draw_layout(lay, pkg))
        return RenderResult(self.name, Status.OK, pdf, lay, info={"pages": lay.n_pages, "font_substitution": fonts.substitution_report(list(lay.font_faces)),
                                                                   "warnings": lay.warnings})


def find_soffice() -> str | None:
    cands = [os.environ.get("FORMBENCH_SOFFICE")] + sorted(glob.glob(os.path.expanduser("~/opt/lo/root/opt/libreoffice*/program/soffice")), reverse=True) \
        + [shutil.which("soffice"), shutil.which("libreoffice")]
    return next((c for c in cands if c and os.path.exists(c)), None)


class LibreOfficeRenderer:
    """Independent renderer. Needs LibreOffice plus the H2Orestart extension for HWP/HWPX import."""
    name = "libreoffice"

    def available(self) -> tuple[bool, str]:
        so = find_soffice()
        if not so:
            return False, "soffice not found (set FORMBENCH_SOFFICE or install LibreOffice)"
        return True, so

    def version(self) -> dict:
        so = find_soffice()
        if not so:
            return {"renderer": "libreoffice", "status": Status.BLOCKED_BY_ENVIRONMENT}
        rc, out, _ = run_cmd([so, "--headless", "--version"], timeout=60)
        return {"renderer": "libreoffice", "version": out.strip(), "import_filter": "H2Orestart"}

    def convert(self, src: bytes, ext: str, to: str = "pdf", infilter: str | None = None, timeout: float = 180,
                isolated_profile: bool = False) -> tuple[bytes | None, str]:
        so = find_soffice()
        if not so:
            return None, "soffice not found"
        with tempfile.TemporaryDirectory(prefix="fb_lo_") as td:
            inp = Path(td) / f"in{ext}"
            inp.write_bytes(src)
            env = dict(os.environ)
            jh = os.path.expanduser("~/.sdkman/candidates/java/current")
            if "JAVA_HOME" not in env and os.path.isdir(jh):
                env["JAVA_HOME"] = jh
            cmd = [so, "--headless", "--norestore"]
            if isolated_profile:                       # concurrent conversions elsewhere must not share the profile lock
                cmd.append(f"-env:UserInstallation=file://{td}/profile")
            if infilter:
                cmd.append(f"--infilter={infilter}")
            cmd += ["--convert-to", to, "--outdir", td, str(inp)]
            rc, out, err = run_cmd(cmd, timeout=timeout, env=env)
            produced = [p for p in Path(td).iterdir() if p.is_file() and p.name != inp.name and p.suffix.lower() == "." + to.split(":")[0].lower()]
            if rc != 0 or not produced:
                return None, (err or out or f"rc={rc}").strip()[-300:]
            return produced[0].read_bytes(), ""

    def render(self, pkg_or_bytes, ext: str = ".hwpx") -> RenderResult:
        ok, why = self.available()
        if not ok:
            return RenderResult(self.name, Status.BLOCKED_BY_ENVIRONMENT, reason=why)
        data = pkg_or_bytes.to_bytes() if isinstance(pkg_or_bytes, HwpxPackage) else pkg_or_bytes
        pdf, err = self.convert(data, ext)
        if pdf is None:
            return RenderResult(self.name, Status.FAILED, reason=err)
        with pymupdf.open(stream=pdf, filetype="pdf") as d:
            n = d.page_count
        return RenderResult(self.name, Status.OK, pdf, None, info={"pages": n})


class HancomAutomationRenderer:
    """Hancom Office (HwpObject COM automation). Only possible on Windows with a licensed installation."""
    name = "hancom"

    @staticmethod
    def wsl_com() -> bool:
        """WSL with a Windows host where Hangul's COM server is registered (formats/native_render, hidden, read-only)."""
        import shutil
        import subprocess
        if os.name == "nt" or not shutil.which("powershell.exe") or not shutil.which("reg.exe"):
            return False
        r = subprocess.run(["reg.exe", "query", r"HKCR\HWPFrame.HwpObject"], capture_output=True)
        return r.returncode == 0

    def available(self) -> tuple[bool, str]:
        if os.name != "nt":
            if self.wsl_com():
                return True, ""
            return False, f"Hancom Office automation requires Windows + licensed Hancom Office (this host: {os.uname().sysname})"
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return False, "pywin32 not installed"
        return True, ""

    def version(self) -> dict:
        ok, why = self.available()
        return {"renderer": "hancom", "status": Status.OK if ok else Status.BLOCKED_BY_ENVIRONMENT, "reason": why}

    def render(self, pkg_or_bytes, ext: str = ".hwpx") -> RenderResult:  # pragma: no cover - needs Windows + Hancom Office
        ok, why = self.available()
        if not ok:
            return RenderResult(self.name, Status.BLOCKED_BY_ENVIRONMENT, reason=why)
        data = pkg_or_bytes.to_bytes() if isinstance(pkg_or_bytes, HwpxPackage) else pkg_or_bytes
        if os.name != "nt":                              # WSL: the Windows host's Hangul through powershell COM
            from .native_render import hwp_pdf
            r = hwp_pdf([(data, "doc")])["doc"]
            if r.get("status") != "ok":
                return RenderResult(self.name, Status.FAILED, reason=r.get("reason", ""))
            return RenderResult(self.name, Status.OK, pdf=r["pdf"], info={"renderer": r.get("renderer")})
        import win32com.client
        with tempfile.TemporaryDirectory() as td:
            src, dst = os.path.join(td, "in" + ext), os.path.join(td, "out.pdf")
            Path(src).write_bytes(data)
            hwp = win32com.client.gencache.EnsureDispatch("HWPFrame.HwpObject")
            try:
                hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
                hwp.Open(src, "HWPX" if ext == ".hwpx" else "HWP", "forceopen:true")
                hwp.SaveAs(dst, "PDF")
            finally:
                hwp.Quit()
            return RenderResult(self.name, Status.OK, Path(dst).read_bytes())


RENDERERS = {"internal": InternalRenderer, "libreoffice": LibreOfficeRenderer, "hancom": HancomAutomationRenderer}


def get_renderer(name: str):
    return RENDERERS[name]()


def render_png_pages(pkg: HwpxPackage, dpi: int = 100) -> tuple[list[bytes], Layout]:
    res = InternalRenderer().render(pkg)
    return pdf_to_pngs(res.pdf, dpi), res.layout


def image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.size
