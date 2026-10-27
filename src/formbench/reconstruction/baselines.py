"""Reconstruction baselines: PDF / page image -> blank HWPX. Every baseline is a pure function of ``ReconInput``.

B0  image_only            page raster as a full-page picture (visual-only control)
B0  image_overlay         B0 + an empty text box over every blank table cell found in the raster
B1  parse_flow            lines + text (vector or OCR) -> native flow paragraphs and native tables with merges
B1  parse_textbox         same analysis, layout-preserving output: line shapes + one text box per cell / text line
B2  vlm_generate          multimodal model emits a schema-checked structured document -> native HWPX
B3  intermediary_pdf2docx PDF -> DOCX (pdf2docx) -> HWPX (formbench DOCX converter)
B3  intermediary_libreoffice  PDF -> DOCX (LibreOffice writer_pdf_import) -> HWPX
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from ..formats.docx import docx_to_hwpx
from ..formats.hwpx_build import HwpxBuilder, make_line, make_p, make_pic, make_table, make_textbox
from ..formats.renderers import LibreOfficeRenderer, images_to_pdf
from ..util import Status, path
from .adapters import (
    PageAnalysis,
    ReconInput,
    analyze,
    assign_units,
    build_grids,
    cell_is_blank,
    group_lines,
    line_text,
)

BASELINE_VERSION = "1.0"


@dataclass
class ReconResult:
    baseline: str
    family: str
    status: str
    hwpx: bytes | None = None
    reason: str = ""
    info: dict = field(default_factory=dict)
    elapsed_s: float = 0.0


def _png_size(png: bytes) -> tuple[int, int]:
    import struct
    return struct.unpack(">II", png[16:24]) if png[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)


def _abs_builder(pa: PageAnalysis) -> HwpxBuilder:
    return HwpxBuilder(pa.width, pa.height, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))


def _page_anchor(b: HwpxBuilder, i: int, st: dict):
    return b.add_para("", st["tiny"], st["tight"], page_break=i > 0)


def _styles(b: HwpxBuilder) -> dict:
    return {"tiny": b.head.char_pr(size=1.0), "tight": b.head.para_pr(align="LEFT", line_spacing=100, left=0, right=0, indent=0, prev=0, next_=0)}


def _char(b: HwpxBuilder, size: float, bold: bool = False) -> str:
    return b.head.char_pr(size=max(4.0, round(size * 2) / 2), bold=bold)


# ----------------------------------------------------------------------------- B0
def image_only(inp: ReconInput, overlay: bool = False) -> tuple[bytes, dict]:
    pages = analyze(inp) if overlay else _raster_pages(inp)
    b = _abs_builder(pages[0])
    st = _styles(b)
    n_boxes = 0
    for i, pa in enumerate(pages):
        p = _page_anchor(b, i, st)
        bid = b.add_bin(pa.image, "png")
        b.attach(make_pic(b.next_id(), bid, 0.0, 0.0, pa.width, pa.height, px=_png_size(pa.image), wrap="BEHIND_TEXT"), p)
        if overlay:
            grids, _, _ = build_grids(pa)
            cache: dict = {}
            body, left = _char(b, 10.0), b.head.para_pr(align="LEFT", line_spacing=130, left=0, right=0, indent=0, prev=0, next_=0)
            for g in grids:
                for c in g.cells:
                    if c.x1 - c.x0 >= 25 and c.y1 - c.y0 >= 10 and cell_is_blank(pa, c.x0, c.y0, c.x1, c.y1, cache):
                        b.attach(make_textbox(b.next_id(), c.x0 + 1.5, c.y0 + 1.5, c.x1 - c.x0 - 3.0, c.y1 - c.y0 - 3.0, [make_p("", body, left)],
                                              border=False, margin=1.5, valign="CENTER", z=1), p)
                        n_boxes += 1
    return b.to_bytes(), {"pages": len(pages), "overlay_boxes": n_boxes}


def _raster_pages(inp: ReconInput) -> list[PageAnalysis]:
    """Page rasters without any analysis (B0 needs nothing else)."""
    import cv2
    import numpy as np
    import pymupdf
    out = []
    if inp.kind in ("vector_pdf", "scanned_pdf"):
        with pymupdf.open(stream=inp.pdf, filetype="pdf") as d:
            for page in d:
                out.append(PageAnalysis(page.rect.width, page.rect.height, page.get_pixmap(dpi=150, alpha=False).tobytes("png")))
    else:
        for img in inp.images:
            arr = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
            h, w = arr.shape[:2]
            out.append(PageAnalysis(w * 72.0 / inp.dpi, h * 72.0 / inp.dpi, cv2.imencode(".png", arr)[1].tobytes()))
    return out


def image_overlay(inp: ReconInput) -> tuple[bytes, dict]:
    return image_only(inp, overlay=True)


# ----------------------------------------------------------------------------- B1 helpers
def _cell_layout(c, margin: float = 5.1) -> tuple[list, str]:
    """Paragraph specs [(text, size, bold, align, indent, gap_before)] and vertical alignment for a grid cell."""
    lines = group_lines(c.units)
    if not lines:
        return [], "CENTER"
    specs = []
    w, h = c.x1 - c.x0, c.y1 - c.y0
    prev_bottom = None
    for ln in lines:
        x0, x1 = ln[0].x0, ln[-1].x1
        size = statistics.median(u.size for u in ln)
        centred = abs((x0 + x1) / 2 - (c.x0 + c.x1) / 2) < max(3.0, 0.06 * w) and (x1 - x0) < 0.85 * w and (x0 - c.x0) > margin + 2
        y0 = min(u.y0 for u in ln)
        gap = max(0.0, y0 - prev_bottom) if prev_bottom is not None else 0.0
        specs.append((line_text(ln), size, any(u.bold for u in ln), "CENTER" if centred else "LEFT", 0.0 if centred else max(0.0, x0 - c.x0 - margin), gap))
        prev_bottom = y0 + size
    top, bottom = min(u.y0 for ln in lines for u in ln), max(u.y1 for ln in lines for u in ln)
    mid = abs((top + bottom) / 2 - (c.y0 + c.y1) / 2) < max(2.5, 0.15 * h)
    return specs, ("CENTER" if mid else "TOP")


def _paras(b: HwpxBuilder, specs: list, default_size: float = 10.0) -> list:
    if not specs:
        return [make_p("", _char(b, default_size), b.head.para_pr(align="LEFT", line_spacing=100, left=0, right=0, indent=0, prev=0, next_=0))]
    out = []
    for text, size, bold, align, indent, gap in specs:
        out.append(make_p(text, _char(b, size, bold), b.head.para_pr(align=align, line_spacing=100, left=round(indent, 1), right=0, indent=0, prev=round(gap, 1), next_=0)))
    return out


def _body_size(pages: list[PageAnalysis]) -> float:
    sizes = [u.size for pa in pages for u in pa.units]
    return float(statistics.median(sizes)) if sizes else 10.0


# ----------------------------------------------------------------------------- B1 native flow
def parse_flow(inp: ReconInput) -> tuple[bytes, dict]:
    pages = analyze(inp)
    body = _body_size(pages)
    content = [(min([u.x0 for u in pa.units] + [h[0] for h in pa.hlines] + [pa.width * 0.5]), min([u.y0 for u in pa.units] + [h[1] for h in pa.hlines] + [pa.height * 0.5]))
               for pa in pages]
    left = max(8.0, min(c[0] for c in content) - 0.5)
    top = max(8.0, min(c[1] for c in content) - 0.5)
    pa0 = pages[0]
    b = HwpxBuilder(pa0.width, pa0.height, (left, 8.0, top, 8.0, 0.0, 0.0))
    info = {"pages": len(pages), "tables": 0, "cells": 0, "merged_cells": 0, "paragraphs": 0, "text_source": pages[0].text_source,
            "notes": sorted({n for pa in pages for n in pa.notes})}
    first = True
    for pa in pages:
        grids, rest_h, _rest_v = build_grids(pa)
        free = assign_units(grids, pa.units)
        blocks = [("table", g.bbox[1], g) for g in grids] + [("line", min(u.y0 for u in ln), ln) for ln in group_lines(free)]
        blocks.sort(key=lambda t: (t[1], 0 if t[0] == "table" else 1))
        y = top
        new_page = not first
        anchor = None
        for kind, y0, obj in blocks:
            gap = max(0.0, y0 - y)
            if kind == "line":
                size = statistics.median(u.size for u in obj)
                p = b.add_para(line_text(obj), _char(b, size, any(u.bold for u in obj)),
                               b.head.para_pr(align="LEFT", line_spacing=100, left=round(max(0.0, obj[0].x0 - left), 1), right=0, indent=0, prev=round(gap, 1), next_=0),
                               page_break=new_page)
                y = y0 + size
                info["paragraphs"] += 1
            else:
                g = obj
                cells = []
                for c in g.cells:
                    specs, valign = _cell_layout(c)
                    bf = b.head.border_fill(c.borders.get("left", True), c.borders.get("right", True), c.borders.get("top", True), c.borders.get("bottom", True))
                    cells.append({"row": c.row, "col": c.col, "rowspan": c.rowspan, "colspan": c.colspan, "paras": _paras(b, specs, body), "valign": valign, "bf": bf})
                    info["merged_cells"] += int(c.rowspan > 1 or c.colspan > 1)
                tbl = make_table(b.next_id(), [g.xs[i + 1] - g.xs[i] for i in range(g.n_cols)], [g.ys[i + 1] - g.ys[i] for i in range(g.n_rows)], cells,
                                 b.head.border_fill(), inline=True, char_id=_char(b, body))
                p = b.add_para("", _char(b, 1.0), b.head.para_pr(align="LEFT", line_spacing=100, left=round(max(0.0, g.bbox[0] - left), 1), right=0, indent=0,
                                                                  prev=round(gap, 1), next_=0), page_break=new_page)
                b.attach(tbl, p)
                y = g.bbox[3] + 1.0
                info["tables"] += 1
                info["cells"] += len(g.cells)
            anchor = anchor if anchor is not None else p
            new_page = False
            first = False
        if anchor is None:
            anchor = b.add_para("", _char(b, body), page_break=not first)
            first = False
        for x0, yy, x1 in rest_h:               # rules outside tables (underlines, separators): kept as line shapes
            if x1 - x0 >= 20:
                b.attach(make_line(b.next_id(), x0, yy, x1, yy), anchor)
    return b.to_bytes(), info


# ----------------------------------------------------------------------------- B1 layout-preserving text boxes
def parse_textbox(inp: ReconInput) -> tuple[bytes, dict]:
    pages = analyze(inp)
    body = _body_size(pages)
    b = _abs_builder(pages[0])
    st = _styles(b)
    info = {"pages": len(pages), "textboxes": 0, "lines": 0, "text_source": pages[0].text_source, "notes": sorted({n for pa in pages for n in pa.notes})}
    for i, pa in enumerate(pages):
        p = _page_anchor(b, i, st)
        grids, _, _ = build_grids(pa)
        free = assign_units(grids, pa.units)
        for x0, y, x1 in pa.hlines:
            b.attach(make_line(b.next_id(), x0, y, x1, y), p)
            info["lines"] += 1
        for x, y0, y1 in pa.vlines:
            b.attach(make_line(b.next_id(), x, y0, x, y1), p)
            info["lines"] += 1
        for g in grids:
            for c in g.cells:
                if c.x1 - c.x0 < 6 or c.y1 - c.y0 < 6:
                    continue
                specs, valign = _cell_layout(c, margin=2.0)
                b.attach(make_textbox(b.next_id(), c.x0 + 1.0, c.y0 + 1.0, c.x1 - c.x0 - 2.0, c.y1 - c.y0 - 2.0, _paras(b, specs, body), border=False, margin=1.0,
                                      valign=valign, z=1), p)
                info["textboxes"] += 1
        for ln in group_lines(free):
            size = statistics.median(u.size for u in ln)
            x0, y0, x1 = ln[0].x0, min(u.y0 for u in ln), ln[-1].x1
            para = make_p(line_text(ln), _char(b, size, any(u.bold for u in ln)), st["tight"])
            b.attach(make_textbox(b.next_id(), x0 - 0.5, y0 - 0.5, (x1 - x0) + size * 1.2 + 1.0, size + 2.0, [para], border=False, margin=0.5, z=1), p)
            info["textboxes"] += 1
    return b.to_bytes(), info


# ----------------------------------------------------------------------------- B2 multimodal generation
VLM_SCHEMA = {
    "type": "object", "required": ["blocks"], "additionalProperties": False,
    "properties": {"blocks": {"type": "array", "maxItems": 400, "items": {"oneOf": [
        {"type": "object", "required": ["type", "text"], "additionalProperties": False,
         "properties": {"type": {"const": "paragraph"}, "text": {"type": "string", "maxLength": 4000}, "align": {"enum": ["left", "center", "right"]},
                        "bold": {"type": "boolean"}, "size": {"type": "number", "minimum": 5, "maximum": 40}}},
        {"type": "object", "required": ["type", "rows"], "additionalProperties": False,
         "properties": {"type": {"const": "table"}, "rows": {"type": "array", "minItems": 1, "maxItems": 120, "items": {
             "type": "array", "maxItems": 40, "items": {"type": "object", "required": ["text"], "additionalProperties": False, "properties": {
                 "text": {"type": "string", "maxLength": 2000}, "rowspan": {"type": "integer", "minimum": 1, "maximum": 60},
                 "colspan": {"type": "integer", "minimum": 1, "maximum": 40}}}}}}}]}}},
}
VLM_PROMPT = (
    "You are converting a scanned form page into an editable document. Read the page image and output ONLY a JSON object "
    "{\"blocks\": [...]} in reading order. Block types: {\"type\":\"paragraph\",\"text\":str,\"align\":\"left|center|right\",\"bold\":bool,\"size\":pt} and "
    "{\"type\":\"table\",\"rows\":[[{\"text\":str,\"rowspan\":int,\"colspan\":int}, ...], ...]}. Keep every blank cell as a cell with empty text. "
    "Use rowspan/colspan for merged cells and list each merged cell only once (in its top-left position). Preserve the original language and characters. "
    "Do not add explanations.")


class VlmBackend:
    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, "no multimodal backend configured"

    def generate(self, image_png: bytes) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def version(self) -> dict:
        return {"backend": self.name}


class LocalHfVlm(VlmBackend):
    """Local open-weights vision-language model through transformers (deterministic greedy decoding)."""
    name = "hf_local"
    _cache: dict = {}

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.environ.get("FORMBENCH_VLM_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")

    def available(self):
        try:
            import torch
            import transformers  # noqa: F401
        except ImportError as e:
            return False, f"transformers/torch not installed ({e.name})"
        if not torch.cuda.is_available():
            return False, "no CUDA device for the local vision-language model"
        return True, ""

    def version(self):
        try:
            import torch
            import transformers
            return {"backend": self.name, "model": self.model_id, "transformers": transformers.__version__, "torch": torch.__version__, "decoding": "greedy"}
        except ImportError:
            return {"backend": self.name, "status": Status.BLOCKED_BY_ENVIRONMENT}

    def _load(self):
        if self.model_id not in self._cache:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor
            proc = AutoProcessor.from_pretrained(self.model_id, max_pixels=1280 * 28 * 28)
            model = AutoModelForImageTextToText.from_pretrained(self.model_id, torch_dtype=torch.float16, device_map="cuda")
            model.eval()
            self._cache[self.model_id] = (proc, model)
        return self._cache[self.model_id]

    def generate(self, image_png: bytes) -> str:
        import io

        import torch
        from PIL import Image
        proc, model = self._load()
        img = Image.open(io.BytesIO(image_png)).convert("RGB")
        messages = [{"role": "user", "content": [{"type": "image", "image": img}, {"type": "text", "text": VLM_PROMPT}]}]
        text = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = proc(text=[text], images=[img], return_tensors="pt").to("cuda")
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=2048, do_sample=False, num_beams=1, repetition_penalty=1.1)
        return proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]


class ClaudeCliVlm(VlmBackend):
    """Hosted multimodal model through the ``claude`` CLI (opt-in: FORMBENCH_VLM_BACKEND=claude_cli). Page images of
    public blank forms are sent to an external service, so this backend is never used implicitly."""
    name = "claude_cli"

    def available(self):
        import shutil
        if os.environ.get("FORMBENCH_VLM_BACKEND") != "claude_cli":
            return False, "external model not enabled (set FORMBENCH_VLM_BACKEND=claude_cli to opt in)"
        return (shutil.which("claude") is not None, "claude CLI not found")

    def generate(self, image_png: bytes) -> str:
        with tempfile.TemporaryDirectory(prefix="fb_vlm_") as td:
            img = Path(td) / "page.png"
            img.write_bytes(image_png)
            p = subprocess.run(["claude", "-p", f"Read the image file {img} and follow these instructions. {VLM_PROMPT}", "--allowedTools", "Read"],
                               capture_output=True, timeout=600, cwd=td)
            return p.stdout.decode("utf-8", "replace")


def get_vlm_backend() -> VlmBackend:
    pref = os.environ.get("FORMBENCH_VLM_BACKEND", "hf_local")
    return ClaudeCliVlm() if pref == "claude_cli" else LocalHfVlm()


def parse_vlm_output(raw: str) -> dict:
    """Strict schema gate for model output (first JSON object in the response)."""
    s = re.sub(r"(?:\\n|\\t| ){4,}", " ", raw.strip())           # degenerate whitespace runs from greedy decoding
    if "```" in s:
        parts = s.split("```")
        s = max(parts, key=len)
        s = s[4:] if s.startswith("json") else s
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in model output")
    data = None
    end = b
    for _ in range(8):                               # truncated generation: close the block list after the last complete object
        for cand in (s[a:end + 1], s[a:end + 1] + "]}"):
            try:
                data = json.loads(cand)
                break
            except ValueError:
                continue
        if data is not None or end <= a:
            break
        end = s.rfind("}", a, end)
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")
    # deterministic normalisation of trivial structural slips before the strict schema gate
    for blk in data.get("blocks", []) if isinstance(data.get("blocks"), list) else []:
        if isinstance(blk, dict) and blk.get("type") == "table" and isinstance(blk.get("rows"), list):
            rows = []
            for row in blk["rows"]:
                row = [row] if isinstance(row, dict) else (row if isinstance(row, list) else [])
                cells = [{"text": c} if isinstance(c, str) else c for c in row]
                rows.append([{k: v for k, v in c.items() if k in ("text", "rowspan", "colspan")} if isinstance(c, dict) else c for c in cells])
            blk["rows"] = rows
            for k in [k for k in blk if k not in ("type", "rows")]:
                del blk[k]
        elif isinstance(blk, dict) and blk.get("type") == "paragraph":
            for k in [k for k in blk if k not in ("type", "text", "align", "bold", "size")]:
                del blk[k]
    jsonschema.validate(data, VLM_SCHEMA)
    return data


def structured_to_hwpx(pages: list[dict], page_size: tuple[float, float]) -> tuple[bytes, dict]:
    """Write schema-checked structured pages as native HWPX (tool-mediated: the model never writes XML)."""
    b = HwpxBuilder(page_size[0], page_size[1])
    body_w = page_size[0] - 2 * 56.7
    info = {"pages": len(pages), "tables": 0, "paragraphs": 0, "dropped_cells": 0}
    for pi, pg in enumerate(pages):
        first = True
        for blk in pg["blocks"]:
            pb = pi > 0 and first
            first = False
            if blk["type"] == "paragraph":
                b.add_para(blk["text"], _char(b, blk.get("size", 10.0), blk.get("bold", False)),
                           b.head.para_pr(align={"center": "CENTER", "right": "RIGHT"}.get(blk.get("align", "left"), "LEFT"), line_spacing=160), page_break=pb)
                info["paragraphs"] += 1
                continue
            occupied: set = set()
            cells = []
            n_cols = 0
            for r, row in enumerate(blk["rows"]):
                col = 0
                for cell in row:
                    while (r, col) in occupied:
                        col += 1
                    rs, cs = cell.get("rowspan", 1), cell.get("colspan", 1)
                    rs = min(rs, len(blk["rows"]) - r)
                    if any((rr, cc) in occupied for rr in range(r, r + rs) for cc in range(col, col + cs)):
                        info["dropped_cells"] += 1
                        continue
                    for rr in range(r, r + rs):
                        for cc in range(col, col + cs):
                            occupied.add((rr, cc))
                    cells.append({"row": r, "col": col, "rowspan": rs, "colspan": cs, "paras": [make_p(t, _char(b, 10.0)) for t in (cell["text"].split("\n") or [""])]})
                    col += cs
                    n_cols = max(n_cols, col)
            if not n_cols or n_cols > 60:
                continue
            tbl = make_table(b.next_id(), [body_w / n_cols] * n_cols, [22.0] * len(blk["rows"]), cells, b.head.border_fill(), inline=True)
            b.attach(tbl, b.add_para("", page_break=pb))
            info["tables"] += 1
    return b.to_bytes(), info


def vlm_generate(inp: ReconInput) -> tuple[bytes, dict]:
    be = get_vlm_backend()
    ok, why = be.available()
    cache_dir = path("data", "cache", "vlm")
    pages = _raster_pages(inp)
    out_pages, raw_hashes, cached_all = [], [], True
    for pa in pages:
        key = hashlib.sha256(pa.image + be.name.encode() + getattr(be, "model_id", "").encode() + VLM_PROMPT.encode()).hexdigest()
        cf = cache_dir / f"{key}.json"
        if cf.exists():
            raw = json.loads(cf.read_text(encoding="utf-8"))["raw"]
        else:
            cached_all = False
            if not ok:
                raise OSError(why)
            raw = be.generate(pa.image)
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps({"raw": raw, "backend": be.version()}, ensure_ascii=False), encoding="utf-8")
        raw_hashes.append(hashlib.sha256(raw.encode()).hexdigest()[:16])
        out_pages.append(parse_vlm_output(raw))
    data, info = structured_to_hwpx(out_pages, (pages[0].width, pages[0].height))
    info.update({"backend": be.version(), "from_cache": cached_all, "raw_output_hashes": raw_hashes})
    return data, info


# ----------------------------------------------------------------------------- B3 intermediary conversion
def _as_pdf(inp: ReconInput) -> bytes:
    if inp.pdf is not None:
        return inp.pdf
    pages = _raster_pages(inp)
    return images_to_pdf([p.image for p in pages], [(p.width, p.height) for p in pages])


def intermediary_pdf2docx(inp: ReconInput) -> tuple[bytes, dict]:
    try:
        from pdf2docx import Converter
    except ImportError as e:
        raise OSError("pdf2docx not installed") from e
    import logging
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="fb_p2d_") as td:
            src, dst = os.path.join(td, "in.pdf"), os.path.join(td, "out.docx")
            Path(src).write_bytes(_as_pdf(inp))
            cv = Converter(src)
            try:
                cv.convert(dst, multi_processing=False)
            finally:
                cv.close()
            docx = Path(dst).read_bytes()
    finally:
        logging.disable(logging.NOTSET)
    data, info = docx_to_hwpx(docx)
    from importlib.metadata import version
    info.update({"via": "docx", "converter": f"pdf2docx {version('pdf2docx')}", "docx_bytes": len(docx)})
    return data, info


def intermediary_libreoffice(inp: ReconInput) -> tuple[bytes, dict]:
    lo = LibreOfficeRenderer()
    ok, why = lo.available()
    if not ok:
        raise OSError(why)
    docx, err = lo.convert(_as_pdf(inp), ".pdf", to="docx:MS Word 2007 XML", infilter="writer_pdf_import")
    if docx is None:
        raise RuntimeError(f"LibreOffice PDF import failed: {err}")
    data, info = docx_to_hwpx(docx)
    info.update({"via": "docx", "converter": lo.version().get("version", "LibreOffice"), "docx_bytes": len(docx)})
    return data, info


BASELINES = {
    "image_only": ("B0", image_only), "image_overlay": ("B0", image_overlay), "parse_flow": ("B1", parse_flow), "parse_textbox": ("B1", parse_textbox),
    "vlm_generate": ("B2", vlm_generate), "intermediary_pdf2docx": ("B3", intermediary_pdf2docx), "intermediary_libreoffice": ("B3", intermediary_libreoffice),
}


def reconstruct(name: str, inp: ReconInput) -> ReconResult:
    family, fn = BASELINES[name]
    t0 = time.monotonic()
    try:
        data, info = fn(inp)
        res = ReconResult(name, family, Status.OK, data, info=info)
    except OSError as e:
        res = ReconResult(name, family, Status.BLOCKED_BY_ENVIRONMENT, reason=str(e)[:300])
    except Exception as e:      # noqa: BLE001 - a baseline failure is a recorded outcome, never a crash of the run
        res = ReconResult(name, family, Status.FAILED, reason=f"{type(e).__name__}: {e}"[:300])
    res.elapsed_s = round(time.monotonic() - t0, 3)
    res.info["baseline_version"] = BASELINE_VERSION
    return res
