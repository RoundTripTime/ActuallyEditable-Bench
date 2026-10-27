"""Experiment source, step 1: render every original blank with its native editor (Word / Hangul / LibreOffice).
Output: data/source_documents/<task>/native.pdf + native.json (renderer, pages, sha256). Resumable."""
import glob
import json
import sys
from pathlib import Path

from formbench.collection.discovery import blob_path
from formbench.formats.native_render import render_native
from formbench.util import DB, path, utcnow, write_json

OUT = Path(path("data", "source_documents"))


def main():
    db = DB()
    by_fmt = {}
    for f in sorted(glob.glob(str(path("data", "contracts_obs", "doc-*.json")))):
        c = json.load(open(f))
        d = OUT / Path(f).stem
        if (d / "native.json").exists() and json.load(open(d / "native.json")).get("status") == "ok":
            continue
        by_fmt.setdefault(c["format"], []).append(c)
    for fmt, cs in by_fmt.items():
        for i in range(0, len(cs), 40):                       # batches: one application session per 40 files
            chunk = cs[i:i + 40]
            items = [(blob_path(db.get_doc(c["template_id"])["sha256"]).read_bytes(), c["template_id"]) for c in chunk]
            res = render_native(fmt, items)
            for c in chunk:
                r = res.get(c["template_id"], {"status": "failed", "reason": "missing"})
                d = OUT / c["task_id"].replace("~", "__")
                d.mkdir(parents=True, exist_ok=True)
                if r.get("pdf"):
                    (d / "native.pdf").write_bytes(r.pop("pdf"))
                write_json(d / "native.json", {"task_id": c["task_id"], "format": fmt, "rendered_at": utcnow(), **r})
                print(c["task_id"], fmt, r["status"], r.get("pages"), r.get("reason", "")[:100], flush=True)


if __name__ == "__main__":
    sys.exit(main())
