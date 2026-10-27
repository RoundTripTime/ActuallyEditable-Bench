"""Measured yield of every collection strategy (reports/coverage/collection_strategy_metrics.{json,md}).

The strategy tree (reports/coverage/collection_strategy_tree.md) says what was tried; this report says what each try
returned, computed from the database only: pages visited per discovery origin, pages showing publisher examples,
pages with same-format pairs, candidates and pairs per evidence kind, query distribution, coverage per
(country, sector) cell. Re-running it on the same database gives the same numbers."""
from __future__ import annotations

import collections
import json

from ..util import DB, path, utcnow, write_text
from .pair_explorer import SCHEMA as EXPLORE_SCHEMA, coverage, coverage_cell

ORIGIN_STRATEGY = {
    "websearch": "S1.1b assistant WebSearch seeds", "gap": "S1.1b WebSearch seeds for unexplored cells",
    "user": "S1.6 user-nominated", "seed:": "S1.1a/S2.1 search-engine seed query", "site:": "S1.1a site: query",
    "learned:": "S2.3 learned query", "neighbour": "S3.1 board / pagination neighbours", "hub": "S1.3 hub crawl below an institution root",
    "site_index": "S3.2 site index re-visit", "site_index2": "S3.2 site index re-visit", "reanalyse": "S4.3 re-analysis",
    "sitemap": "S3.3 sitemap pages of productive templates", "render_retry": "S4.4 headless render re-visit", "retry": "S5.2 retry",
    "robots_override": "S5.4 robots override re-visit", "open_data": "S1.5 open-data form pages", "open_data_host": "S1.5 open-data hosts",
    "directory_tw": "S1.3 institution directory (TW)", "directory_jp": "S1.3 institution directory (JP)",
    "directory_kr": "S1.3 institution directory (KR)", "cc_host": "S1.2b Common Crawl host enumeration",
    "ccsite:": "S1.2e site: query on Common Crawl document hosts", "ccname:": "S1.2d search for a labelled file name",
    "dirsite:": "S1.3b site: query on directory institutions",
}


def _strategy(origin: str) -> str:
    for k, v in ORIGIN_STRATEGY.items():
        if origin == k or (k.endswith(":") and origin.startswith(k)):
            return v
    return origin or "unknown"


def build(db: DB | None = None) -> dict:
    db = db or DB()
    db.conn.executescript(EXPLORE_SCHEMA)
    by_origin: dict = collections.defaultdict(lambda: {"pages": 0, "hosts": set(), "pages_with_examples": 0, "pages_with_pairs": 0,
                                                       "pair_documents": 0, "rendered": 0, "errors": 0})
    for r in db.q("SELECT origin, host, status, n_examples, n_pair_docs FROM explore_pages"):
        o = by_origin[_strategy(r["origin"])]
        o["pages"] += 1
        o["hosts"].add(r["host"])
        o["pages_with_examples"] += 1 if (r["n_examples"] or 0) > 0 else 0
        o["pages_with_pairs"] += 1 if (r["n_pair_docs"] or 0) > 0 else 0
        o["pair_documents"] += r["n_pair_docs"] or 0
        o["rendered"] += 1 if r["status"] == "ok_rendered" else 0
        o["errors"] += 1 if r["status"] in ("error", "http_error", "blocked") else 0
    origins = sorted(({"strategy": k, **{**v, "hosts": len(v["hosts"])}} for k, v in by_origin.items()), key=lambda x: (-x["pair_documents"], x["strategy"]))
    queries = [dict(r) for r in db.q("SELECT engine, CASE WHEN instr(origin, ':') > 0 THEN substr(origin, 1, instr(origin, ':')) ELSE origin END AS origin_kind, "
                                     "status, count(*) n, sum(n_pair_pages) pair_pages FROM explore_queries GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")]
    langs = collections.Counter()
    for r in db.q("SELECT origin FROM explore_queries WHERE engine != 'direct'"):
        langs[r["origin"].split(":", 1)[1] if ":" in r["origin"] else "-"] += 1
    cands = [dict(r) for r in db.q("SELECT provider, count(*) n, sum(fetch_status='ok') fetched FROM candidates GROUP BY provider ORDER BY n DESC")]
    pairs_by_relation: dict = collections.defaultdict(collections.Counter)
    pairs_by_cell: dict = collections.defaultdict(collections.Counter)
    for r in db.q("SELECT pair_type, evidence FROM pairs WHERE pair_type LIKE 'observed%'"):
        ev = json.loads(r["evidence"] or "{}")
        pairs_by_relation[str(ev.get("name_relation", "?")).split(":", 1)[0]][r["pair_type"]] += 1
        from urllib.parse import urlsplit
        host = (urlsplit(ev.get("page") or "").hostname or "").lower()
        pairs_by_cell["/".join(coverage_cell(host))][r["pair_type"]] += 1
    cc = {}
    try:
        cc = {"files": db.one("SELECT count(*) n FROM cc_files")["n"], "hosts": db.one("SELECT count(DISTINCT host) n FROM cc_files")["n"],
              "labelled_files": db.one("SELECT coalesce(sum(labelled),0) n FROM cc_files")["n"],
              "patterns": [dict(r) for r in db.q("SELECT pattern, sum(status='ok') ok_pages, sum(status='failed') failed_pages, sum(n) rows "
                                                  "FROM cc_scans GROUP BY pattern ORDER BY pattern")]}
    except Exception:      # noqa: BLE001 - index tables absent before the first scan
        pass
    out = {"generated_at": utcnow(), "origins": origins, "queries": queries, "query_languages": dict(langs), "candidates_by_provider": cands,
           "pairs_by_relation": {k: dict(v) for k, v in pairs_by_relation.items()}, "pairs_by_cell": {k: dict(v) for k, v in pairs_by_cell.items()},
           "coverage_cells": coverage(db), "common_crawl": cc}
    d = path("reports", "coverage")
    write_text(d / "collection_strategy_metrics.json", json.dumps(out, ensure_ascii=False, indent=1))
    write_text(d / "collection_strategy_metrics.md", _md(out))
    return {"origins": len(origins), "file": str(d / "collection_strategy_metrics.md")}


