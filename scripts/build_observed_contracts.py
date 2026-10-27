"""Build task contracts from all verified observed pairs (contracts/observed.py) and freeze them.

Writes data/contracts_obs/<pair_id>.json and data/contracts_obs/manifest.json (contract hashes, statuses).
Usage: python scripts/build_observed_contracts.py [--limit N] [--only PAIR_ID] [--out DIR] [--workers N]
(experiment source: --out observed_contracts, the current ODT reader; v2's data/contracts_obs stays as it was)"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

from formbench.collection.discovery import blob_path
from formbench.completion.sessions import open_session
from formbench.contracts.observed import EXTRACTION_VERSION, build_observed_contract
from formbench.util import DB, jhash, path, utcnow, write_json


def _one(r: dict, out_dir: str):
    db = DB()
    t, e = db.get_doc(r["template_id"]), db.get_doc(r["completed_id"])
    fmt = t["original_format"]
    meta = {"pair_id": r["pair_id"], "template_id": r["template_id"], "completed_id": r["completed_id"], "format": fmt,
            "template_sha256": t["sha256"], "language": t.get("language"), "source_domain": t.get("source_domain"),
            "template_family_id": t.get("template_family_id") or r["template_id"]}
    t0 = time.time()
    try:
        bs = open_session(blob_path(t["sha256"]).read_bytes(), fmt)
        es = open_session(blob_path(e["sha256"]).read_bytes(), e["original_format"])
        c = build_observed_contract(r["pair_id"], bs, es, meta)
    except Exception as ex:  # noqa: BLE001 - recorded as a failed extraction, never skipped silently
        c = {"schema_version": "2.0", "task_id": r["pair_id"], **meta, "status": "extraction_failed",
             "error": f"{type(ex).__name__}: {ex}"[:400], "targets": [], "rows": [], "unsupported": []}
        c["contract_hash"] = jhash(c)
    c["extraction_seconds"] = round(time.time() - t0, 2)
    write_json(Path(out_dir) / f"{r['pair_id'].replace('~', '__')}.json", c)
    print(f"{r['pair_id']} {fmt} {c['status']} targets={len(c.get('targets', []))} rows={len(c.get('rows', []))} "
          f"unsupported={len(c.get('unsupported', []))} conf={c.get('alignment_confidence')} {c['extraction_seconds']}s", flush=True)
    return r["pair_id"], c, fmt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default="contracts_obs")
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()
    db = DB()
    rows = db.q("SELECT pair_id, template_id, completed_id FROM pairs WHERE pair_type='observed_verified' ORDER BY pair_id")
    if a.only:
        rows = [r for r in rows if r["pair_id"] == a.only]
    if a.limit:
        rows = rows[: a.limit]
    out_dir = Path(path("data", a.out))
    out_dir.mkdir(parents=True, exist_ok=True)
    man = {"extraction_version": EXTRACTION_VERSION, "built_at": utcnow(), "contracts": {}}
    st = collections.Counter()
    if a.workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            results = list(ex.map(_one, [dict(r) for r in rows], [str(out_dir)] * len(rows)))
    else:
        results = [_one(dict(r), str(out_dir)) for r in rows]
    for pair_id, c, fmt in results:
        man["contracts"][pair_id] = {"status": c["status"], "hash": c["contract_hash"], "format": fmt,
                                     "targets": len(c.get("targets", [])), "unsupported": len(c.get("unsupported", [])),
                                     "confidence": c.get("alignment_confidence")}
        st[c["status"]] += 1
    man["by_status"] = dict(st)
    man["manifest_hash"] = jhash(man["contracts"])
    write_json(out_dir / "manifest.json", man)
    print(json.dumps(man["by_status"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
