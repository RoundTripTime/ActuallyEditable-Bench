"""Iterative discovery of sites that publish a blank form next to its completed example.

Loop: search (short, general, multilingual) -> result pages -> same-format pairs on the page -> productive site ->
site expansion (board list, neighbouring posts, pagination, form-library links, ``site:`` queries) -> pairs ->
patterns learned from real attachment names -> new short queries -> ... Pairs found here are only *candidates*;
the regular fetch / parse / pairing stages download and check the real files, and a person verifies them.

Every query, page and decision is logged in ``explore_queries`` / ``explore_pages`` / ``explore_hosts``.
"""
from __future__ import annotations

import collections
import functools
import json
import math
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

from ..util import DB, network_up, utcnow
from . import button_capture, render_page
from .discovery import _add_candidate, make_client
from .search import EXAMPLE_RX, PROVIDERS, PairedAttachmentProvider, SearchHit, canonical_post_url, extract_links

SCHEMA = """
CREATE TABLE IF NOT EXISTS explore_queries(query TEXT, engine TEXT, region TEXT, origin TEXT, status TEXT, n_results INTEGER,
  n_pair_pages INTEGER DEFAULT 0, ts TEXT, PRIMARY KEY(query, engine, region));
CREATE TABLE IF NOT EXISTS explore_pages(url TEXT PRIMARY KEY, host TEXT, query TEXT, origin TEXT, depth INTEGER, status TEXT,
  n_docs INTEGER, n_examples INTEGER, n_pair_docs INTEGER, formats TEXT, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS explore_docs(url TEXT, page TEXT, host TEXT, role TEXT, key TEXT, format TEXT, title TEXT, PRIMARY KEY(url, page));
CREATE TABLE IF NOT EXISTS explore_engine(engine TEXT PRIMARY KEY, blocked_until REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS explore_hosts(host TEXT PRIMARY KEY, lang TEXT, productive INTEGER DEFAULT 0, pair_docs INTEGER DEFAULT 0,
  pages INTEGER DEFAULT 0, site_queries_done INTEGER DEFAULT 0, first_seen TEXT, note TEXT);
"""

# short expressions people actually use for "blank form / filled-in example", per language, with the search region
SEED_QUERIES = {
    "ko": ("kr-kr", ["서식 예시", "양식 견본", "작성예시", "기재례", "작성례", "양식 예시", "서식 견본", "작성 예시 양식"]),
    "en": ("us-en", ["blank completed form", "template sample completed", "completed example", "filled example form",
                     "blank and completed", "worked example template", "example completed template", "sample filled form"]),
    "en-gb": ("uk-en", ["completed example form", "blank template completed example", "example of completed form"]),
    "en-au": ("au-en", ["completed example form", "template and completed example"]),
    "zh-tw": ("tw-tzh", ["空白 範例", "填寫範例", "表單 範例", "範例 下載", "空白表格 範例", "書表 範例"]),
    "zh-cn": ("cn-zh", ["空白 样例", "填写示例", "填写样例", "表格 范例", "模板 填写示例"]),
    "ja": ("jp-jp", ["様式 記入例", "様式 記載例", "記入例 ダウンロード", "見本 様式", "記載例 Word"]),
    "fr": ("fr-fr", ["modèle rempli", "modèle vierge exemple", "exemple rempli", "formulaire vierge exemple"]),
    "de": ("de-de", ["Ausfüllbeispiel", "Muster ausgefüllt", "Vordruck Beispiel ausgefüllt"]),
    "es": ("es-es", ["modelo rellenado ejemplo", "ejemplo cumplimentado", "plantilla en blanco ejemplo"]),
    "es-mx": ("mx-es", ["formato ejemplo de llenado", "ejemplo llenado formato"]),
    "pt": ("br-pt", ["modelo exemplo preenchido", "formulário exemplo preenchido", "modelo em branco exemplo"]),
    "nl": ("nl-nl", ["voorbeeld ingevuld formulier", "invulvoorbeeld"]),
    "id": ("id-en", ["contoh pengisian formulir", "contoh pengisian"]),
    "th": ("th-th", ["ตัวอย่างการกรอก แบบฟอร์ม"]),
    "vi": ("vn-vi", ["mẫu đơn và hướng dẫn điền", "mẫu điền sẵn"]),
}
SITE_MARKERS = {"ko": ["작성예시", "기재례", "작성례", "예시", "견본", "서식 예시"],
                "ja": ["記入例", "記載例", "作成例", "様式 記入例", "見本", "様式 word"],
                "zh-tw": ["範例", "填寫範例", "參考範例", "範本", "空白 範例", "表單 範例"],
                "zh-cn": ["示例", "样例", "填写示例", "填写样例", "范例", "模板 示例"],
                "en": ["completed example", "example", "sample", "worked example", "filled example", "example form"],
                "fr": ["exemple", "exemple rempli"], "de": ["Beispiel", "Ausfüllbeispiel"], "es": ["ejemplo", "ejemplo llenado"],
                "pt": ["exemplo preenchido", "exemplo"], "nl": ["voorbeeld", "ingevuld"], "id": ["contoh pengisian", "contoh"],
                "th": ["ตัวอย่างการกรอก", "ตัวอย่าง"]}
