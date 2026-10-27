"""Common Crawl URL index as a host- and file-discovery source (strategy S1.2a/b in
reports/coverage/collection_strategy_tree.md).

The CDX server (index.commoncrawl.org) is a public API that Common Crawl documents for programmatic queries; its
robots.txt keeps search engines out of the HTML. We query it slowly (one request at a time, a pause between pages),
cache every page under data/cache/cc/ and never fetch WARC contents: the index only tells *which hosts serve editable
documents* and *which file URLs carry a publisher's example label*. Pages are then visited by the normal explorer.

Nothing here confirms a pair. Labelled file names become seed pages (their directory / host) for the explorer and
file-name pairs (S4.5) are kept as hints with their own evidence grade."""
from __future__ import annotations

import collections
import json
import re
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit

import requests

from ..util import DB, path, utcnow
from .search import EXAMPLE_RX, pair_key

API = "https://index.commoncrawl.org"
UA = "formbench-research/1.0 (academic benchmark of editable forms; low rate)"
EDITABLE_MIME_RX = r".*(opendocument\.text|wordprocessingml|hwp).*"
FORMAT_OF = [("opendocument.text", "odt"), ("wordprocessingml", "docx"), ("hwp", "hwpx")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_files(url TEXT PRIMARY KEY, host TEXT, tld TEXT, crawl TEXT, mime TEXT, format TEXT, ts TEXT,
  labelled INTEGER, name TEXT, key TEXT);
CREATE TABLE IF NOT EXISTS cc_scans(crawl TEXT, pattern TEXT, page INTEGER, n INTEGER, status TEXT, ts TEXT, PRIMARY KEY(crawl, pattern, page));
"""


def _fmt(mime: str, url: str) -> str | None:
    m = (mime or "").lower()
    for k, f in FORMAT_OF:
        if k in m:
            return f
    low = urlsplit(url).path.lower()
    return next((f for f in ("hwpx", "docx", "odt") if low.endswith("." + f)), None)


def file_name(url: str) -> str:
    sp = urlsplit(url)
    name = unquote(sp.path.rsplit("/", 1)[-1])
    for m in re.finditer(r"(?:^|&)(?:filename|filedisplay|fn|file_name|name|orgfilename|FileName)=([^&]+)", sp.query, re.I):
        name = unquote(m.group(1).replace("+", " "))
    return unicodedata.normalize("NFKC", name)


class CCIndex:
    def __init__(self, db: DB | None = None, crawl: str | None = None, pause_s: float = 2.0, log=print):
        self.db = db or DB()
        self.db.conn.executescript(SCHEMA)
        self.pause_s, self.log = pause_s, log
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.crawl = crawl or self.latest()

    def latest(self) -> str:
        p = path("data", "cache", "cc", "collinfo.json")
        if p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
            return json.loads(p.read_text())[0]["id"]
        r = self.s.get(f"{API}/collinfo.json", timeout=60)
        r.raise_for_status()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(r.text)
        return r.json()[0]["id"]

    def _get(self, params: dict, tries: int = 4) -> requests.Response | None:
        url = f"{API}/{self.crawl}-index"
        for k in range(tries):
            try:
                r = self.s.get(url, params=params, timeout=180)
            except requests.RequestException as e:
                self.log(f"cc: {e.__class__.__name__}; retry {k + 1}")
                time.sleep(15 * (k + 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:                   # "No Captures found"
                return r
            if r.status_code in (429, 502, 503, 504):
                time.sleep(30 * (k + 1))               # overloaded index server: back off
                continue
            self.log(f"cc: http {r.status_code} for {params.get('url')} page {params.get('page')}")
            time.sleep(10)
        return None

    def _num_pages(self, pattern: str) -> int | None:
        r = self._get({"url": pattern, "showNumPages": "true"}, tries=6)
        if r is None or r.status_code != 200:
            return None
        try:
            return int(r.json().get("pages", 0))
        except ValueError:
            return None

    def _page_text(self, pattern: str, page: int, mime_rx: str) -> str | None:
        """One index page; a truncated transfer (last line not JSON) is discarded and fetched again."""
        cache = path("data", "cache", "cc", self.crawl, quote(pattern, safe="") + f".p{page}.jsonl")
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        for _ in range(3):
            r = self._get({"url": pattern, "output": "json", "page": page, "filter": f"~mime-detected:{mime_rx}"})
            time.sleep(self.pause_s)
            if r is None:
                return None
            text = r.text if r.status_code == 200 else ""
            lines = [x for x in text.splitlines() if x.strip()]
            try:
                for x in lines:
                    json.loads(x)
            except ValueError:
                self.log(f"cc: truncated page {page} of {pattern}; fetching again")
                continue
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
            return text
        return None

    def scan(self, pattern: str, mime_rx: str = EDITABLE_MIME_RX, max_pages: int | None = None) -> dict:
        """All captures of ``pattern`` (e.g. ``*.gov.tw``) whose detected MIME is an editable document. Resumable:
        pages already stored in cc_scans are skipped; failed pages get a second pass at the end."""
        pages = self._num_pages(pattern)
        if pages is None:
            self.db.x("INSERT OR REPLACE INTO cc_scans VALUES(?,?,?,?,?,?)", (self.crawl, pattern, -1, 0, "failed", utcnow()))
            return {"pattern": pattern, "status": "failed", "reason": "page count unavailable"}
        n_new, failed = 0, []
        todo = list(range(pages if max_pages is None else min(pages, max_pages)))
        for attempt in (1, 2):
            for page in todo:
                if self.db.one("SELECT 1 FROM cc_scans WHERE crawl=? AND pattern=? AND page=? AND status='ok'", (self.crawl, pattern, page)):
                    continue
                text = self._page_text(pattern, page, mime_rx)
                if text is None:
                    self.db.x("INSERT OR REPLACE INTO cc_scans VALUES(?,?,?,?,?,?)", (self.crawl, pattern, page, 0, "failed", utcnow()))
                    failed.append(page)
                    continue
                n_new += self._store(text)
                self.db.x("INSERT OR REPLACE INTO cc_scans VALUES(?,?,?,?,?,?)", (self.crawl, pattern, page, text.count("\n"), "ok", utcnow()))
            if not failed:
                break
            todo, failed = failed, []
            time.sleep(120)
        return {"pattern": pattern, "pages": pages, "new_files": n_new, "failed_pages": failed, "crawl": self.crawl}

    def _store(self, text: str) -> int:
        n_new = 0
        rows = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        for row in rows:
            url = row["url"]
            fmt = _fmt(row.get("mime-detected") or row.get("mime"), url)
            if not fmt or str(row.get("status", "200")) != "200":
                continue
            name = file_name(url)
            host = (urlsplit(url).hostname or "").lower()
            labelled = 1 if EXAMPLE_RX.search(name) else 0
            n_new += self.db.x("INSERT OR IGNORE INTO cc_files VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (url, host, host.rsplit(".", 1)[-1], self.crawl, row.get("mime-detected"), fmt, row.get("timestamp"),
                                labelled, name, pair_key(name))).rowcount
        return n_new


def host_table(db: DB, suffix: str = "") -> list[dict]:
    """Hosts serving editable documents (optionally under a suffix), with labelled-file counts."""
    db.conn.executescript(SCHEMA)
    rows = db.q("SELECT host, format, count(*) n, sum(labelled) l FROM cc_files WHERE host LIKE ? GROUP BY host, format", (f"%{suffix}",))
    agg: dict = collections.defaultdict(lambda: {"files": 0, "labelled": 0, "formats": {}})
    for r in rows:
        a = agg[r["host"]]
        a["files"] += r["n"]; a["labelled"] += r["l"] or 0; a["formats"][r["format"]] = r["n"]
    return sorted(({"host": h, **v} for h, v in agg.items()), key=lambda x: (-x["labelled"], -x["files"], x["host"]))


def filename_pairs(db: DB) -> list[dict]:
    """S4.5 hints: in one directory of one host, a labelled file and an unlabelled file of the same format whose names
    are equal once the label is removed. Evidence grade 'publisher_file_names' (weaker than a page that lists both)."""
    db.conn.executescript(SCHEMA)
    by_dir: dict = collections.defaultdict(list)
    for r in db.q("SELECT url, host, format, labelled, key FROM cc_files WHERE key != ''"):
        d = r["url"].split("?", 1)[0].rsplit("/", 1)[0]
        by_dir[(r["host"], d, r["format"])].append(r)
    out = []
    for (host, d, fmt), rows in by_dir.items():
        blanks = collections.defaultdict(list)
        for r in rows:
            if not r["labelled"]:
                blanks[r["key"]].append(r["url"])
        for r in rows:
            if r["labelled"] and len(r["key"]) >= 3 and len(blanks.get(r["key"], [])) == 1:
                out.append({"host": host, "format": fmt, "blank": blanks[r["key"]][0], "example": r["url"], "key": r["key"]})
    return out


def unit_groups(db: DB, min_files: int = 2, max_files: int = 6) -> list[dict]:
    """S4.6 units: files of one format under one path-addressed folder (a post's attachment folder such as
    ``/Upload/<board>/relfile/<node>/<post>/``). Query-addressed downloads (``down.php?id=``) carry no folder and are
    left to the page explorer; folders with many files are listings, not one publication."""
    db.conn.executescript(SCHEMA)
    by_dir: dict = collections.defaultdict(list)
    for r in db.q("SELECT url, host, format FROM cc_files"):
        sp = urlsplit(r["url"])
        if sp.query and not re.search(r"mediadl=true", sp.query, re.I):
            continue
        by_dir[(r["host"], sp.path.rsplit("/", 1)[0], r["format"])].append(r["url"].split("#")[0])
    return [{"host": h, "dir": d, "format": f, "files": sorted(set(us))} for (h, d, f), us in by_dir.items()
            if min_files <= len(set(us)) <= max_files]


def enqueue(db: DB, seed_hosts: bool = True, log=print) -> dict:
    """Hand the index results to the normal pipeline: file-name pairs and folder units become document candidates with
    pair links (fetched, parsed and paired by `collect fetch` / `corpus pair`); hosts become explorer seed pages."""
    from .discovery import _add_candidate
    from .pair_explorer import add_seed_pages
    from .search import SearchHit
    stats = collections.Counter()
    for p in filename_pairs(db):
        d = p["blank"].split("?", 1)[0].rsplit("/", 1)[0] + "/"
        group = f"cc:{d}#{p['key']}:{p['format']}"
        for url, role in ((p["blank"], "blank"), (p["example"], "example")):
            link = {"group": group, "page": d, "role": role, "key": p["key"], "name_relation": "file_name_same_dir", "link_text": file_name(url)}
            stats["file_name_docs"] += _link_candidate(db, url, p["format"], link, _add_candidate, SearchHit)
    for u in unit_groups(db):
        group = f"cc-unit:{u['host']}{u['dir']}/:{u['format']}"
        for url in u["files"]:
            link = {"group": group, "page": f"https://{u['host']}{u['dir']}/", "role": "member", "key": "", "name_relation": "content_same_unit",
                    "link_text": file_name(url)}
            stats["unit_docs"] += _link_candidate(db, url, u["format"], link, _add_candidate, SearchHit)
        stats["units"] += 1
    stats["name_queries"] = enqueue_name_queries(db)
    if seed_hosts:
        visited = {r["host"] for r in db.q("SELECT DISTINCT host FROM explore_pages")}
        hosts = [h["host"] for h in host_table(db) if h["host"] not in visited]
        stats["seed_hosts"] = add_seed_pages(db, [f"https://{h}/" for h in hosts], origin="cc_host")
    return dict(stats)


def _link_candidate(db, url, fmt, link, add, Hit) -> int:
    new = add(db, Hit(url, link["link_text"], "", 0, "document", fmt, {"pair_links": [link]}), "commoncrawl-index", "cc_index", link["page"], 0)
    if not new:                                        # known file: add the link once
        row = db.one("SELECT extra FROM candidates WHERE url=?", (url,))
        x = json.loads(row["extra"] or "{}")
        links = x.setdefault("pair_links", [])
        if not any(lk["group"] == link["group"] for lk in links):
            links.append(link)
            db.x("UPDATE candidates SET extra=? WHERE url=?", (json.dumps(x, ensure_ascii=False), url))
    return 1


def enqueue_name_queries(db: DB) -> int:
    """S1.2d: the page that lists a labelled file is found by searching for the file's own name (minus extension and
    revision noise); the explorer then reads that page like any other."""
    from .pair_explorer import PairExplorer, SCHEMA as EX_SCHEMA, WEB_ENGINE
    db.conn.executescript(EX_SCHEMA)
    n = 0
    for r in db.q("SELECT DISTINCT name, tld FROM cc_files WHERE labelled = 1"):
        stem = re.sub(r"\.(odt|docx|hwpx|doc|pdf)$", "", r["name"], flags=re.I)
        stem = re.sub(r"[_\-\s]*\(?\d{6,}\)?$", "", stem).strip(" _-")
        if len(stem) < 4:
            continue
        region = {"tw": "tw-tzh", "kr": "kr-kr", "jp": "jp-jp", "cn": "cn-zh"}.get(r["tld"], "wt-wt")
        n += db.x("INSERT OR IGNORE INTO explore_queries(query,engine,region,origin,status,n_results,ts) VALUES(?,?,?,?,?,?,?)",
                  (f'"{stem}"', WEB_ENGINE, region, "ccname:" + r["tld"], "pending", 0, utcnow())).rowcount
    return n
