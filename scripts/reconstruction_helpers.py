"""Experiment source, step 2: inputs from the NATIVE rendering and ODT reconstructions by every baseline.

Inputs (data/source_documents/<task>/inputs/), all derived from native.pdf (Word / Hangul / LibreOffice rendering of the blank):
  vector_pdf     the native PDF itself
  png_150        page images, 150 dpi
  scan_pdf_150   page images degraded like a scan (JPEG q85, noise 3) wrapped in an image-only PDF

Baselines (output: data/source_documents/<task>/recon/<input>__<baseline>.odt + .json). Reconstructors receive only the input
(+ the document language for OCR model selection); never the blank's structure, the example or the contract.
  image_only, image_overlay, parse_flow, parse_textbox   in-house B0/B1 (HWPX writer -> ODT writer)
  lo_pdf_import        LibreOffice Writer PDF import (writer_pdf_import) saved as ODT
  pdf2docx_lo          pdf2docx -> DOCX -> LibreOffice -> ODT
  word_pdf             Microsoft Word opens the PDF (PDF Reflow) and saves ODT          (Windows host, serial)
  hwp_pdf              Hancom Hangul opens the PDF (PDF import) and saves ODT            (Windows host, serial)

Resumable: an existing <stem>.json is never redone.
Usage: python scripts/reconstruction_helpers.py [--part local|windows|all] [--workers N] [--limit N] [--baselines a,b] [--inputs a,b]"""
from __future__ import annotations

import argparse
import functools
import glob
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from formbench.util import obs_name, path, sha256_bytes, utcnow, write_json

OUT = Path(path("data", obs_name()))
SEED = 20261014
INPUTS = {"vector_pdf": "pdf", "png_150": "image", "scan_pdf_150": "pdf", "image_pdf_150": "pdf"}
LOCAL = {"image_only": ["vector_pdf", "png_150"], "image_overlay": ["vector_pdf", "png_150", "scan_pdf_150"],
         "parse_flow": ["vector_pdf", "png_150", "scan_pdf_150"], "parse_textbox": ["vector_pdf", "png_150", "scan_pdf_150"],
         "lo_pdf_import": ["vector_pdf", "scan_pdf_150"], "pdf2docx_lo": ["vector_pdf", "scan_pdf_150"]}
WINDOWS = {"word_pdf": ["vector_pdf", "scan_pdf_150", "image_pdf_150"], "hwp_pdf": ["vector_pdf", "scan_pdf_150", "image_pdf_150"]}
VLM = {"vlm_generate": ["png_150"]}                 # GPU, serial: local Qwen2.5-VL-3B (structured JSON -> ODT)
FAMILY = {"vlm_generate": "B2", "image_only": "B0", "image_overlay": "B0", "parse_flow": "B1", "parse_textbox": "B1", "lo_pdf_import": "B3",
          "pdf2docx_lo": "B3", "word_pdf": "B3-app", "hwp_pdf": "B3-app"}


def tasks() -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(path("data", "contracts_obs", "doc-*.json")))):
        c = json.load(open(f))
        if c["status"] not in ("ok", "needs_review"):
            continue
        d = OUT / c["task_id"].replace("~", "__")
        if (d / "native.pdf").exists():
            out.append({"task_id": c["task_id"], "dir": str(d), "lang": c.get("language") or "ko"})
    return out


def make_inputs(d: Path) -> dict:
    from formbench.formats.renderers import degrade_image, images_to_pdf, pdf_to_pngs
    inp = d / "inputs"
    meta_f = inp / "inputs.json"
    if meta_f.exists():
        meta = json.loads(meta_f.read_text())
        if "image_pdf_150" not in meta:                # edit image condition: the clean png_150 pages, no text layer
            files = [inp / f for f in meta["png_150"]["files"]]
            pdf = images_to_pdf([f.read_bytes() for f in files], [tuple(s) for s in meta["page_sizes"]])
            (inp / "image_pdf_150.pdf").write_bytes(pdf)
            meta["image_pdf_150"] = {"files": ["image_pdf_150.pdf"], "dpi": 150, "from": "png_150 (lossless, no text layer)",
                                     "sha256": [sha256_bytes(pdf)]}
            write_json(meta_f, meta)
        return meta
    import pymupdf
    inp.mkdir(parents=True, exist_ok=True)
    pdf = (d / "native.pdf").read_bytes()
    sizes = [(p.rect.width, p.rect.height) for p in pymupdf.open(stream=pdf, filetype="pdf")]
    (inp / "vector_pdf.pdf").write_bytes(pdf)
    pngs = pdf_to_pngs(pdf, 150)
    files = []
    for k, b in enumerate(pngs):
        (inp / f"png_150_p{k}.png").write_bytes(b)
        files.append(f"png_150_p{k}.png")
    deg = [degrade_image(b, jpeg_quality=85, noise_sigma=3.0, seed=SEED + k, out_format="jpeg") for k, b in enumerate(pngs)]
    (inp / "scan_pdf_150.pdf").write_bytes(images_to_pdf(deg, sizes))
    meta = {"pages": len(sizes), "page_sizes": sizes, "created_at": utcnow(),
            "vector_pdf": {"files": ["vector_pdf.pdf"], "sha256": [sha256_bytes(pdf)]},
            "png_150": {"files": files, "dpi": 150, "sha256": [sha256_bytes((inp / f).read_bytes()) for f in files]},
            "scan_pdf_150": {"files": ["scan_pdf_150.pdf"], "dpi": 150, "jpeg_quality": 85, "noise_sigma": 3.0, "seed": SEED,
                             "sha256": [sha256_bytes((inp / "scan_pdf_150.pdf").read_bytes())]}}
    write_json(meta_f, meta)
    return meta


