"""Experiment edit: the example as an ODT document (reference of the block edit cost for ODT reconstructions).
Every format goes through LibreOffice's own import, so a lossless copy of the original scores 1 (positive control,
scripts/convert_originals.py): DOCX examples: LibreOffice import; ODT examples: the file itself; HWPX examples: LibreOffice with
the H2Orestart import filter (experiment §6). When LibreOffice cannot store a HWPX task's template or example as
ODT directly, both files of that task go through DOCX (HWPX -> DOCX -> ODT), so the two stay on one route.
Output: data/source_documents/<task>/example.odt and example_odt.json (route). --redo-hwpx rebuilds the HWPX examples.
Usage: python scripts/convert_examples.py [--redo-hwpx]"""
import argparse
import glob
import importlib.util
import json
import sys
import threading
from pathlib import Path

from formbench.collection.discovery import blob_path
from formbench.util import DB, path, utcnow, write_json

HERE = Path(__file__).resolve().parent
_R = None


_LOCK = threading.Lock()


def _recon():
    """scripts/reconstruction_helpers.py as a module, loaded once (thread-safe: published only after it has fully loaded)."""
    global _R
    with _LOCK:
        if _R is None:
            spec = importlib.util.spec_from_file_location("sourcer", HERE / "reconstruction_helpers.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _R = m
    return _R


def hwpx_to_odt(data: bytes, via_docx: bool) -> tuple[bytes | None, str]:
    """HWPX -> ODT with LibreOffice + H2Orestart, directly or through DOCX."""
    r = _recon()
    if not via_docx:
        return r._lo_convert(data, ".hwpx", "odt")
    docx, err = r._lo_convert(data, ".hwpx", "docx")
    if not docx:
        return None, err
    return r._lo_convert(docx, ".docx", "odt")


def hwpx_route(template: bytes, example: bytes) -> tuple[str, bytes | None, bytes | None, str]:
    """One route for both files of a HWPX task: direct when both convert directly, else both through DOCX."""
    t, et = hwpx_to_odt(template, False)
    e, ee = hwpx_to_odt(example, False)
    if t and e:
        return "libreoffice_h2orestart", t, e, ""
    t, et = hwpx_to_odt(template, True)
    e, ee = hwpx_to_odt(example, True)
    return "libreoffice_h2orestart_via_docx", t, e, (et or ee)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo-hwpx", action="store_true")
    a = ap.parse_args()
    db = DB()
    n = 0
    for f in sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json")))):
        c = json.load(open(f))
        d = Path(path("data", "source_documents", Path(f).stem))
        out = d / "example.odt"
        meta = d / "example_odt.json"
        done_h2o = meta.exists() and json.load(open(meta)).get("route", "").startswith("libreoffice_h2orestart")
        if out.exists() and not (a.redo_hwpx and c["format"] == "hwpx" and not done_h2o):
            continue
        example = blob_path(db.get_doc(c["completed_id"])["sha256"]).read_bytes()
        if c["format"] == "odt":
            odt, route, err = example, "copy", ""
        elif c["format"] == "docx":
            odt, err = _recon()._lo_convert(example, ".docx", "odt")
            route = "libreoffice_import"
        else:
            template = blob_path(db.get_doc(c["template_id"])["sha256"]).read_bytes()
            route, _t, odt, err = hwpx_route(template, example)
        if not odt:
            print(c["task_id"], "convert failed", err[:100], flush=True)
            continue
        out.write_bytes(odt)
        write_json(d / "example_odt.json", {"task_id": c["task_id"], "format": c["format"], "route": route, "created_at": utcnow()})
        n += 1
    print("example.odt written", n)


if __name__ == "__main__":
    sys.exit(main())
