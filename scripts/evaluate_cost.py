"""Experiment edit, metric ②: human actions to turn each document into the example (editing/cost.py), for the original
(native format, compared with the example in that format) and for every reconstruction (ODT, compared with the example
as ODT through LibreOffice's import: example.odt). Always finite. Output: data/runs/<run_id>/<label>/<task>.json. Resumable.
Usage: python scripts/evaluate_cost.py <run_id> [--workers N]"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from formbench.util import obs_name, path, utcnow, write_json

LABELS = ["oracle__template_odt", "png_150__image_only", "vector_pdf__word_pdf", "vector_pdf__hwp_pdf", "vector_pdf__docling_odt",
          "vector_pdf__mineru", "png_150__gpt6luna_odt"]


def empty_odt() -> bytes:
    """A new empty text document (A4, one empty paragraph) as an ODT application creates it."""
    import io
    import zipfile
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3"><office:body><office:text><text:p/>'
               '</office:text></office:body></office:document-content>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)
        z.writestr("styles.xml", '<?xml version="1.0"?><office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                                 'office:version="1.3"/>')
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
                   'manifest:version="1.3"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                   '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
    return b.getvalue()


def task_job(args) -> list[dict]:
    cpath, run_dir = args
    from formbench.collection.discovery import blob_path
    from formbench.completion.sessions import open_session
    from formbench.util import DB
    from formbench.editing.cost import COST_VERSION, document_cost
    c = json.load(open(cpath))
    tid = c["task_id"].replace("~", "__")
    d = Path(path("data", obs_name(), tid))
    out, logs = Path(run_dir), []
    db = DB()

    def save(label, rec):
        (out / label).mkdir(parents=True, exist_ok=True)
        write_json(out / label / f"{tid}.json", rec)
        logs.append({"task": tid[:20], "label": label, "cost": rec.get("cost"), "status": rec["status"]})
    todo = [lab for lab in ["original"] + LABELS if not (out / lab / f"{tid}.json").exists()]
    if not todo:
        return logs
    # current (experiment): the original and the example were preprocessed to ODT; both edit times are measured on ODT
    current = (d / "template.odt").exists()
    e_native = None if current else open_session(blob_path(db.get_doc(c["completed_id"])["sha256"]).read_bytes(), c["format"])
    # reference for the ODT reconstructions: the example as ODT through LibreOffice's own import (DOCX: LibreOffice,
    # ODT: itself, HWPX: LibreOffice + H2Orestart), the same route as the positive control (experiment §6)
    e_odt = open_session((d / "example.odt").read_bytes(), "odt") if (d / "example.odt").exists() else None
    for lab in todo:
        t0 = time.time()
        from formbench.editing.cost import C as _C
        rec = {"task_id": c["task_id"], "label": lab, "format": c["format"], "language": c.get("language"),
               "source_domain": c.get("source_domain"), "cost_version": COST_VERSION, "computed_at": utcnow(),
               "tolerance_pt": _C()["layout_tolerance_pt"], "unit": _C().get("unit", "actions"),
               "klm_K": (_C().get("klm") or {}).get("K"), "keys_per_char_flat": len(set((_C().get("keys_per_char") or {"x": 0}).values())) == 1}
        try:
            if lab == "original" and current:
                r = document_cost(open_session((d / "template.odt").read_bytes(), "odt"), e_odt, lang=c.get("language"))
            elif lab == "original":
                dsess = open_session(blob_path(db.get_doc(c["template_id"])["sha256"]).read_bytes(), c["format"])
                r = document_cost(dsess, e_native, lang=c.get("language"))
            else:
                f = d / "recon" / f"{lab}.odt"
                meta = d / "recon" / f"{lab}.json"
                if e_odt is None:
                    continue                                      # example.odt not ready yet
                if not f.exists():
                    st = json.load(open(meta)).get("status") if meta.exists() else "missing"
                    if st == "missing":
                        continue                                  # not produced yet: computed on a later run
                    # the reconstruction failed: the user writes the example from an empty document (experiment §6)
                    rec.update(reconstruction_status=st, from_empty_document=True)
                    r = document_cost(open_session(empty_odt(), "odt"), e_odt, lang=c.get("language"))
                else:
                    r = document_cost(open_session(f.read_bytes(), "odt"), e_odt, lang=c.get("language"))
            rec.update(status="ok", cost=r["cost"], page_setup=r["page_setup"],
                       zones={z: {k: v for k, v in x.items() if k != "ops"} for z, x in r["zones"].items()},
                       ops=r["zones"]["body"]["ops"][:400], seconds=round(time.time() - t0, 2))
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", error=f"{type(e).__name__}: {e}"[:300], traceback=traceback.format_exc()[-1200:])
        save(lab, rec)
    return logs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    run_dir = Path(path("data", "runs", a.run_id))
    run_dir.mkdir(parents=True, exist_ok=True)
    cs = sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json"))))
    n = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(task_job, (c, str(run_dir))) for c in cs]):
            n += 1
            for lg in fut.result():
                if lg["status"] != "ok":
                    print(json.dumps(lg), flush=True)
            print(f"# {n}/{len(cs)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
