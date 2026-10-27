"""Experiment edit, metric ①: visual similarity of the edit reconstruction labels, rendered by LibreOffice with the host's
Windows and Hancom fonts (experiment §6). Scores made before the font change are recomputed (fonts_version tag).
Reuses scripts/visual_helpers.py (batch rendering, SSIM / blurred SSIM / ink IoU / CER vs. the native rendering)."""
import argparse
import glob
import importlib.util
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from formbench.util import obs_name, path

FONTS = "host-fonts-2026-10-17"
LABELS = ["oracle__template_odt", "png_150__image_only", "vector_pdf__word_pdf", "vector_pdf__hwp_pdf", "vector_pdf__docling_odt",
          "vector_pdf__mineru", "png_150__gpt6luna_odt"]
HERE = Path(__file__).resolve().parent


def stale(odt: Path) -> bool:
    v = odt.with_suffix(".visual.json")
    if not v.exists():
        return True
    return json.loads(v.read_text()).get("fonts") != FONTS


def batch(odts):
    spec = importlib.util.spec_from_file_location("sourcev", HERE / "visual_helpers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for o in odts:                                  # old renderings were made with the Linux substitute fonts
        Path(o[:-4] + ".lo.pdf").unlink(missing_ok=True)
    res = m.batch(odts)
    for o in odts:
        v = Path(o[:-4] + ".visual.json")
        d = json.loads(v.read_text())
        d["fonts"] = FONTS
        v.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    todo = [f for lab in LABELS for f in sorted(glob.glob(str(path("data", obs_name(), "*", "recon", f"{lab}.odt")))) if stale(Path(f))]
    print(f"# {len(todo)} reconstructions to score", flush=True)
    chunks = [todo[i:i + 25] for i in range(0, len(todo), 25)]
    k = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(batch, c) for c in chunks]):
            k += len(fut.result())
            print(f"{k}/{len(todo)}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
