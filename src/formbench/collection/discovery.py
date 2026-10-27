"""discover -> policy_check -> fetch stages. Resumable, logged, round-robin over queries and providers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..corpus.security import MIME_BY_FORMAT, scan_bytes
from ..util import DB, Status, load_yaml, path, sha256_bytes, utcnow
from .fetch import Downloader, HttpClient
from .metadata import new_record
from .policy import PolicyGate
from .search import SearchHit, build_providers, extract_links


def load_queries(cfg: dict) -> list[str]:
    terms = load_yaml(path(*cfg.get("search_terms_file", "configs/search_terms.yaml").split("/")))
    q = list(terms.get("seeds", []))
    if cfg.get("use_combinations", True):
        q += list(terms.get("two_word_combinations", []))
    if cfg.get("use_language_variants", True):
        q += list(terms.get("language_variants", []))
    seen, out = set(), []
    for s in q:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def make_client(db: DB, cfg: dict, offline: bool = False, sleep=None) -> HttpClient:
    kw = {}
    if sleep is not None:
        kw["sleep"] = sleep
    client = HttpClient(db, contact=cfg.get("contact", "unset"), offline=offline,
                        max_bytes=int(cfg.get("max_bytes", 30 * 1024 * 1024)), timeout=float(cfg.get("timeout", 30)), **kw)
    client.policy = PolicyGate(db, fetch_robots=client._fetch_robots, offline=offline)
    return client


def _add_candidate(db: DB, hit: SearchHit, query: str, provider: str, parent: str | None, depth: int) -> bool:
    dom = (urlsplit(hit.url).hostname or "local").lower()
    exists = db.one("SELECT 1 FROM candidates WHERE url=?", (hit.url,)) is not None
    if not exists:
        db.x("INSERT INTO candidates(url,kind,source_domain,crawl_query,provider,discovered_at,parent_url,link_text,guessed_format,depth,extra) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (hit.url, hit.kind, dom, query, provider, utcnow(), parent, hit.title[:300], hit.guessed_format, depth,
              json.dumps(hit.extra, ensure_ascii=False)))
    return not exists


def discover(db: DB, cfg: dict, limit: int | None = None, dry_run: bool = False, offline: bool = False, resume: bool = True,
             client: HttpClient | None = None, log=print, provider: str | None = None, query: str | None = None) -> dict:
    """Round-robin: for query i, provider j — each (provider, query) is a resumable unit."""
    client = client or make_client(db, cfg, offline)
    providers = build_providers(client, cfg)
    queries = load_queries(cfg)
    per_q = int(cfg.get("results_per_query", 10))
    stats = {"queries_run": 0, "hits": 0, "new_candidates": 0, "provider_status": {}}
    units = [(q, p) for q in queries for p in providers
             if (not getattr(p, "cfg", {}).get("queries")) or q in p.cfg["queries"]]
    # providers with an explicit query list also run on queries outside the seed list
    for p in providers:
        for q in p.cfg.get("queries", []) or []:
            if (q, p) not in units:
                units.append((q, p))
    if provider:                                       # run one provider / one query only (parallel, independent units)
        units = [(q, p) for q, p in units if p.label == provider]
    if query:
        units = [(q, p) for q, p in units if q == query]
    if limit:
        units = units[:limit]
    for q, p in units:
        key = f"{p.label}::{q}"
        if resume and db.q_status("discover", key) in ("done", "blocked"):
            continue
        if dry_run:
            log(f"[dry-run] would query provider={p.label} q={q!r}")
            continue
        ok, why = p.available()
        if not ok:
            db.x("INSERT INTO query_log(provider,engine,query,ts,status,n_results,reason) VALUES(?,?,?,?,?,?,?)",
                 (p.label, p.engine, q, utcnow(), Status.BLOCKED_BY_ENVIRONMENT, 0, why))
            db.q_done("discover", key, "blocked", why)
            stats["provider_status"][p.label] = Status.BLOCKED_BY_ENVIRONMENT
            continue
        res = p.search(q, int(p.cfg.get("limit", per_q)))
        stats["queries_run"] += 1
        stats["provider_status"][p.label] = res.status
        db.x("INSERT INTO query_log(provider,engine,query,ts,status,n_results,reason) VALUES(?,?,?,?,?,?,?)",
             (p.label, res.engine, q, utcnow(), res.status, len(res.hits), res.reason))
        for h in res.hits:
            new = _add_candidate(db, h, q, p.label, None, 0)
            db.x("INSERT INTO search_log(provider,engine,query,rank,url,title,snippet,discovered_at,status,reason,is_duplicate) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (p.label, res.engine, q, h.rank, h.url, h.title[:300], h.snippet[:500], utcnow(),
                                                 "candidate", "", int(not new)))
            stats["hits"] += 1
            stats["new_candidates"] += int(new)
        db.q_done("discover", key, "done" if res.status == "ok" else "blocked" if res.status in ("blocked", "policy_denied") else "error",
                  res.reason)
        log(f"discover provider={p.label} q={q!r} status={res.status} hits={len(res.hits)} {res.reason}")
    return stats


def expand_pages(db: DB, cfg: dict, limit: int | None = None, dry_run: bool = False, offline: bool = False,
                 client: HttpClient | None = None, log=print) -> dict:
    """Recursive link discovery: fetch candidate pages (policy-gated) and enqueue document links found on them."""
    client = client or make_client(db, cfg, offline)
    max_depth = int(cfg.get("max_depth", 1))
    per_domain = int(cfg.get("max_pages_per_domain", 8))
    stats = {"pages": 0, "doc_links": 0, "denied": 0}
    rows = db.q("SELECT * FROM candidates WHERE kind='page' AND fetch_status IS NULL AND depth<=? ORDER BY depth, discovered_at, url",
                (max_depth,))
    domain_count: dict[str, int] = {}
    # round-robin across domains so one site cannot dominate the crawl budget
    by_dom: dict[str, list] = {}
    for r in rows:
        by_dom.setdefault(r["source_domain"], []).append(r)
    order = []
    while any(by_dom.values()):
        for d in sorted(by_dom):
            if by_dom[d]:
                order.append(by_dom[d].pop(0))
    if limit:
        order = order[:limit]
    total_budget = int(cfg.get("max_pages_total", 10 ** 9))
    done_before = db.one("SELECT count(*) AS n FROM candidates WHERE kind='page' AND fetch_status IS NOT NULL AND fetch_status!='skipped_budget'")["n"]
    for r in order:
        dom = r["source_domain"]
        if not dry_run and done_before + stats["pages"] + stats["denied"] >= total_budget:
            db.x("UPDATE candidates SET fetch_status='skipped_budget', policy_reason='global page budget reached' WHERE kind='page' AND fetch_status IS NULL")
            break
        if domain_count.get(dom, 0) >= per_domain:
            db.x("UPDATE candidates SET fetch_status=?, policy_reason=? WHERE url=?", ("skipped_budget", "per-domain page budget", r["url"]))
            continue
        if dry_run:
            log(f"[dry-run] would expand {r['url']}")
            continue
        domain_count[dom] = domain_count.get(dom, 0) + 1
        fr = client.fetch(r["url"], purpose="page")
        pol = fr.policy
        db.x("UPDATE candidates SET fetch_status=?, policy_status=?, policy_reason=? WHERE url=?",
             (fr.status, pol.decision if pol else None, (pol.reason if pol else fr.reason)[:300], r["url"]))
        if fr.status != "ok":
            stats["denied"] += 1
            log(f"expand {fr.status} {r['url']} :: {fr.reason}")
            continue
        stats["pages"] += 1
        if not (fr.content_type or "").lower().startswith(("text/html", "application/xhtml")) and b"<html" not in fr.content[:3000].lower():
            continue
        links = extract_links(fr.content, fr.final_url or r["url"])
        n = 0
        for h in links:
            if h.kind == "document":
                n += int(_add_candidate(db, h, r["crawl_query"], r["provider"], r["url"], r["depth"] + 1))
            elif r["depth"] + 1 <= max_depth and (urlsplit(h.url).hostname or "") == dom and _looks_like_form_page(h):
                _add_candidate(db, h, r["crawl_query"], r["provider"], r["url"], r["depth"] + 1)
        stats["doc_links"] += n
        log(f"expand ok {r['url']} doc_links={n}")
    return stats


_FORM_WORDS = ("서식", "양식", "신청서", "자료실", "form", "template", "별지", "다운로드", "download")


def _looks_like_form_page(h: SearchHit) -> bool:
    t = (h.title + " " + unquote(h.url)).lower()
    return any(w in t for w in _FORM_WORDS)


def fetch_documents(db: DB, cfg: dict, limit: int | None = None, dry_run: bool = False, offline: bool = False,
                    resume: bool = True, client: HttpClient | None = None, log=print) -> dict:
    client = client or make_client(db, cfg, offline)
    import os as _os
    sh = _os.environ.get("FORMBENCH_FETCH_SHARD", "")           # "i/N": fetch only the institutions of shard i (parallel fetch)
    shard = tuple(int(x) for x in sh.split("/")) if sh else None
    if not shard or shard[0] == 0:
        client.cleanup_partials()                      # one process cleans partial downloads
    dl = Downloader(client, run_av=bool(cfg.get("run_av", True)))
    per_domain = int(cfg.get("max_documents_per_domain", 60))
    per_group = int(cfg.get("max_documents_per_query_per_domain", 8))
    per_pair_page = int(cfg.get("max_documents_per_pair_page", 40))
    per_domain_groups = int(cfg.get("max_pair_groups_per_domain", 25))
    dom_groups: dict[str, set] = {}
    stats = {"ok": 0, "held": 0, "denied": 0, "failed": 0, "quarantined": 0, "duplicate": 0, "excluded_format": 0}
    excluded = set(cfg.get("exclude_formats", []) or [])
    pair_formats = set(cfg["pair_formats"]) if cfg.get("pair_formats") else None
    rows = db.q("SELECT * FROM candidates WHERE kind='document' ORDER BY discovered_at, url")
    for r in rows:                                     # groups already collected count against the cap
        if r["fetch_status"] == "ok":
            for lk in json.loads(r["extra"] or "{}").get("pair_links", []):
                dom_groups.setdefault(institution_key(lk["page"], cfg), set()).add(_canon_group(lk))

    by_key: dict[tuple, list] = {}
    for r in rows:
        by_key.setdefault((r["source_domain"], r["crawl_query"]), []).append(r)
    editable = {"hwpx", "docx", "odt"}
    for lst in by_key.values():                       # within a source, pairs with an editable side are fetched first
        lst.sort(key=lambda r: (0 if json.loads(r["extra"] or "{}").get("pair_links") and (
            r["guessed_format"] in editable or any(f in (lk.get("link_text") or "").lower() for lk in json.loads(r["extra"] or "{}").get("pair_links", [])
                                                     for f in editable)) else 1))
    order = []
    while any(by_key.values()):
        for k in sorted(by_key):
            if by_key[k]:
                order.append(by_key[k].pop(0))
    dom_n: dict[str, int] = {}
    grp_n: dict[tuple, int] = {}
    done = 0
    for r in order:
        if shard and _shard_of(r, cfg, shard[1]) != shard[0]:
            continue                                   # another fetch process owns this institution
        if r["fetch_status"] in (None, "", "error", "http_error") and cfg.get("exclude_countries"):
            from .pair_explorer import excluded_country
            if excluded_country(r["url"], cfg):        # user decision 2026-10-15: no new documents from excluded countries
                db.x("UPDATE candidates SET fetch_status='skipped_country' WHERE url=?", (r["url"],))
                continue
        if resume and r["fetch_status"] not in (None, "", "error", "http_error"):
            if r["fetch_status"] == "ok":
                dom_n[r["source_domain"]] = dom_n.get(r["source_domain"], 0) + 1
                grp_n[(r["source_domain"], r["crawl_query"])] = grp_n.get((r["source_domain"], r["crawl_query"]), 0) + 1
            continue
        if limit and done >= limit:
            break
        dom, gk = r["source_domain"], (r["source_domain"], r["crawl_query"])
        pair_links = json.loads(r["extra"] or "{}").get("pair_links") or []
        limit_g = per_group
        if pair_links:                                 # observed pairs: the budget unit is the pair page, so pairs stay whole
            gk = (r["source_domain"], pair_links[0]["page"])
            limit_g = per_pair_page
            inst = institution_key(pair_links[0]["page"], cfg)
            groups = dom_groups.setdefault(inst, set())
            new_groups = {_canon_group(lk) for lk in pair_links} - groups
            if new_groups and len(groups) >= per_domain_groups:      # source diversity: one site may not dominate the real pairs
                db.x("UPDATE candidates SET fetch_status=?, policy_reason=? WHERE url=?",
                     ("skipped_budget", f"diversity cap: {per_domain_groups} pair groups per source domain", r["url"]))
                continue
        if dom_n.get(dom, 0) >= per_domain or grp_n.get(gk, 0) >= limit_g:
            db.x("UPDATE candidates SET fetch_status=?, policy_reason=? WHERE url=?", ("skipped_budget", "per-domain/query document budget (bias control)", r["url"]))
            continue
        if pair_links and pair_formats is not None and r["guessed_format"] not in pair_formats:
            db.x("UPDATE candidates SET fetch_status=?, policy_reason=? WHERE url=?", ("excluded_pair_format", f"pair file format {r['guessed_format']} not in pair_formats", r["url"]))
            continue
        if r["guessed_format"] in excluded:
            db.x("UPDATE candidates SET fetch_status=?, policy_reason=? WHERE url=?", ("excluded_format", f"format {r['guessed_format']} excluded by collection config", r["url"]))
            continue
        if pair_links:
            dom_groups.setdefault(institution_key(pair_links[0]["page"], cfg), set()).update(_canon_group(lk) for lk in pair_links)
        if dry_run:
            log(f"[dry-run] would fetch {r['url']}")
            continue
        done += 1
        extra = json.loads(r["extra"] or "{}")
        if extra.get("captured_file"):              # S4.5: file a page generated from a download button (no file URL)
            info = _import_local(client, Path(extra["captured_file"]).resolve().as_uri(), extra)
            info["policy"] = client.policy.check(extra.get("landing_url") or r["url"], purpose="page").to_json()
            info["filename"] = extra.get("captured_name") or info.get("filename")
        elif r["url"].startswith("file://"):
            info = _import_local(client, r["url"], extra)
        else:
            info = dl.download(r["url"], referer=r["parent_url"] or extra.get("landing_url"), declared_name=r["link_text"])
        st = info["fetch_status"]
        pol = info.get("policy") or {}
        doc_id = None
        if st == "ok" and (info.get("original_format") in excluded or
                           (pair_links and pair_formats is not None and info.get("original_format") not in pair_formats)):   # content signature
            st = info["fetch_status"] = "excluded_format"
            info["reason"] = f"content is {info['original_format']} (excluded by collection config); bytes not kept"
            if db.one("SELECT 1 FROM documents WHERE sha256=?", (info["sha256"],)) is None:
                client.blob_path(info["sha256"]).unlink(missing_ok=True)
            stats["excluded_format"] += 1
        if st == "ok":
            doc_id = "doc-" + info["sha256"][:16]
            existing = db.get_doc(doc_id)
            if existing:
                stats["duplicate"] += 1
                srcs = existing.setdefault("alternate_sources", [])
                if r["url"] != existing["source_url"] and r["url"] not in srcs:
                    srcs.append(r["url"])
                links = existing.setdefault("observed_pair_links", [])
                links.extend(lk for lk in extra.get("pair_links", []) if lk not in links)
                db.put_doc(existing)
            else:
                rights = extra.get("rights") or {}
                lic_spdx = extra.get("license_spdx") or extra.get("license_short")
                rights_status = rights.get("rights_status") or pol.get("rights_status") or "unknown"
                redistributable = bool(rights.get("redistribution_allowed", pol.get("redistribution_allowed", False)))
                if lic_spdx:
                    ok_lic = _license_allows_redistribution(str(lic_spdx))
                    rights_status = f"licensed:{lic_spdx}"
                    redistributable = ok_lic
                rec = new_record(
                    document_id=doc_id, source_url=r["url"], source_domain=r["source_domain"], crawl_query=r["crawl_query"],
                    discovered_at=r["discovered_at"], downloaded_at=info["downloaded_at"], original_format=info["original_format"],
                    mime_type=info["mime_type"], sha256=info["sha256"], file_size=info["file_size"],
                    license_url=extra.get("license_url") or rights.get("license_url") or pol.get("license_url"),
                    rights_status=rights_status, redistribution_allowed=redistributable,
                    robots_result=pol.get("robots_result", "not_applicable_local"), tos_status=pol.get("tos_status", rights.get("tos_status", "not_applicable_local")),
                    error_code=None)
                rec.update({"filename": info.get("filename"), "provider": r["provider"], "landing_url": extra.get("landing_url") or r["parent_url"],
                            "link_text": r["link_text"], "security": info.get("security"), "form_group": extra.get("form_group"),
                            "publicly_accessible": pol.get("publicly_accessible", not r["url"].startswith("file://")),
                            "declared_content_type": info.get("content_type"), "alternate_sources": [],
                            "observed_pair_links": extra.get("pair_links", [])})
                db.put_doc(rec)
                stats["ok"] += 1
            dom_n[dom] = dom_n.get(dom, 0) + 1
            grp_n[gk] = grp_n.get(gk, 0) + 1
        elif st == "policy_review":
            stats["held"] += 1
        elif st in ("policy_denied", "blocked"):
            stats["denied"] += 1
        elif st == Status.QUARANTINED:
            stats["quarantined"] += 1
        else:
            stats["failed"] += 1
        db.x("UPDATE candidates SET fetch_status=?, policy_status=?, policy_reason=?, document_id=? WHERE url=?",
             (st, pol.get("decision"), (pol.get("reason") or info.get("reason") or "")[:300], doc_id, r["url"]))
        log(f"fetch {st} {r['url'][:110]} {info.get('original_format', '')} {info.get('reason', '')[:80]}")
    return stats


def _shard_of(r, cfg: dict, n: int) -> int:
    """Stable shard of a candidate: its publishing institution (first pair page) or its source domain, so that one
    institution's caps and one domain's request pacing stay inside one fetch process."""
    import hashlib
    links = json.loads(r["extra"] or "{}").get("pair_links") or []
    key = institution_key(links[0]["page"], cfg) if links else (r["source_domain"] or "")
    return int(hashlib.sha1(key.encode()).hexdigest(), 16) % n


def _canon_group(lk: dict) -> str:
    """Pair group without the list state of the post URL (one post reached from many list pages is one group)."""
    from .search import canonical_post_url
    return f"{canonical_post_url(lk['page'])}#{lk.get('key', '')}"


def institution_key(url: str, cfg: dict) -> str:
    """Publishing institution of a page: the host, or host + first path segment on hosts shared by many offices
    (prefectural labour bureaus, legal affairs bureaus, diplomatic missions)."""
    sp = urlsplit(url)
    host = (sp.hostname or "").lower()
    depth = (cfg.get("multi_institution_path_depth") or {}).get(host)
    if depth:                                          # e.g. www-ws.gov.taipei/001/Upload/<agency>/...: 3 segments name the agency
        seg = [x for x in sp.path.split("/") if x]
        return "/".join([host] + seg[: int(depth)])
    if host in set(cfg.get("multi_institution_hosts", []) or []):
        seg = [x for x in sp.path.split("/") if x]
        return f"{host}/{seg[0]}" if seg else host
    return host


def _license_allows_redistribution(lic: str) -> bool:
    l = lic.lower()
    return any(k in l for k in ("mit", "apache", "bsd", "cc0", "cc-by", "cc by", "public domain", "pd", "kogl", "isc", "unlicense", "mpl"))


def _import_local(client: HttpClient, url: str, extra: dict) -> dict:
    p = Path(unquote(urlsplit(url).path))
    out = {"url": url, "downloaded_at": utcnow(), "policy": None, "reason": ""}
    try:
        data = p.read_bytes()
    except OSError as e:
        return {**out, "fetch_status": "error", "reason": str(e)}
    if len(data) > client.max_bytes:
        return {**out, "fetch_status": "too_large", "reason": "local file exceeds size limit"}
    rep = scan_bytes(data, declared_ext=p.suffix)
    sha = sha256_bytes(data)
    info = {**out, "sha256": sha, "file_size": len(data), "filename": p.name, "content_type": None, "final_url": url,
            "original_format": rep.format, "mime_type": MIME_BY_FORMAT.get(rep.format, "application/octet-stream"),
            "security": rep.to_json(), "fetch_status": "ok"}
    if not rep.ok:
        qdir = path("data", "quarantine")
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / sha).write_bytes(data)
        info["fetch_status"] = Status.QUARANTINED
        info["reason"] = "; ".join(f["code"] for f in rep.findings if f["severity"] == "block")
        return info
    client._store(data)
    return info


def apply_takedown(db: DB, pattern: str, reason: str, log=print) -> dict:
    """Propagate a deletion request to raw bytes, caches, derived files and public lists (AGENTS.md §2, test #14)."""
    import fnmatch
    import shutil
    PolicyGate(db, config={"default": {}}).add_takedown(pattern, reason)
    removed = {"documents": 0, "blobs": 0, "derived": 0, "candidates": 0}
    for rec in db.docs():
        urls = [rec.get("source_url") or ""] + list(rec.get("alternate_sources") or [])
        if any(fnmatch.fnmatch(u, pattern) or pattern in u for u in urls):
            sha = rec.get("sha256") or ""
            for base in (path("data", "raw", "sha256"), path("data", "cache", "http")):
                bp = base / sha[:2] / sha
                if bp.exists():
                    bp.unlink()
                    removed["blobs"] += 1
            for sub in ("normalized", "derived", "pairs", "contracts"):
                root = path("data", sub)
                if root.exists():
                    for f in list(root.rglob(f"*{rec['document_id']}*")):
                        if f.is_dir():
                            shutil.rmtree(f, ignore_errors=True)
                        else:
                            f.unlink(missing_ok=True)
                        removed["derived"] += 1
            db.x("DELETE FROM documents WHERE document_id=?", (rec["document_id"],))
            db.x("DELETE FROM pairs WHERE template_id=? OR completed_id=?", (rec["document_id"], rec["document_id"]))
            removed["documents"] += 1
    cur = db.x("UPDATE candidates SET fetch_status='takedown', policy_status='deny', policy_reason=? WHERE url LIKE ?",
               (f"takedown: {reason}", "%" + pattern.strip("*") + "%"))
    removed["candidates"] = cur.rowcount
    db.x("DELETE FROM http_cache WHERE url LIKE ?", ("%" + pattern.strip("*") + "%",))
    log(f"takedown {pattern!r}: {removed}")
    return removed


def blob_path(sha: str) -> Path:
    return path("data", "raw", "sha256") / sha[:2] / sha


def doc_bytes(rec: dict) -> bytes:
    return blob_path(rec["sha256"]).read_bytes()


def export_ext(rec: dict) -> str:
    return {"jpeg": ".jpg", "tiff": ".tif"}.get(rec["original_format"], "." + (rec["original_format"] or "bin"))


__all__ = ["discover", "expand_pages", "fetch_documents", "apply_takedown", "load_queries", "make_client", "blob_path",
           "doc_bytes", "export_ext", "os"]


def relink_pair_pages(db: DB, cfg: dict, offline: bool = False, log=print) -> dict:
    """Recompute (blank, example) links of every known pair page with the current matching rules and rewrite the
    links stored on candidates and documents (old links of a page are replaced, never merged)."""
    from .search import PairedAttachmentProvider
    client = make_client(db, cfg, offline)
    excluded = cfg.get("exclude_formats", []) or []
    pages: set[str] = set()
    for r in db.q("SELECT extra FROM candidates WHERE provider LIKE 'observed_pair%'"):
        pages.update(lk["page"] for lk in json.loads(r["extra"] or "{}").get("pair_links", []))
    fresh: dict[str, list] = {}
    stats = {"pages": len(pages), "fetched": 0, "failed": 0, "links": 0}
    for page in sorted(pages):
        fr = client.fetch(page, purpose="expand")
        if fr.status != "ok":
            stats["failed"] += 1
            continue
        stats["fetched"] += 1
        for h in PairedAttachmentProvider.pairs_on_page(fr.content, page, excluded, cfg.get("pair_formats")):
            fresh.setdefault(h.url, []).extend(h.extra["pair_links"])
    done_pages = pages                                  # a page that failed to load keeps no stale links either
    for r in db.q("SELECT url, extra, document_id FROM candidates WHERE provider LIKE 'observed_pair%'"):
        x = json.loads(r["extra"] or "{}")
        x["pair_links"] = [lk for lk in x.get("pair_links", []) if lk["page"] not in done_pages] + fresh.get(r["url"], [])
        db.x("UPDATE candidates SET extra=? WHERE url=?", (json.dumps(x, ensure_ascii=False), r["url"]))
        stats["links"] += len(x["pair_links"])
    for d in db.docs():
        if not d.get("observed_pair_links"):
            continue
        urls = [d.get("source_url")] + list(d.get("alternate_sources") or [])
        new = [lk for lk in d["observed_pair_links"] if lk["page"] not in done_pages]
        for u in urls:
            new.extend(lk for lk in fresh.get(u, []) if lk not in new)
        d["observed_pair_links"] = new
        db.put_doc(d)
    log(f"relink pages={stats['pages']} fetched={stats['fetched']} failed={stats['failed']}")
    return stats
