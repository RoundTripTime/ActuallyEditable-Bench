"""Experiment current: the Word and Hangul PDF-import baselines on the current inputs (data/experiment_documents/<task>/inputs/vector_pdf.pdf).
Same conversion and record as scripts/reconstruction_helpers.py run_windows (labels vector_pdf__word_pdf, vector_pdf__hwp_pdf),
but 20 files per app_pdf_to_odt call (one application session per file as before; the registry set-up and the Word
crash-memory cleanup run once per call instead of once per file). Resumable. Serial (one Windows application at a time).
Usage: FORMBENCH_OBS=experiment_documents python scripts/reconstruct_apps.py [--baselines word_pdf,hwp_pdf]"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconstruction_helpers as source  # noqa: E402
from formbench.util import utcnow, write_json  # noqa: E402

INPUT = "vector_pdf"


def main() -> int:
    from formbench.formats.native_render import app_pdf_to_odt
    ap = argparse.ArgumentParser()
    ap.add_argument("--baselines", default="word_pdf,hwp_pdf")
    a = ap.parse_args()
    for b in a.baselines.split(","):
        app = {"word_pdf": "word", "hwp_pdf": "hwp"}[b]
        stem = f"{INPUT}__{b}"
        todo = [Path(t["dir"]) for t in source.tasks() if not (Path(t["dir"]) / "recon" / f"{stem}.json").exists()
                and (Path(t["dir"]) / "inputs" / f"{INPUT}.pdf").exists()]
        tid = {Path(t["dir"]): t["task_id"] for t in source.tasks()}
        print(f"# {b}: {len(todo)} tasks", flush=True)
        for i in range(0, len(todo), 20):
            chunk = todo[i:i + 20]
            t0 = time.monotonic()
            for _ in range(4):                                  # our previous instance may still be shutting down
                res = app_pdf_to_odt(app, [((d / "inputs" / f"{INPUT}.pdf").read_bytes(), f"f{j}") for j, d in enumerate(chunk)])
                if all(r["status"] != "blocked" for r in res.values()):
                    break
                time.sleep(20)
            for j, d in enumerate(chunk):
                r = res[f"f{j}"]
                if r["status"] == "blocked":                     # the user has the application open: stop, resume later
                    print(json.dumps({"task_id": tid[d], "baseline": b, "status": "blocked", "reason": r.get("reason")}), flush=True)
                    return 1
                rd = d / "recon"
                rd.mkdir(exist_ok=True)
                rec = {"task_id": tid[d], "input": INPUT, "baseline": b, "family": source.FAMILY[b], "started_at": utcnow(),
                       "status": r["status"], "reason": r.get("reason", ""), "info": {"converter": r.get("app"), "app_seconds": r.get("s")},
                       "elapsed_s": r.get("s")}
                if r["status"] == "ok":
                    (rd / f"{stem}.odt").write_bytes(r["odt"])
                    rec["sha256"] = r["sha256"]
                write_json(rd / f"{stem}.json", rec)
                if r["status"] != "ok":
                    print(json.dumps({"task_id": tid[d][:24], "baseline": b, "status": r["status"], "reason": rec["reason"][:120]},
                                     ensure_ascii=False), flush=True)
            print(f"# {b} chunk {i // 20 + 1}: {len(chunk)} in {time.monotonic() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
