"""Re-parse already visited pages from the HTTP cache after an extraction fix (no network).

Pages whose cached HTML now yields a same-format (blank, example) pair under the current link extraction are
re-queued for a normal visit (origin 'reparse'), so the pair enters the pipeline with fresh policy checks and logging.
Usage: python scripts/reparse_cached_pages.py [--host-like %.kr] [--limit N] [--dry-run]"""
from __future__ import annotations

import argparse
import json
import re
import sys

from formbench.collection.discovery import make_client
from formbench.collection.pair_explorer import add_seed_pages
from formbench.collection.search import PairedAttachmentProvider
from formbench.util import DB, load_yaml


FORMAT_HINT_RX = re.compile(rb"(hwpx|docx|\.odt|odt\b)", re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-like", default="%.kr")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = DB()
    cfg = load_yaml("configs/collection.yaml")
    client = make_client(db, cfg, True)
    formats = cfg.get("pair_formats")
    excluded = set(cfg.get("exclude_formats", []))
    rows = db.q("SELECT url FROM explore_pages WHERE host LIKE ? AND status LIKE 'ok%' AND coalesce(n_pair_docs,0)=0 "
                "ORDER BY url" + (f" LIMIT {int(a.limit)}" if a.limit else ""), (a.host_like,))
    stats = {"pages": len(rows), "cached": 0, "with_pairs": 0, "pair_docs": 0}
    hit_urls = []
    for r in rows:
        fr = client.fetch(r["url"], purpose="expand")          # offline client: cache only
        if fr.status != "ok" or not fr.content:
            continue
        stats["cached"] += 1
        if not FORMAT_HINT_RX.search(fr.content):               # no editable format named anywhere: no pair possible
            continue
        pairs = PairedAttachmentProvider.pairs_on_page(fr.content, r["url"], excluded, formats)
        if pairs:
            stats["with_pairs"] += 1
            stats["pair_docs"] += len(pairs)
            hit_urls.append(r["url"])
    if not a.dry_run and hit_urls:
        for u in hit_urls:
            db.x("DELETE FROM explore_pages WHERE url=?", (u,))
            db.x("DELETE FROM explore_queries WHERE query=?", (f"page:{u}",))
        add_seed_pages(db, hit_urls, origin="reparse")
    print(json.dumps({**stats, "requeued": 0 if a.dry_run else len(hit_urls), "sample": hit_urls[:10]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
