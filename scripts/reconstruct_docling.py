"""Experiment edit (user decision 2026-10-18): Docling with a direct ODT writer instead of HTML -> LibreOffice -> ODT.

1. Docling reruns on the same input (vector_pdf) with the same pipeline options as the first run, now saving the
   DoclingDocument JSON (src/formbench/reconstruction/external/docling_json_run.py, own venv, one GPU process; the HTML
   of the rerun is kept and compared with the first run's HTML to show the recognition is unchanged).
2. formbench.reconstruction.docling_odt writes the ODT from the JSON (no office application; rules in that module).
Label: vector_pdf__docling_odt in data/source_documents/<task>/recon/ (.docling.json, .odt, .json record). Resumable.
Usage: python scripts/reconstruct_docling.py [--limit N] [--chunk 20]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from formbench.reconstruction.docling_odt import docling_json_to_odt  # noqa: E402
from formbench.util import obs_name, path, sha256_bytes, utcnow, write_json  # noqa: E402

LABEL = "vector_pdf__docling_odt"
INPUT = "vector_pdf"
PY = Path.home() / "opt" / "venvs" / "docling" / "bin" / "python"
RUNNER = Path(path("src", "formbench", "reconstruction", "external", "docling_json_run.py"))
WRITER = Path(path("src", "formbench", "reconstruction", "docling_odt.py"))
LICENSE = "MIT (docling, docling-ibm-models); Apache-2.0 (easyocr)"


def tasks() -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json")))):
        c = json.load(open(f))
        d = Path(path("data", obs_name(), c["task_id"].replace("~", "__")))
        out.append({"task_id": c["task_id"], "dir": d, "lang": c.get("language") or "en"})
    return out


def finish(t: dict, j: dict) -> dict:
    rd = t["dir"] / "recon"
    js = rd / f"{LABEL}.docling.json"
    rec = {"task_id": t["task_id"], "input": INPUT, "baseline": "docling_odt", "family": "B3-parser", "started_at": utcnow(),
           "status": j["status"], "reason": j.get("reason", ""), "elapsed_s": j.get("s"),
           "info": {"tool": j.get("tool"), "license": LICENSE, "ocr_lang": j.get("ocr_lang"), "via": "DoclingDocument JSON -> docling_odt",
                    "writer_sha256": sha256_bytes(WRITER.read_bytes())}}
    old = rd / f"{INPUT}__docling.html"
    new = rd / f"{LABEL}.docling.rerun.html"
    if old.exists() and new.exists():
        rec["info"]["rerun_html_identical_to_first_run"] = old.read_bytes() == new.read_bytes()
        mask = lambda b: re.sub(rb"base64,[^\"]*", b"B64", b)  # noqa: E731   embedded image bytes may be re-encoded
        rec["info"]["rerun_html_identical_except_images"] = mask(old.read_bytes()) == mask(new.read_bytes())
    if j["status"] == "ok" and js.exists():
        try:
            odt = docling_json_to_odt(js.read_text(encoding="utf-8"))
            (rd / f"{LABEL}.odt").write_bytes(odt)
            rec.update(sha256=sha256_bytes(odt))
        except Exception as e:  # noqa: BLE001
            rec.update(status="failed", reason=f"ODT writer: {type(e).__name__}: {e}"[:300])
    write_json(rd / f"{LABEL}.json", rec)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--rewrite", action="store_true", help="rewrite every ODT from the saved JSON with the current writer")
    a = ap.parse_args()
    if a.rewrite:
        n = 0
        for t in tasks():
            f = t["dir"] / "recon" / f"{LABEL}.json"
            if not f.exists():
                continue
            r = json.load(open(f))
            j = {"status": "ok" if (t["dir"] / "recon" / f"{LABEL}.docling.json").exists() and (r["status"] == "ok" or r["reason"].startswith("ODT writer"))
                 else r["status"], "reason": "" if r["reason"].startswith("ODT writer") else r.get("reason", ""), "s": r.get("elapsed_s"),
                 "tool": r["info"].get("tool"), "ocr_lang": r["info"].get("ocr_lang")}
            (t["dir"] / "recon" / f"{LABEL}.odt").unlink(missing_ok=True)
            rec = finish(t, j)
            rec["started_at"] = r["started_at"]
            write_json(f, rec)
            n += 1
        print(f"# rewrote {n}", flush=True)
        return 0
    ts = tasks()
    if a.limit:
        ts = ts[: a.limit]
    todo = [t for t in ts if not (t["dir"] / "recon" / f"{LABEL}.json").exists()
            and (t["dir"] / "inputs" / f"{INPUT}.pdf").exists()]     # current builds the inputs per task: a later run does the rest
    work = Path(path("data", obs_name(), "_external", "docling_odt"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"# {len(todo)} tasks", flush=True)
    for c0 in range(0, len(todo), a.chunk):
        chunk = todo[c0:c0 + a.chunk]
        lst, out = work / "list.tsv", work / "out.jsonl"
        out.unlink(missing_ok=True)
        lst.write_text("\n".join(f"{(t['dir'] / 'inputs' / f'{INPUT}.pdf').resolve()}\t{(t['dir'] / 'recon' / f'{LABEL}.docling.json').resolve()}\t{t['lang']}"
                                 for t in chunk), encoding="utf-8")
        t0 = time.monotonic()
        hung = False
        try:
            subprocess.run([str(PY), "-I", str(RUNNER), str(lst), str(out)], cwd=work, env=dict(os.environ, PYTHONNOUSERSITE="1"),
                           stdout=subprocess.DEVNULL, stderr=open(work / "stderr.log", "a"), timeout=700 * len(chunk))
        except subprocess.TimeoutExpired:
            hung = True
        recs = {}
        if out.exists():
            for x in out.read_text(encoding="utf-8").splitlines():
                if x.strip():
                    j = json.loads(x)
                    recs[j["src"]] = j
        first_missing = True
        for t in chunk:
            j = recs.get(str((t["dir"] / "inputs" / f"{INPUT}.pdf").resolve()))
            if j is None:
                if not first_missing:
                    continue                                       # never reached: the next run retries it
                first_missing = False
                j = {"status": "timeout" if hung else "failed",
                     "reason": "tool process hung on this file" if hung else "tool process ended without a record"}
            if "CUDA error" in (j.get("reason") or "") or "out of memory" in (j.get("reason") or "").lower():
                print(json.dumps({"task_id": t["task_id"], "status": "retry_later", "reason": j["reason"][:120]}), flush=True)
                continue
            rec = finish(t, j)
            print(json.dumps({"task_id": t["task_id"][:24], "status": rec["status"], "s": rec.get("elapsed_s"),
                              "same_html": rec["info"].get("rerun_html_identical_to_first_run")}), flush=True)
        print(f"# chunk {c0 // a.chunk + 1}: {len(chunk)} in {time.monotonic() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