def recon_input(d: Path, iid: str, lang: str):
    from formbench.reconstruction.adapters import ReconInput
    inp = d / "inputs"
    if INPUTS[iid] == "pdf":
        kind = "vector_pdf" if iid == "vector_pdf" else "scanned_pdf"
        return ReconInput(kind, pdf=(inp / f"{iid}.pdf").read_bytes(), label=iid, lang=lang)
    files = sorted(inp.glob(f"{iid}_p*.png"), key=lambda p: int(p.stem.rsplit("_p", 1)[1]))
    return ReconInput("image", images=tuple(f.read_bytes() for f in files), dpi=150, label=iid, lang=lang)


def _lo_profile() -> str:
    import threading
    return f"file:///tmp/claude-1000/lo_source_prof_{os.getpid()}_{threading.get_ident() % 100000}"


def _lo_convert(data: bytes, ext: str, to: str, infilter: str | None = None) -> tuple[bytes | None, str]:
    import tempfile

    from formbench.formats.renderers import find_soffice
    from formbench.util import run_cmd
    so = find_soffice()
    if not so:
        raise OSError("soffice not found")
    with tempfile.TemporaryDirectory(prefix="fb_sourcelo_") as td:
        src = Path(td) / f"in{ext}"
        src.write_bytes(data)
        cmd = [so, "--headless", "--norestore", f"-env:UserInstallation={_lo_profile()}"]
        if infilter:
            cmd.append(f"--infilter={infilter}")
        cmd += ["--convert-to", to, "--outdir", td, str(src)]
        rc, out, err = run_cmd(cmd, timeout=int(os.environ.get("FORMBENCH_LO_TIMEOUT", "300")))
        dst = Path(td) / f"in.{to.split(':')[0]}"
        if dst.exists() and dst.stat().st_size:
            return dst.read_bytes(), ""
        return None, (err or out or f"rc={rc}").strip()[-300:]


@functools.lru_cache(maxsize=1)
def lo_version() -> str:
    from formbench.formats.renderers import find_soffice
    from formbench.util import run_cmd
    so = find_soffice()
    return run_cmd([so, "--version"], timeout=60)[1].strip() if so else ""


def run_local(b: str, d: Path, iid: str, lang: str) -> tuple[bytes, dict]:
    if b in ("image_only", "image_overlay", "parse_flow", "parse_textbox", "vlm_generate"):
        from formbench.formats.odt_write import hwpx_to_odt
        from formbench.reconstruction.baselines import reconstruct
        r = reconstruct(b, recon_input(d, iid, lang))
        if r.status != "ok" or not r.hwpx:
            raise RuntimeError(f"{r.status}: {r.reason}")
        odt, info = hwpx_to_odt(r.hwpx)
        return odt, {"baseline_info": r.info, "odt_writer": info, "via": "hwpx"}
    pdf = (d / "inputs" / f"{iid}.pdf").read_bytes()
    if b == "lo_pdf_import":
        odt, err = _lo_convert(pdf, ".pdf", "odt", infilter="writer_pdf_import")
        if odt is None:
            raise RuntimeError(f"LibreOffice PDF import failed: {err}")
        return odt, {"converter": lo_version(), "filter": "writer_pdf_import"}
    if b == "pdf2docx_lo":
        import logging
        import tempfile

        from pdf2docx import Converter
        logging.disable(logging.CRITICAL)
        try:
            with tempfile.TemporaryDirectory(prefix="fb_sourcep2d_") as td:
                src, dst = os.path.join(td, "in.pdf"), os.path.join(td, "out.docx")
                Path(src).write_bytes(pdf)
                cv = Converter(src)
                try:
                    cv.convert(dst, multi_processing=False)
                finally:
                    cv.close()
                docx = Path(dst).read_bytes()
        finally:
            logging.disable(logging.NOTSET)
        odt, err = _lo_convert(docx, ".docx", "odt")
        if odt is None:
            raise RuntimeError(f"LibreOffice DOCX->ODT failed: {err}")
        from importlib.metadata import version
        return odt, {"converter": f"pdf2docx {version('pdf2docx')} + {lo_version()}", "docx_bytes": len(docx)}
    raise ValueError(b)


