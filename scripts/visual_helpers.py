"""Experiment source: visual similarity of every ODT reconstruction to the original's native rendering.

The reconstruction is rendered as an ODT application shows it (LibreOffice headless -> PDF, kept as
recon/<stem>.lo.pdf); both PDFs are rasterised at 72 dpi and compared page by page (SSIM, ink IoU; a missing page
scores 0) and by character error rate of their text layers. The renderers differ by design (native editor vs.
LibreOffice): the pair is recorded with every score. Cached in recon/<stem>.visual.json; resumable.
Usage: python scripts/visual_helpers.py [--workers N]"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from formbench.util import obs_name, path, write_json

DPI = 72
BLUR_SIGMA = 1.0        # px at 72 dpi: SSIM after a light blur is insensitive to raster-vs-vector anti-aliasing


def ssim_blur(a: bytes, b: bytes) -> float:
    import io

    import numpy as np
    from PIL import Image
    from scipy.ndimage import gaussian_filter
    from skimage.metrics import structural_similarity
    ga = np.asarray(Image.open(io.BytesIO(a)).convert("L"), dtype=float)
    im = Image.open(io.BytesIO(b)).convert("L")
    if im.size != (ga.shape[1], ga.shape[0]):
        im = im.resize((ga.shape[1], ga.shape[0]), Image.BILINEAR)
    gb = np.asarray(im, dtype=float)
    return round(float(structural_similarity(gaussian_filter(ga, BLUR_SIGMA), gaussian_filter(gb, BLUR_SIGMA), data_range=255)), 4)


def render_batch(odts: list[str]) -> None:
    """One LibreOffice start for many files (a start per file made the run disk-bound)."""
    import shutil
    import tempfile

    from formbench.formats.renderers import find_soffice
    from formbench.util import run_cmd
    todo = [Path(o) for o in odts if not Path(o[:-4] + ".lo.pdf").exists()]
    if not todo:
        return
    so = find_soffice()
    with tempfile.TemporaryDirectory(prefix="fb_sourcevis_") as td:
        names = {}
        for k, o in enumerate(todo):
            n = f"f{k:04d}.odt"
            shutil.copyfile(o, Path(td) / n)
            names[n] = o
        run_cmd([so, "--headless", "--norestore", f"-env:UserInstallation=file:///tmp/claude-1000/lo_sourcevis_{os.getpid()}",
                 "--convert-to", "pdf", "--outdir", td] + [str(Path(td) / n) for n in names], timeout=120 + 60 * len(names))
        for n, o in names.items():
            pdf = Path(td) / (n[:-4] + ".pdf")
            if pdf.exists():
                shutil.copyfile(pdf, o.parent / f"{o.stem}.lo.pdf")


def batch(odts: list[str]) -> list[dict]:
    try:
        render_batch(odts)
    except Exception:  # noqa: BLE001 - files without a PDF are scored as failed below
        pass
    return [one(o) for o in odts]


def one(odt_path: str) -> dict:
    import pymupdf

    from formbench.formats.renderers import pdf_to_pngs
    from formbench.metrics.visual import page_similarity, text_cer
    p = Path(odt_path)
    d = p.parent.parent
    out = p.parent / f"{p.stem}.visual.json"
    nat = json.loads((d / "native.json").read_text())
    try:
        lo_pdf = p.parent / f"{p.stem}.lo.pdf"
        if not lo_pdf.exists():
            raise RuntimeError("LibreOffice produced no PDF")
        a, b = (d / "native.pdf").read_bytes(), lo_pdf.read_bytes()
        pa, pb = pdf_to_pngs(a, DPI), pdf_to_pngs(b, DPI)
        n = max(len(pa), len(pb))
        per = [page_similarity(pa[i], pb[i]) | {"ssim_blur": ssim_blur(pa[i], pb[i])} if i < len(pa) and i < len(pb)
               else {"ssim": 0.0, "ink_iou": 0.0, "ssim_blur": 0.0} for i in range(n)]
        ta = "".join(pg.get_text() for pg in pymupdf.open(stream=a, filetype="pdf"))
        tb = "".join(pg.get_text() for pg in pymupdf.open(stream=b, filetype="pdf"))
        res = {"status": "ok", "ssim": round(sum(x["ssim"] for x in per) / n, 4), "ssim_blur": round(sum(x["ssim_blur"] for x in per) / n, 4),
               "blur_sigma_px": BLUR_SIGMA, "ink_iou": round(sum(x["ink_iou"] for x in per) / n, 4),
               "ssim_page1": per[0]["ssim"], "pages_native": len(pa), "pages_recon": len(pb), "page_count_match": len(pa) == len(pb),
               "text_cer": text_cer(ta, tb), "recon_text_chars": len("".join(tb.split())), "dpi": DPI,
               "renderers": {"original": nat.get("renderer"), "reconstruction": "LibreOffice (headless PDF export)"}}
    except Exception as e:  # noqa: BLE001
        res = {"status": "failed", "reason": f"{type(e).__name__}: {e}"[:300]}
    write_json(out, res)
    return {"file": str(p), **{k: res.get(k) for k in ("status", "ssim", "pages_native", "pages_recon")}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    todo = [f for f in sorted(glob.glob(str(path("data", obs_name(), "*", "recon", "*.odt"))))
            if not Path(f[:-4] + ".visual.json").exists()]
    print(f"# {len(todo)} reconstructions to score", flush=True)
    chunks = [todo[i:i + 25] for i in range(0, len(todo), 25)]
    k = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(batch, c) for c in chunks]):
            for r in fut.result():
                k += 1
                if k % 50 == 0 or r["status"] != "ok":
                    print(k, json.dumps(r, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