def _md(o: dict) -> str:
    L = ["# Collection strategy metrics", "", f"generated {o['generated_at']} from the project database (`formbench collect strategy-report`).",
         "Companion of `collection_strategy_tree.md`: what each strategy returned.", "",
         "## Pages by discovery strategy", "", "| strategy | pages | hosts | pages with examples | pages with same-format pairs | pair documents | rendered | errors |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in o["origins"]:
        L.append(f"| {r['strategy']} | {r['pages']} | {r['hosts']} | {r['pages_with_examples']} | {r['pages_with_pairs']} | {r['pair_documents']} | {r['rendered']} | {r['errors']} |")
    L += ["", "## Observed pairs by evidence kind", "", "| relation | candidate | verified | rejected |", "|---|---:|---:|---:|"]
    for k, v in sorted(o["pairs_by_relation"].items()):
        L.append(f"| {k} | {v.get('observed_candidate', 0)} | {v.get('observed_verified', 0)} | {v.get('observed_rejected', 0)} |")
    L += ["", "## Observed pairs by (country / sector) of the evidence page", "", "| cell | candidate | verified | rejected |", "|---|---:|---:|---:|"]
    for k, v in sorted(o["pairs_by_cell"].items(), key=lambda kv: -sum(kv[1].values())):
        L.append(f"| {k} | {v.get('observed_candidate', 0)} | {v.get('observed_verified', 0)} | {v.get('observed_rejected', 0)} |")
    L += ["", "## Exploration coverage per (country, sector)", "", "| country | sector | hosts | pages | examples | pair docs | pending |", "|---|---|---:|---:|---:|---:|---:|"]
    for c in o["coverage_cells"]:
        L.append(f"| {c['country']} | {c['sector']} | {c['hosts']} | {c['pages']} | {c['examples']} | {c['pair_docs']} | {c['pending']} |")
    L += ["", "## Search queries", "", "| engine | origin | status | n | pair pages |", "|---|---|---|---:|---:|"]
    for q in o["queries"]:
        L.append(f"| {q['engine']} | {q['origin_kind']} | {q['status']} | {q['n']} | {q['pair_pages'] or 0} |")
    L += ["", f"Engine queries by language: {json.dumps(o['query_languages'], ensure_ascii=False)}", "", "## Candidates by provider", "",
          "| provider | candidates | fetched |", "|---|---:|---:|"]
    for c in o["candidates_by_provider"]:
        L.append(f"| {c['provider']} | {c['n']} | {c['fetched'] or 0} |")
    cc = o.get("common_crawl") or {}
    if cc:
        L += ["", "## Common Crawl index (S1.2)", "", f"editable files {cc['files']}, hosts {cc['hosts']}, files with an example label in the name {cc['labelled_files']}", "",
              "| pattern | ok pages | failed pages | rows |", "|---|---:|---:|---:|"]
        for p in cc["patterns"]:
            L.append(f"| {p['pattern']} | {p['ok_pages']} | {p['failed_pages']} | {p['rows']} |")
    return "\n".join(L) + "\n"
