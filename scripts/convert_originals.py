"""Experiment edit positive control: the ORIGINAL blank form converted to ODT by the same route as the example's ODT
reference (experiment §6). DOCX: LibreOffice import; ODT: the file itself; HWPX: LibreOffice + H2Orestart, on
the same route as the task's example (example_odt.json: direct, or through DOCX). A reconstruction cannot be better than a lossless copy of the original, so this label's ratio is the
ceiling of metric ② under the ODT pipeline: ratio 1 means the format change and the reference asymmetry cost nothing.
Output: data/source_documents/<task>/recon/oracle__template_odt.{odt,json}. Resumable.
Usage: python scripts/convert_originals.py [--redo-hwpx] [--workers N]"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from formbench.collection.discovery import blob_path
from formbench.util import DB, path, sha256_bytes, utcnow, write_json

LABEL = "oracle__template_odt"
HERE = Path(__file__).resolve().parent


def _record(d: Path, c: dict, odt: bytes | None, route: str, t0: float, reason: str = "") -> None:
    rd = d / "recon"
    rd.mkdir(exist_ok=True)
    rec = {"task_id": c["task_id"], "input": "template", "baseline": "oracle", "label": LABEL, "created_at": utcnow(),
           "elapsed_s": round(time.time() - t0, 2), "info": {"route": route}}
    if odt:
        (rd / f"{LABEL}.odt").write_bytes(odt)
        rec.update(status="ok", sha256=sha256_bytes(odt))
    else:
        rec.update(status="failed", reason=reason[:300])
    write_json(rd / f"{LABEL}.json", rec)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo-hwpx", action="store_true", help="rebuild HWPX controls made by an earlier route")
    ap.add_argument("--workers", type=int, default=4, help="parallel LibreOffice conversions (one profile per thread)")
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("sourcer", HERE / "reconstruction_helpers.py")
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    db = DB()
    hw, docx, n = [], [], 0
    for f in sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json")))):
        c = json.load(open(f))
        d = Path(path("data", "source_documents", Path(f).stem))
        meta = d / "recon" / f"{LABEL}.json"
        if meta.exists() and not (a.redo_hwpx and c["format"] == "hwpx"
                                  and not json.load(open(meta))["info"]["route"].startswith("libreoffice_h2orestart")):
            continue
        data = blob_path(db.get_doc(c["template_id"])["sha256"]).read_bytes()
        if c["format"] == "odt":
            _record(d, c, data, "copy", time.time())
            n += 1
        elif c["format"] == "docx":
            docx.append((d, c, data))
        elif c["format"] == "hwpx":
            em = d / "example_odt.json"
            if not em.exists():
                print(c["task_id"], "example.odt not built yet", flush=True)
                continue
            hw.append((d, c, data, json.load(open(em))["route"]))

    def conv(item):
        d, c, data = item
        t0 = time.time()
        odt, err = r._lo_convert(data, ".docx", "odt")
        _record(d, c, odt, "libreoffice_import", t0, err)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(conv, docx))
    n += len(docx)
    ex_spec = importlib.util.spec_from_file_location("edite", HERE / "convert_examples.py")
    ex = importlib.util.module_from_spec(ex_spec)
    ex_spec.loader.exec_module(ex)

    def conv_hwpx(item):
        d, c, data, route = item
        t0 = time.time()
        odt, err = ex.hwpx_to_odt(data, route.endswith("via_docx"))
        _record(d, c, odt, route, t0, err)
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        list(pool.map(conv_hwpx, hw))
    n += len(hw)
    print(f"{LABEL}: {n} written", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