def local_job(args) -> list[dict]:
    t, plan = args
    os.environ.setdefault("FORMBENCH_OCR_THREADS", "2")
    d = Path(t["dir"])
    logs = []
    try:
        make_inputs(d)
    except Exception as e:  # noqa: BLE001
        return [{"task_id": t["task_id"], "status": "input_failed", "reason": f"{type(e).__name__}: {e}"[:300]}]
    rd = d / "recon"
    rd.mkdir(exist_ok=True)
    for b, iid in plan:
        stem = f"{iid}__{b}"
        if (rd / f"{stem}.json").exists():
            continue
        t0 = time.monotonic()
        rec = {"task_id": t["task_id"], "input": iid, "baseline": b, "family": FAMILY[b], "started_at": utcnow()}
        try:
            odt, info = run_local(b, d, iid, t["lang"])
            (rd / f"{stem}.odt").write_bytes(odt)
            rec.update(status="ok", sha256=sha256_bytes(odt), info=info)
        except OSError as e:
            rec.update(status="blocked_by_environment", reason=str(e)[:300])
        except Exception as e:  # noqa: BLE001 - a baseline failure is a recorded outcome
            rec.update(status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        prev = rd / f"{stem}.attempt1.json"             # an earlier attempt that failed for an environment reason
        if prev.exists():
            rec["previous_attempts"] = [json.loads(prev.read_text())]
        write_json(rd / f"{stem}.json", rec)
        logs.append({k: rec.get(k) for k in ("task_id", "input", "baseline", "status", "elapsed_s")})
    return logs


def run_windows(ts: list[dict], baselines: dict, log=print):
    from formbench.formats.native_render import app_pdf_to_odt
    for b, inputs in baselines.items():
        app = {"word_pdf": "word", "hwp_pdf": "hwp"}[b]
        for t in ts:
            d = Path(t["dir"])
            make_inputs(d)
            rd = d / "recon"
            rd.mkdir(exist_ok=True)
            for iid in inputs:
                stem = f"{iid}__{b}"
                if (rd / f"{stem}.json").exists():
                    continue
                t0 = time.monotonic()
                for attempt in range(4):                    # our previous instance may still be shutting down
                    r = app_pdf_to_odt(app, [((d / "inputs" / f"{iid}.pdf").read_bytes(), "x")])["x"]
                    if r["status"] != "blocked":
                        break
                    time.sleep(20)
                rec = {"task_id": t["task_id"], "input": iid, "baseline": b, "family": FAMILY[b], "started_at": utcnow(),
                       "status": r["status"], "reason": r.get("reason", ""), "info": {"converter": r.get("app"), "app_seconds": r.get("s")}}
                if r["status"] == "blocked":               # the user has the application open: stop, resume later
                    log(json.dumps({"task_id": t["task_id"], "baseline": b, "status": "blocked", "reason": r.get("reason")}))
                    return False
                if r["status"] == "ok":
                    (rd / f"{stem}.odt").write_bytes(r["odt"])
                    rec["sha256"] = r["sha256"]
                rec["elapsed_s"] = round(time.monotonic() - t0, 2)
                write_json(rd / f"{stem}.json", rec)
                log(json.dumps({k: rec.get(k) for k in ("task_id", "input", "baseline", "status", "elapsed_s")} | {"reason": rec["reason"][:120]},
                               ensure_ascii=False))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", default="all", choices=["local", "windows", "vlm", "all"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--baselines", default="")
    ap.add_argument("--inputs", default="")
    a = ap.parse_args()
    ts = tasks()
    if a.limit:
        ts = ts[: a.limit]
    sel_b = set(a.baselines.split(",")) if a.baselines else None
    sel_i = set(a.inputs.split(",")) if a.inputs else None

    def keep(b, i):
        return (sel_b is None or b in sel_b) and (sel_i is None or i in sel_i)
    if a.part in ("windows", "all"):
        wb = {b: [i for i in v if keep(b, i)] for b, v in WINDOWS.items() if any(keep(b, i) for i in v)}
        if wb:
            run_windows(ts, wb, log=lambda s: print(s, flush=True))
    if a.part == "vlm":                                    # serial on the GPU, same recording as the local part
        plan = [(b, i) for b, v in VLM.items() for i in v if keep(b, i)]
        for k, t in enumerate(ts, 1):
            for lg in local_job((t, plan)):
                print(json.dumps(lg, ensure_ascii=False), flush=True)
            print(f"# vlm {k}/{len(ts)}", flush=True)
    if a.part in ("local", "all"):
        plan = [(b, i) for b, v in LOCAL.items() for i in v if keep(b, i)]
        done = 0
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for fut in as_completed([ex.submit(local_job, (t, plan)) for t in ts]):
                done += 1
                for lg in fut.result():
                    print(json.dumps(lg, ensure_ascii=False), flush=True)
                print(f"# local {done}/{len(ts)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