DOWNLOAD_BUTTON_RX = re.compile(rb"<button\b(?:(?!</button>).){0,1500}?(download|\xeb\x8b\xa4\xec\x9a\xb4\xeb\xa1\x9c\xeb\x93\x9c)", re.I | re.S)
GITHUB_REPO_RX = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?(?:tree/[^?#]*)?$")
# file endpoints of boards (a document, or a viewer of one), never a page with further links
DOWNLOAD_ENDPOINT_RX = re.compile(r"(file_?down|filedown|download\.do|down\.do|fileview|file_?view|filedownload|atchfileid=|"
                                  r"/download/|download\.php|down\.php|getfile|attach_?down|\bfn_?down)", re.I)
BLANK_WORD_RX = re.compile(r"(blank|template|empty|빈\s*양식|서식|양식)", re.I)
PAGINATION_RX = re.compile(r"^\s*(\d{1,3}|next|다음|次へ|次|下一頁|下一页|下頁|suivant|weiter|siguiente|›|»|>)\s*$", re.I)
PAGE_PARAM_RX = re.compile(r"(page|pageindex|pageno|cpage|curpage|pg|p|offset|start|startpage|page_no|nowpage)$", re.I)
NOUN_RX = re.compile(r"([\w가-힣぀-ヿ一-鿿]{2,12}(?:서|장|표|대장|書|表|届|簿|單|单|form|report|plan|letter|agreement|register))", re.I)


def lang_of(host: str, text: str = "") -> str:
    h = host or ""
    if h.endswith(".kr"):
        return "ko"
    if h.endswith(".jp"):
        return "ja"
    if h.endswith(".tw") or h.endswith(".hk") or h.endswith(".mo"):
        return "zh-tw"
    if h.endswith(".cn"):
        return "zh-cn"
    if h.endswith(".fr") or h.endswith(".ma") or h.endswith(".be"):
        return "fr"
    if h.endswith(".de") or h.endswith(".at") or h.endswith(".ch"):
        return "de"
    if h.endswith(".es") or h.endswith(".mx") or h.endswith(".ar") or h.endswith(".cl"):
        return "es"
    if h.endswith(".br") or h.endswith(".pt"):
        return "pt"
    if h.endswith(".nl"):
        return "nl"
    if h.endswith(".id"):
        return "id"
    if h.endswith(".th"):
        return "th"
    if re.search(r"[가-힣]", text):
        return "ko"
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    return "en"


_CC = [(".gov.taipei", "TW"), (".tw", "TW"), (".kr", "KR"), (".jp", "JP"), (".cn", "CN"), (".hk", "HK"), (".mo", "MO"), (".sg", "SG"),
       (".my", "MY"), (".ph", "PH"), (".in", "IN"), (".id", "ID"), (".th", "TH"), (".vn", "VN"), (".uk", "UK"), (".ie", "IE"),
       (".us", "US"), (".gov", "US"), (".mil", "US"), (".ca", "CA"), (".au", "AU"), (".nz", "NZ"), (".za", "ZA"), (".fr", "FR"),
       (".be", "BE"), (".ch", "CH"), (".de", "DE"), (".at", "AT"), (".nl", "NL"), (".it", "IT"), (".es", "ES"), (".pt", "PT"),
       (".br", "BR"), (".mx", "MX"), (".ar", "AR"), (".cl", "CL"), (".co", "CO"), (".ma", "MA")]
_SECTORS = [("gov", r"(\.|^)(gov|go|lg|gob|gouv|govt|gc|mil|admin|metro\.tokyo)\.|\.gov$|gov\.taipei|^(www\.)?(city|pref|town|vill)\."),
            ("health", r"hosp|clinic|medical|health|nhs\.|hospital|医院|病院|\.or\.kr$.*hosp"),
            ("court_law", r"court|scourt|judicial|law|legal|bar\.|moj\.|attorney"),
            ("edu", r"(\.|^)(edu|ac|sch|school|k12)\.|\.edu$|univ|college"),
            ("org", r"(\.|^)(or|org|ngo|assoc)\.|\.org$"),
            ("com", r"(\.|^)(co|com|biz|ne|net)\.|\.(com|net|io|biz)$")]


@functools.lru_cache(maxsize=65536)
def coverage_cell(host: str) -> tuple[str, str]:
    """(country, sector) of a host, from its domain name only (deterministic; used to spread exploration)."""
    h = (host or "").lower()
    cc = next((c for suf, c in _CC if h.endswith(suf)), "other")
    sec = next((n for n, rx in _SECTORS if re.search(rx, h)), "other")
    return cc, sec


def excluded_country(target: str, cfg: dict) -> bool:
    """User decision 2026-10-15 ('일본 문서가 너무 많아. 일본을 제외하고 ...'): hosts of excluded countries are not explored.
    ``target`` is a URL, a host or a search query ('site:<host> ...'); queries in an excluded language region count too."""
    excl = {c.upper() for c in (cfg.get("exclude_countries") or [])}
    if not excl:
        return False
    t = target.strip()
    if t.startswith("page:"):
        t = t[5:]
    if t.startswith("site:"):
        t = t.split()[0][5:]
    host = (urlsplit(t).hostname if "://" in t else t.split("/")[0]).lower() if t else ""
    return coverage_cell(host)[0] in excl if host and "." in host else False


def language_allowed(lang: str | None, cfg: dict) -> bool:
    """Search queries are run only in the collection languages (user decision 2026-10-15: Korean and English)."""
    allowed = set(cfg.get("include_languages") or [])
    if not allowed or not lang:
        return True
    base = "ko" if lang.startswith("ko") or lang == "kr" else "en" if lang.startswith("en") or lang in ("uk", "us") else lang
    return base in allowed


def coverage(db: DB) -> list[dict]:
    """Explored pages / hosts / labelled examples / pair documents per (country, sector) cell."""
    cells: dict = collections.defaultdict(lambda: {"hosts": set(), "pages": 0, "examples": 0, "pair_docs": 0, "pending": 0})
    for r in db.q("SELECT host, n_examples, n_pair_docs FROM explore_pages"):
        c = cells[coverage_cell(r["host"])]
        c["hosts"].add(r["host"]); c["pages"] += 1; c["examples"] += r["n_examples"] or 0; c["pair_docs"] += r["n_pair_docs"] or 0
    for r in db.q("SELECT query FROM explore_queries WHERE engine='direct' AND status='pending'"):
        cells[coverage_cell(urlsplit(r["query"][5:]).hostname or "")]["pending"] += 1
    return sorted(({"country": k[0], "sector": k[1], **{**v, "hosts": len(v["hosts"])}} for k, v in cells.items()),
                  key=lambda x: (-x["pages"], x["country"], x["sector"]))


REGION_OF_COUNTRY = {"JP": "jp-jp", "KR": "kr-kr", "TW": "tw-tzh", "CN": "cn-zh", "UK": "uk-en", "FR": "fr-fr", "DE": "de-de"}
PRODUCTIVE_BOARD_PAGES = 50
POST_VIEW_RX = re.compile(r"(view\.do|/view\b|View\.do|amode=view|act=view|mode=view|boardView|Detail|read\.do|[?&](idx|bIdx|nttId|list_no|boardSeq|seq|articleNo)=)", re.I)
WEB_ENGINE = "web"            # FailoverSearchProvider: local SearXNG -> public SearXNG (JSON) -> DuckDuckGo HTML
DEFAULT_BACKENDS = [{"type": "searxng", "label": "searxng-local", "base_url": "http://127.0.0.1:8888", "local": True},
                    {"type": "duckduckgo_html", "label": "duckduckgo-html"}]


class NetworkDown(RuntimeError):
    """The VM lost its network (not a single site failing)."""


class PairExplorer:
    def __init__(self, db: DB, cfg: dict, log=print, client=None):
        self.db, self.cfg, self.log = db, cfg, log
        self.db.conn.executescript(SCHEMA)
        self.client = client or make_client(db, cfg)
        self.excluded = cfg.get("exclude_formats", [])
        self.formats = cfg.get("pair_formats")
        ex = cfg.get("pair_explorer", {})
        self.pages_per_host = int(ex.get("pages_per_host", 40))
        self.site_queries_per_host = int(ex.get("site_queries_per_host", 3))
        self.results_per_query = int(ex.get("results_per_query", 20))
        self.hub_pages = int(ex.get("hub_pages_per_root", 40))
        self.search_backends = ex.get("search_backends") or DEFAULT_BACKENDS
        rb = int(ex.get("render_pages_per_host", 30))       # headless renders per host and process (S4.4)
        self.render_budget: collections.defaultdict = collections.defaultdict(lambda: rb)
        cb = int(ex.get("capture_pages_per_host", 12))      # S4.5 button captures per host and process
        self.capture_budget: collections.defaultdict = collections.defaultdict(lambda: cb)
        self.host_pages: collections.Counter = collections.Counter(
            {r["host"]: r["n"] for r in db.q("SELECT host, count(*) n FROM explore_pages GROUP BY host")})

    # ------------------------------------------------------------------ queue of queries
    def _enqueue_query(self, query: str, region: str, origin: str) -> None:
        self.db.x("INSERT OR IGNORE INTO explore_queries(query,engine,region,origin,status,n_results,ts) VALUES(?,?,?,?,?,?,?)",
                  (query, WEB_ENGINE, region, origin, "pending", 0, utcnow()))

    def _productive_hosts(self) -> set:
        c = getattr(self, "_prod_cache", None)
        if c is None or time.time() - c[0] > 120:
            c = self._prod_cache = (time.time(), {r["host"] for r in self.db.q("SELECT host FROM explore_hosts WHERE productive=1")})
        return c[1]

    def reset_stale(self) -> dict:
        """Work left half done by a stopped process (VM shutdown, kill) goes back to the queue: queries 'running' become
        'pending' and pages stuck in 'visiting' are forgotten so they are visited again. Call it once, while no other
        explorer runs (the loop script does it before starting its workers)."""
        q = self.db.x("UPDATE explore_queries SET status='pending' WHERE status='running'").rowcount
        p = self.db.x("DELETE FROM explore_pages WHERE status='visiting'").rowcount
        return {"queries_requeued": q, "pages_forgotten": p}

    def seed(self, langs: list[str] | None = None) -> int:
        n = 0
        for lang, (region, qs) in SEED_QUERIES.items():
            if langs and lang not in langs:
                continue
            for q in qs:
                self._enqueue_query(q, region, f"seed:{lang}")
                n += 1
        return n

    def _next_query(self, langs: list[str] | None):
        # site expansion and learned queries first (they come from productive sites), then generic seeds
        eng = self.db.one("SELECT blocked_until FROM explore_engine WHERE engine=?", (WEB_ENGINE,))
        engine_ok = eng is None or (eng["blocked_until"] or 0) < time.time()
        rows = self.db.q("SELECT * FROM explore_queries WHERE status='pending' AND engine='direct' ORDER BY ts LIMIT 8000")
        if engine_ok:                                  # search queries are fetched separately so a long page queue cannot hide them
            rows += self.db.q("SELECT * FROM explore_queries WHERE status='pending' AND engine!='direct' ORDER BY CASE "
                              "WHEN origin LIKE 'usersite:%' THEN -1 "
                              "WHEN origin IN ('krq:ko', 'dirsite:ko', 'ccsite:ko', 'site:ko', 'ccname:kr', 'learned:ko') THEN 0 "   # user 2026-10-15: raise the Korean share
                              "WHEN origin = 'dirsite:zh-tw' THEN 1 "
                              "WHEN origin LIKE 'site:%' OR origin LIKE 'ccsite:%' OR origin LIKE 'ccname:%' OR origin LIKE 'dirsite:%' THEN 1 "
                              "WHEN origin LIKE 'learned:%' THEN 2 ELSE 3 END, ts LIMIT 200")
        # breadth first: among direct pages, unseen hosts and the least explored (country, sector) cells go first, so that
        # productive regions do not absorb the whole budget (re-analysis of already known pages comes after new ground)
        cache = getattr(self, "_cell_cache", None)
        if cache is None or time.time() - cache[0] > 120:  # per-process cache: the aggregate changes slowly, recomputing it was CPU-heavy
            seen = {}
            cell_n: collections.Counter = collections.Counter()
            cell_hit: collections.Counter = collections.Counter()
            for r in self.db.q("SELECT host, count(*) n, sum(n_pair_docs > 0) p, sum(n_examples > 0) e FROM explore_pages GROUP BY host"):
                seen[r["host"]] = r["n"]
                c = coverage_cell(r["host"])
                cell_n[c] += r["n"]
                cell_hit[c] += (r["p"] or 0) + 0.2 * (r["e"] or 0)  # a labelled example counts a little: the site publishes examples
            self._cell_cache = cache = (time.time(), seen, cell_n, cell_hit)
        _, seen, cell_n, cell_hit = cache
        total = max(1, sum(cell_n.values()))
        def ucb(cell) -> float:                        # UCB1 over (country, sector) cells: yield so far + exploration bonus
            n = cell_n[cell]
            if n == 0:
                return float("inf")
            return cell_hit[cell] / n + math.sqrt(2 * math.log(total) / n)
        self._calls = getattr(self, "_calls", 0) + 1
        web_first = engine_ok and self._calls % int(self.cfg.get("pair_explorer", {}).get("search_every", 4)) == 0
        def novelty(r):                                # every Nth pick is a search query, so searching is never starved
            if r["engine"] != "direct":
                return (-1 if web_first else 1, 0, 0, 0)
            h = (urlsplit(r["query"][5:]).hostname or "").lower()
            if r["origin"].startswith("user"):          # pages nominated by the user go first
                return (-2, 0, 0, 0)
            if r["origin"] == "reparse":                 # cached pages that yield a pair after an extraction fix
                return (-2, 1, 0, 0)
            return (0, 1 if r["origin"] in ("reanalyse", "site_index", "site_index2") else 0, -ucb(coverage_cell(h)), 1 if h in seen else 0)
        rows = sorted(rows, key=lambda r: novelty(r))   # stable: keeps the engine / origin / time order inside a tier
        excl_regions = {REGION_OF_COUNTRY.get(c.upper()) for c in (self.cfg.get("exclude_countries") or [])}
        for r in rows:
            lang = r["origin"].split(":", 1)[1] if ":" in r["origin"] else None
            if excluded_country(r["query"], self.cfg) or (r["engine"] != "direct" and r["region"] in excl_regions) or \
                    (r["engine"] != "direct" and not language_allowed(lang, self.cfg)):
                self.db.x("UPDATE explore_queries SET status='skipped_country' WHERE query=? AND engine=? AND region=? AND status='pending'",
                          (r["query"], r["engine"], r["region"]))
                continue
            if not langs or lang is None or lang in langs:
                claimed = self.db.x("UPDATE explore_queries SET status='running' WHERE query=? AND engine=? AND region=? AND status='pending'",
                                    (r["query"], r["engine"], r["region"])).rowcount
                if claimed == 1:                       # another worker may have taken it since the SELECT
                    return r
        return None

    # ------------------------------------------------------------------ one page
    def _visit(self, url: str, query: str, origin: str, depth: int) -> dict | None:
        url = canonical_post_url(url)                    # one board post, not one visit per list state (pageIndex, search)
        if excluded_country(url, self.cfg):              # user decision: excluded countries are not collected any more
            return None
        if self.db.one("SELECT 1 FROM explore_pages WHERE url=?", (url,)):
            return None
        if DOC_URL_RX.search(unquote(url)):            # a data / document file handed in as a page (open-data resources)
            self.db.x("INSERT OR IGNORE INTO explore_pages(url,host,query,origin,depth,status,reason,ts) VALUES(?,?,?,?,?,?,?,?)",
                      (url, (urlsplit(url).hostname or "").lower(), query, origin, depth, "not_html", "document URL, not fetched as a page", utcnow()))
            return None
        host = (urlsplit(url).hostname or "").lower()
        cap = self.pages_per_host * (3 if host in self._productive_hosts() else 1)   # productive hosts are read further
        if self.host_pages[host] >= cap and depth > 0:
            return None
        self.db.x("INSERT OR IGNORE INTO explore_pages(url,host,query,origin,depth,status,ts) VALUES(?,?,?,?,?,?,?)",
                  (url, host, query, origin, depth, "visiting", utcnow()))
        self.host_pages[host] += 1
        gh = GITHUB_REPO_RX.match(url)
        if gh:                                           # a repository: its file tree, read through the public API
            return self._github_repo(url, gh.group(1), gh.group(2), query, depth)
        fr = self.client.fetch(url, purpose="expand")
        if fr.status == "error" and not network_up():
            self.db.x("DELETE FROM explore_pages WHERE url=?", (url,))     # not this site's fault: visit it again later
            raise NetworkDown(f"network unreachable while fetching {url}")
        if fr.status != "ok" or not fr.content:
            self.db.x("UPDATE explore_pages SET status=?, reason=? WHERE url=?", (fr.status, (fr.reason or "")[:200], url))
            return None
        if not re.search(rb"<(a|html|body)\b", fr.content[:200000], re.I):
            self.db.x("UPDATE explore_pages SET status=?, reason=? WHERE url=?", ("not_html", "document or non-HTML result", url))
            return None
        html = fr.content
        labelled = PairedAttachmentProvider.labelled_docs(html, url, self.excluded)
        rendered = False
        if not labelled and self.render_budget[host] > 0 and render_page.looks_scripted(html):
            self.render_budget[host] -= 1               # S4.4: list drawn by script -> read the rendered DOM
            dom = render_page.render(url, self.client.ua)
            if dom:
                html, rendered = dom, True
                labelled = PairedAttachmentProvider.labelled_docs(html, url, self.excluded)
        pairs = PairedAttachmentProvider.pairs_on_page(html, url, self.excluded, self.formats)
        captured = None
        if not pairs and self.capture_budget[host] > 0 and DOWNLOAD_BUTTON_RX.search(html):
            text = re.sub(r"<[^>]+>", " ", html.decode("utf-8", "replace"))
            if EXAMPLE_RX.search(text) and BLANK_WORD_RX.search(text):   # S4.5: blank + example offered as buttons
                self.capture_budget[host] -= 1
                captured = self._capture(url)
                pairs = PairedAttachmentProvider.pair_docs(captured, url, self.formats)
                kept = {h.url for h in pairs}
                for h in captured:                      # unpaired captures are not collected
                    if h.url not in kept:
                        Path(h.extra["captured_file"]).unlink(missing_ok=True)
                labelled = labelled + [(h, "example" if EXAMPLE_RX.search(h.title) else "blank", "") for h in captured]
                rendered = rendered or bool(captured)
        n_ex = sum(1 for _, role, _ in labelled if role == "example")
        fmts = sorted({d.guessed_format for d, _, _ in labelled if d.guessed_format})
        self.db.x("UPDATE explore_pages SET status=?, n_docs=?, n_examples=?, n_pair_docs=?, formats=?, reason=? WHERE url=?",
                  ("ok_rendered" if rendered else "ok", len(labelled), n_ex, len(pairs), ",".join(fmts),
                   ((f"button capture: {len(captured)} files; " if captured is not None else "") +
                    ("" if pairs else ("examples only in other formats or no blank" if n_ex else "no labelled example"))), url))
        for d, role, key in labelled:                     # site index: an example and its blank may sit on different pages
            self.db.x("INSERT OR IGNORE INTO explore_docs(url,page,host,role,key,format,title) VALUES(?,?,?,?,?,?,?)",
                      (d.url, url, host, role, key, d.guessed_format, d.title[:300]))
        pairs = pairs + self._site_pairs(host, {h.url for h in pairs})
        for h in pairs:
            _add_candidate(self.db, h, query, "observed_pair_explore", url, depth)
            row = self.db.one("SELECT extra FROM candidates WHERE url=?", (h.url,))
            x = json.loads(row["extra"] or "{}")
            links = x.get("pair_links", [])
            links.extend(lk for lk in h.extra.get("pair_links", []) if lk not in links)
            x["pair_links"] = links
            self.db.x("UPDATE candidates SET extra=? WHERE url=?", (json.dumps(x, ensure_ascii=False), h.url))
        text = " ".join(d.title for d, _, _ in labelled)[:2000]
        self.db.x("INSERT OR IGNORE INTO explore_hosts(host,lang,first_seen) VALUES(?,?,?)", (host, lang_of(host, text), utcnow()))
        self.db.x("UPDATE explore_hosts SET pages=pages+1, pair_docs=pair_docs+? , productive=MAX(productive, ?) WHERE host=?",
                  (len(pairs), 1 if pairs else 0, host))
        return {"html": html, "pairs": pairs, "labelled": labelled, "host": host, "n_ex": n_ex, "rendered": rendered}

    def _github_repo(self, url: str, owner: str, repo: str, query: str, depth: int) -> dict | None:
        """GitHub repositories list their files by script; the API tree gives them. Files of one directory are one
        publication unit (like the attachments of one post) and are paired by the same publisher-label rules."""
        import os.path
        meta = self.client.fetch(f"https://api.github.com/repos/{owner}/{repo}", purpose="search")
        info = json.loads(meta.content) if meta.status == "ok" and meta.content else {}
        br = info.get("default_branch") or "HEAD"
        lic = ((info.get("license") or {}).get("spdx_id")) or None
        ft = self.client.fetch(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{br}", purpose="search", params={"recursive": "1"})
        if ft.status != "ok" or not ft.content:
            self.db.x("UPDATE explore_pages SET status=?, reason=? WHERE url=?", (ft.status, (ft.reason or "github tree")[:200], url))
            return None
        by_dir: dict[str, list] = collections.defaultdict(list)
        for node in json.loads(ft.content).get("tree", []):
            pth = node.get("path", "")
            fmt = pth.rsplit(".", 1)[-1].lower() if "." in pth else ""
            if node.get("type") == "blob" and fmt in (self.formats or ()) and 1000 < node.get("size", 0) < 20_000_000:
                d = os.path.dirname(pth)
                extra = {"repo": f"{owner}/{repo}", "form_group": f"github:{owner}/{repo}:{d}"}
                if lic and lic != "NOASSERTION":
                    extra.update({"license_spdx": lic, "license_url": f"https://github.com/{owner}/{repo}/blob/{br}/LICENSE"})
                by_dir[d].append(SearchHit(f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/{quote(pth)}",
                                           os.path.basename(pth), "", 0, "document", fmt, extra))
        pairs = []
        for d, docs in sorted(by_dir.items()):
            pairs += PairedAttachmentProvider.pair_docs(docs, f"https://github.com/{owner}/{repo}/tree/{br}/{d}".rstrip("/"), self.formats)
        n_docs = sum(len(v) for v in by_dir.values())
        self.db.x("UPDATE explore_pages SET status=?, n_docs=?, n_examples=?, n_pair_docs=?, formats=?, reason=? WHERE url=?",
                  ("ok", n_docs, sum(1 for v in by_dir.values() for h in v if EXAMPLE_RX.search(h.title)), len(pairs),
                   ",".join(sorted({h.guessed_format for v in by_dir.values() for h in v})),
                   "github tree" + ("" if pairs else ": no labelled pair"), url))
        for h in pairs:
            _add_candidate(self.db, h, query, "observed_pair_explore", url, depth)
        self.db.x("INSERT OR IGNORE INTO explore_hosts(host,lang,first_seen) VALUES(?,?,?)", ("github.com", "en", utcnow()))
        return None                                      # nothing to expand: the tree is the whole repository

    def _capture(self, url: str) -> list[SearchHit]:
        """S4.5: press the page's download buttons in headless Chrome; each saved file becomes a document hit that
        carries the publisher's labels (card heading / button text / suggested file name) for role and pairing."""
        import hashlib
        from ..util import path
        out_dir = path("data", "raw", "captured", hashlib.sha1(url.encode()).hexdigest()[:16])
        res = button_capture.capture(url, self.client.ua, out_dir)
        self.log(f"button capture {url[:90]}: {res['status']} {len(res['files'])} files {res['reason'][:80]}")
        hits = []
        for f in res["files"]:
            name = f["suggested"] or ""
            fmt = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if fmt not in (self.formats or ()):
                continue
            stem = re.sub(r"[-_]+", " ", name.rsplit(".", 1)[0]).strip()
            title = " ".join(x for x in (f["heading"], f["aria"], stem) if x)[:300]
            hits.append(SearchHit(f"{url}#fb-capture={len(hits)}:{name}", title, "", len(hits) + 1, "document", fmt,
                                  {"captured_file": f["path"], "captured_name": name, "landing_url": url, "via": "button_capture",
                                   "capture": {"version": button_capture.CAPTURE_VERSION, "button_text": f["text"],
                                               "aria": f["aria"], "heading": f["heading"], "card": f["card"][:300],
                                               "blob_url": f["blob_url"]}}))
        return hits

    def _site_pairs(self, host: str, known: set) -> list[SearchHit]:
        """Strict same-site pairs across pages (same form name after label removal, same editable format, one blank only)."""
        rows = self.db.q("SELECT * FROM explore_docs WHERE host=?", (host,))
        if not any(r["role"] == "example" for r in rows):
            return []
        items = [(SearchHit(r["url"], r["title"], "", 0, "document", r["format"], {}), r["role"], r["key"], r["page"]) for r in rows
                 if r["format"] and (self.formats is None or r["format"] in self.formats) and r["format"] not in self.excluded]
        return [h for h in PairedAttachmentProvider.cross_page_pairs({host: items}) if h.url not in known]

    # ------------------------------------------------------------------ site expansion
    def _neighbours(self, html: bytes, url: str, productive: bool = False) -> list[str]:
        """Board list / previous-next posts / pagination / form-library links of the same host."""
        host = urlsplit(url).hostname
        out = PairedAttachmentProvider.post_links(html, url)[:30] + PairedAttachmentProvider.library_links(html, url)[:20]
        for h in extract_links(html, url, same_domain_only=True):
            if h.kind == "page" and urlsplit(h.url).hostname == host and PAGINATION_RX.match(h.title or ""):
                out.append(h.url)
        sp = urlsplit(url)                                 # the list page of a board post: drop the post id, keep the board
        qs = parse_qs(sp.query)
        for k in list(qs):
            if PAGE_PARAM_RX.match(k):
                # a board whose page showed same-format pairs is read to the end (civil-service form boards list
                # dozens of forms with their examples, e.g. 민원서식 boards of Korean municipalities); others: 3 pages
                is_post = bool(POST_VIEW_RX.search(url))   # a post's own page: its 'page' parameter only echoes the list
                if is_post:
                    continue
                for n in range(1, (PRODUCTIVE_BOARD_PAGES if productive else 3) + 1):
                    out.append(urlunsplit(sp._replace(query=urlencode({**{a: b[0] for a, b in qs.items()}, k: str(n)}))))
        return list(dict.fromkeys(canonical_post_url(u) for u in out if u != url and not DOC_URL_RX.search(unquote(u))
                                  and not DOWNLOAD_ENDPOINT_RX.search(u)))

    def _expand_site(self, page: dict, url: str, query: str, depth: int, max_depth: int) -> None:
        host = page["host"]
        lang = (self.db.one("SELECT lang FROM explore_hosts WHERE host=?", (host,)) or {"lang": "en"})["lang"]
        done = self.db.one("SELECT site_queries_done FROM explore_hosts WHERE host=?", (host,))["site_queries_done"]
        if done < self.site_queries_per_host:              # the site's own index through the search engine
            region = SEED_QUERIES.get(lang, ("wt-wt", []))[0]
            for m in SITE_MARKERS.get(lang, SITE_MARKERS["en"])[: self.site_queries_per_host]:
                self._enqueue_query(f"site:{host} {m}", region, f"site:{lang}")
            self.db.x("UPDATE explore_hosts SET site_queries_done=? WHERE host=?", (self.site_queries_per_host, host))
        for d, role, _key in page["labelled"]:              # learned: the publisher's own wording becomes a new short query
            if role == "example":
                lab = EXAMPLE_RX.search(unquote(d.title))
                noun = NOUN_RX.search(unicodedata.normalize("NFKC", d.title))
                if lab and noun:
                    region = SEED_QUERIES.get(lang, ("wt-wt", []))[0]
                    self._enqueue_query(f"{noun.group(1)} {lab.group(0).strip('()[]_ ')}", region, f"learned:{lang}")
        if depth < max_depth:
            for u in self._neighbours(page["html"], url, productive=bool(page["pairs"])):
                sub = self._visit(u, query, "neighbour", depth + 1)
                if sub and (sub["pairs"] or sub["n_ex"] or sub.get("rendered")):
                    self._expand_site(sub, u, query, depth + 1, max_depth)

    # ------------------------------------------------------------------ hub crawl (institution roots)
    def _hub_crawl(self, root: str, query: str, origin: str, max_depth: int) -> int:
        """Strategy S1.3/S1.2b: from an institution's home page, follow links that lead to form libraries or to the
        institution's offices (sub-domains of the same organisation included), two levels deep, a bounded number of pages.
        Pages that show labelled examples get the normal site expansion."""
        org = org_domain(root)
        queue, seen, n_pages, n_pair_pages = [(root, 0)], {root}, 0, 0
        while queue and n_pages < self.hub_pages:
            url, d = queue.pop(0)
            page = self._visit(url, query, origin if d == 0 else "hub", d)
            if not page:
                continue
            n_pages += 1
            if page["pairs"]:
                n_pair_pages += 1
            if page["pairs"] or page["n_ex"] or page.get("rendered"):
                self._expand_site(page, url, query, 0, max_depth)
            if d < 2:
                for u in hub_links(page["html"], url, org):
                    if u not in seen:
                        seen.add(u)
                        queue.append((u, d + 1))
        return n_pair_pages

    # ------------------------------------------------------------------ main loop
    def run(self, max_queries: int = 50, max_depth: int = 2, langs: list[str] | None = None) -> dict:
        stats = collections.Counter()
        ddg = {}
        for _ in range(max_queries):
            q = self._next_query(langs)
            if q is None:
                break
            try:
                self._run_query(q, stats, ddg, max_depth)
            except NetworkDown as e:                   # requeue the query, stop this worker; the loop waits for the network
                self.db.x("UPDATE explore_queries SET status='pending' WHERE query=? AND engine=? AND region=?", (q["query"], q["engine"], q["region"]))
                self.db.x("DELETE FROM explore_pages WHERE status='visiting' AND query=?", (q["query"],))
                self.log(f"explore: {e}; stopping")
                stats["network_down"] += 1
                break
        stats["productive_hosts"] = self.db.one("SELECT count(*) n FROM explore_hosts WHERE productive=1")["n"]
        stats["pending_queries"] = self.db.one("SELECT count(*) n FROM explore_queries WHERE status='pending'")["n"]
        return dict(stats)

    def _run_query(self, q, stats, ddg, max_depth: int) -> None:
        if q["engine"] == "direct":                    # a page handed in from outside the loop
            res = type("R", (), {"status": "ok", "hits": [SearchHit(q["query"][5:], kind="page")], "reason": ""})()
        else:
            prov = ddg.get(q["region"]) or PROVIDERS["failover_web"](self.client, {"backends": self.search_backends, "region": q["region"]})
            ddg[q["region"]] = prov
            res = prov.search(q["query"], self.results_per_query)
        stats["queries"] += 1
        m = re.match(r"\s*site:(\S+)", q["query"]) if q["engine"] != "direct" else None
        if m and res.status == "ok":                    # some engines ignore 'site:': keep only hits on that site
            want = m.group(1).lower().lstrip(".")
            res.hits = [h for h in res.hits if (urlsplit(h.url).hostname or "").lower() == want
                        or (urlsplit(h.url).hostname or "").lower().endswith("." + want)]
        if res.status == "blocked":                    # anti-automation page: no retry, no bypass; pause the engine
            self.db.x("UPDATE explore_queries SET status='pending' WHERE query=? AND engine=? AND region=?",
                      (q["query"], q["engine"], q["region"]))
            self.db.x("INSERT OR REPLACE INTO explore_engine(engine,blocked_until,reason) VALUES(?,?,?)",
                      (WEB_ENGINE, time.time() + float(self.cfg.get("pair_explorer", {}).get("all_backends_pause_s", 1800)), res.reason[:200]))
            self.log(f"explore: every search backend unavailable ({res.reason}); pausing search queries, continuing with direct pages")
            return
        n_pair_pages = 0
        if q["engine"] == "direct" and q["origin"] in HUB_ORIGINS:   # an institution's home page, not a form page
            n_pair_pages = self._hub_crawl(q["query"][5:], q["query"], q["origin"], max_depth)
            stats["pages"] += 1
            stats["pair_pages"] += n_pair_pages
            res.hits = []
        for h in res.hits:
            if h.kind != "page":
                continue
            page = self._visit(h.url, q["query"], q["origin"], 0)
            if not page:
                continue
            stats["pages"] += 1
            if page["pairs"]:
                n_pair_pages += 1
                stats["pair_pages"] += 1
            if page["pairs"] or page["n_ex"] or page.get("rendered") or q["origin"].startswith("user"):   # rendered lists / user libraries: follow
                self._expand_site(page, h.url, q["query"], 0, max_depth)
        self.db.x("UPDATE explore_queries SET status=?, n_results=?, n_pair_pages=?, ts=? WHERE query=? AND engine=? AND region=?",
                  (res.status, len(res.hits), n_pair_pages, utcnow(), q["query"], q["engine"], q["region"]))
        self.log(f"explore q={q['query']!r} [{q['region']}/{q['origin']}] results={len(res.hits)} pair_pages={n_pair_pages}")

def report(db: DB) -> dict:
    db.conn.executescript(SCHEMA)
    q = db.q("SELECT origin, status, count(*) n, sum(n_pair_pages) p FROM explore_queries GROUP BY origin, status")
    pages = db.q("SELECT status, count(*) n, sum(n_pair_docs) p FROM explore_pages GROUP BY status")
    hosts = db.q("SELECT host, lang, pages, pair_docs FROM explore_hosts WHERE productive=1 ORDER BY pair_docs DESC")
    return {"queries": [dict(r) for r in q], "pages": [dict(r) for r in pages], "productive_hosts": [dict(r) for r in hosts],
            "by_lang": dict(collections.Counter(r["lang"] for r in hosts)), "coverage": coverage(db)}


DOC_URL_RX = re.compile(r"\.(hwpx?|docx?|odt|ods|odp|xlsx?|pptx?|pdf|zip|jpe?g|png|gif|tiff?|rtf|txt|csv|jtd)(?:$|[?#&])"
                        r"|[?&](?:file|filename|filedisplay|fn)=[^&]*\.(?:hwpx?|docx?|odt|xlsx?|pdf)\b", re.I)


def add_seed_pages(db: DB, urls: list[str], origin: str = "user") -> int:
    """Pages found outside the loop (e.g. by a person or another search engine) enter the same site expansion."""
    db.conn.executescript(SCHEMA)
    for u in urls:
        db.x("INSERT OR IGNORE INTO explore_queries(query,engine,region,origin,status,n_results,ts) VALUES(?,?,?,?,?,?,?)",
             (f"page:{u}", "direct", "", origin, "pending", 0, utcnow()))
    return len(urls)



HUB_ORIGINS = {"directory_tw", "directory_jp", "directory_kr", "cc_host", "open_data_host"}
HUB_LINK_RX = re.compile(r"(表單|書表|表格|下載|下载|範例|申辦|便民|行政單位|行政单位|處室|单位|單位|業務|服務|服务|"
                         r"様式|書式|申請|届出|ダウンロード|記入例|手続|"
                         r"서식|양식|자료실|민원|신청|다운로드|"
                         r"\bforms?\b|download|templates?|administrat|offices?|departments?|services?|accounting|finance|registrar|"
                         r"personnel|human resources|research office|"
                         r"[\u4e00-\u9fff]{1,6}(處|室|中心|組|课|課|局)(?![\u4e00-\u9fff])|[가-힣]{1,6}(과|처|팀|센터)(?![가-힣]))", re.I)
_TWO_LEVEL = re.compile(r"\.(edu|gov|org|ac|go|or|co|lg|ne|re|com|net)\.[a-z]{2}$")


def org_domain(url: str) -> str:
    """Registrable domain of an organisation: ``acco.ncku.edu.tw`` -> ``ncku.edu.tw``, ``www.city.yokohama.lg.jp`` ->
    ``yokohama.lg.jp`` (approximation of the public-suffix rule for the TLDs this project visits)."""
    h = (urlsplit(url).hostname or "").lower()
    parts = h.split(".")
    n = 3 if _TWO_LEVEL.search(h) else 2
    if h.endswith(".lg.jp") and len(parts) >= 4 and parts[-4] in ("city", "town", "vill", "pref"):
        n = 3
    elif h.endswith(".jp") and not _TWO_LEVEL.search(h):       # city.funabashi.chiba.jp: one municipality per name, not one per prefecture
        for i, lab in enumerate(parts[:-2]):
            if lab in ("city", "town", "vill", "pref", "metro"):
                n = len(parts) - i
                break
    return ".".join(parts[-n:])


def hub_links(html: bytes, page: str, org: str) -> list[str]:
    """Links of the same organisation whose text or path points to forms, downloads or offices."""
    out = []
    for h in extract_links(html, page, same_domain_only=False):
        host = (urlsplit(h.url).hostname or "").lower()
        if h.kind != "page" or not (host == org or host.endswith("." + org)):
            continue
        if HUB_LINK_RX.search(unicodedata.normalize("NFKC", (h.title or "") + " " + unquote(urlsplit(h.url).path))):
            out.append(h.url.split("#")[0])
    return list(dict.fromkeys(out))[:40]


def url_template(url: str) -> str:
    """Page template of a URL: host + path with digit runs and long ids replaced (``/p/412-1030-2310.php`` ->
    ``/p/#-#-#.php``); query keys kept, values dropped."""
    sp = urlsplit(url)
    p = re.sub(r"[0-9a-f]{16,}", "*", sp.path.lower())
    p = re.sub(r"\d+", "#", p)
    keys = "&".join(sorted(k for k in parse_qs(sp.query)))
    return f"{(sp.hostname or '').lower()}{p}?{keys}"


def sitemap_seeds(db: DB, client, max_maps: int = 25, max_urls: int = 50000, per_host: int = 150, log=print) -> dict:
    """Strategy S3.3: for every host where a page showed labelled examples, read its sitemap(s) and queue the pages that
    share the URL template of those productive pages (same CMS page type: form library / board post)."""
    db.conn.executescript(SCHEMA)
    good = db.q("SELECT host, url FROM explore_pages WHERE n_examples > 0 OR n_pair_docs > 0")
    templates: dict = collections.defaultdict(set)
    for r in good:
        templates[r["host"]].add(url_template(r["url"]))
    known = {r["url"] for r in db.q("SELECT url FROM explore_pages")}
    stats = collections.Counter()
    for host, tpls in sorted(templates.items()):
        row = db.one("SELECT robots_txt FROM policy_cache WHERE domain=?", (host,))
        maps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", row["robots_txt"] or "") if row else []
        maps = maps or [f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"]
        seen_maps, locs = set(), []
        while maps and len(seen_maps) < max_maps and len(locs) < max_urls:
            sm = maps.pop(0)
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            fr = client.fetch(sm, purpose="page")
            if fr.status != "ok" or not fr.content:
                continue
            text = fr.content.decode("utf-8", "replace")
            found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
            if "<sitemapindex" in text[:2000]:
                maps.extend(found)
            else:
                locs.extend(found)
        picked = [u for u in dict.fromkeys(x.replace("&amp;", "&") for x in locs) if url_template(u) in tpls and u not in known][:per_host]
        stats["hosts"] += 1
        stats["sitemap_urls"] += len(locs)
        stats["seeds"] += add_seed_pages(db, picked, origin="sitemap") if picked else 0
        if locs:
            log(f"sitemap {host}: {len(locs)} urls, {len(picked)} pages of a productive template")
    return dict(stats)
