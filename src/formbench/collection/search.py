"""SearchProvider adapters behind one interface (AGENTS.md §3.1).

Providers: general web search (DuckDuckGo HTML, SearXNG, Brave API), official document search (law.go.kr
statutory forms), public APIs (GitHub, MediaWiki/Wikimedia Commons, Internet Archive), sitemap, recursive link
discovery, local import and recorded result import. Every provider goes through HttpClient => PolicyGate.
"""
from __future__ import annotations

import difflib
import dataclasses
import functools
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..util import Status, path
from .fetch import HttpClient

DOC_EXTS = (".hwpx", ".hwp", ".docx", ".odt", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
DOWNLOAD_HINT_RX = re.compile(r"(download|filedown|file_down|atchfile|fileid=|attach|dwld|getfile|bbsfile|flDownload|/dl-\d+)", re.I)
EXT_IN_TEXT_RX = re.compile(r"\.(hwpx|hwp|docx|odt|pdf)\b", re.I)


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0
    kind: str = "page"                  # page | document
    guessed_format: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ProviderResult:
    provider: str
    engine: str
    query: str
    status: str                         # ok | blocked | policy_denied | blocked_by_environment | error | offline_miss
    hits: list[SearchHit] = field(default_factory=list)
    reason: str = ""


def guess_format(url: str, text: str = "") -> str | None:
    p = unquote(urlsplit(url).path).lower()
    for e in DOC_EXTS:
        if p.endswith(e):
            return e.lstrip(".").replace("jpg", "jpeg").replace("tif", "tiff").replace("tifff", "tiff")
    m = EXT_IN_TEXT_RX.search(text or "") or EXT_IN_TEXT_RX.search(unquote(url))
    return m.group(1).lower() if m else None


class SearchProvider:
    name = "base"
    engine = "base"

    def __init__(self, client: HttpClient, cfg: dict | None = None):
        self.client = client
        self.cfg = cfg or {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, query: str, limit: int = 10) -> ProviderResult:  # pragma: no cover - interface
        raise NotImplementedError

    def _res(self, query, status, hits=None, reason=""):
        return ProviderResult(self.name, self.engine, query, status, hits or [], reason)

    def _fail(self, query, fr):
        st = {"policy_denied": "policy_denied", "policy_review": "policy_denied", "blocked": "blocked",
              "offline_miss": "offline_miss"}.get(fr.status, "error")
        return self._res(query, st, reason=fr.reason or fr.status)


class DuckDuckGoHtmlProvider(SearchProvider):
    """General web search through the HTML endpoint, only if robots.txt of that host permits it."""
    name, engine = "general_web", "duckduckgo_html"
    URL = "https://html.duckduckgo.com/html/"

    def search(self, query, limit=10):
        fr = self.client.fetch(self.URL, purpose="search", params={"q": query, "kl": self.cfg.get("region", "kr-kr")})
        if fr.status != "ok":
            return self._fail(query, fr)
        soup = BeautifulSoup(fr.content, "html.parser")
        hits = []
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            url = unquote(m.group(1)) if m else href
            if not url.startswith("http") or "duckduckgo.com/y.js" in url:
                continue
            sn = a.find_parent("div", class_="result")
            snippet = sn.select_one(".result__snippet").get_text(" ", strip=True) if sn and sn.select_one(".result__snippet") else ""
            hits.append(SearchHit(url, a.get_text(" ", strip=True), snippet, len(hits) + 1, "document" if guess_format(url) else "page",
                                  guess_format(url)))
            if len(hits) >= limit:
                break
        if not hits and b"anomaly" in fr.content:
            return self._res(query, "blocked", reason="anti-automation challenge page; not circumvented")
        return self._res(query, "ok", hits)


class SearxngProvider(SearchProvider):
    name, engine = "general_web", "searxng"

    def available(self):
        return (bool(self.cfg.get("base_url")), "no SearXNG base_url configured")

    def search(self, query, limit=10):
        ok, why = self.available()
        if not ok:
            return self._res(query, Status.BLOCKED_BY_ENVIRONMENT, reason=why)
        url = self.cfg["base_url"].rstrip("/") + "/search"
        params = {"q": query, "format": "json", **({"language": self.cfg["language"]} if self.cfg.get("language") else {})}
        if self.cfg.get("local"):                      # our own instance on 127.0.0.1: no crawl policy, direct call
            import requests as _rq
            try:
                r = _rq.get(url, params=params, timeout=float(self.cfg.get("timeout", 40)))
            except _rq.RequestException as e:
                return self._res(query, "error", reason=f"local SearXNG unreachable: {e.__class__.__name__}")
            if r.status_code != 200:
                return self._res(query, "blocked" if r.status_code in (403, 429) else "error", reason=f"http {r.status_code}")
            content = r.content
        else:
            fr = self.client.fetch(url, purpose="search", params=params)
            if fr.status != "ok":
                return self._fail(query, fr)
            content = fr.content
        try:
            data = json.loads(content)
        except ValueError:
            return self._res(query, "blocked", reason="non-JSON response (JSON API disabled or bot check)")
        if not data.get("results") and data.get("unresponsive_engines") and len(data["unresponsive_engines"]) >= int(self.cfg.get("min_down_engines", 10)):
            return self._res(query, "error", reason="engines down: " + ", ".join(f"{e[0]}:{e[1]}" for e in data["unresponsive_engines"])[:180])
        hits = [SearchHit(r["url"], r.get("title", ""), r.get("content", ""), i + 1, "document" if guess_format(r["url"]) else "page",
                          guess_format(r["url"])) for i, r in enumerate(data.get("results", [])[:limit])]
        return self._res(query, "ok", hits)


class FailoverSearchProvider(SearchProvider):
    """General web search over several backends in order (local SearXNG, public SearXNG instances with a JSON API,
    DuckDuckGo HTML). A backend that is blocked, rate-limited or failing is paused with exponential back-off
    (state in the database, shared by all worker processes) and the next one answers; nothing is circumvented.
    Inside SearXNG the same happens per engine (ban_time_on_fail)."""
    name, engine = "general_web", "web"
    SCHEMA = ("CREATE TABLE IF NOT EXISTS search_backends(label TEXT PRIMARY KEY, paused_until REAL DEFAULT 0, fails INTEGER DEFAULT 0, "
              "ok INTEGER DEFAULT 0, last_status TEXT, last_reason TEXT, ts TEXT)")
    BASE_PAUSE_S, MAX_PAUSE_S = 60.0, 6 * 3600.0

    def __init__(self, client, cfg=None):
        super().__init__(client, cfg)
        self.backends = []
        backends = self.cfg.get("backends")
        if not backends:                               # default: the order configured for the explorer
            from ..util import load_yaml, path as _path
            backends = (load_yaml(_path("configs", "collection.yaml")).get("pair_explorer") or {}).get("search_backends") or []
        for b in backends:
            if b.get("type") == "searxng":
                prov = SearxngProvider(client, {"base_url": b["base_url"], **b})
            elif b.get("type") == "duckduckgo_html":
                prov = DuckDuckGoHtmlProvider(client, {"region": self.cfg.get("region", b.get("region", "wt-wt"))})
            else:
                continue
            self.backends.append((b.get("label") or b.get("base_url") or b["type"], prov))
        self.db = getattr(client, "db", None)
        if self.db is not None:
            self.db.conn.execute(self.SCHEMA)

    def _state(self, label):
        if self.db is None:
            return {"paused_until": 0, "fails": 0}
        r = self.db.one("SELECT * FROM search_backends WHERE label=?", (label,))
        return dict(r) if r else {"paused_until": 0, "fails": 0}

    def _record(self, label, ok: bool, status: str, reason: str):
        if self.db is None:
            return
        import time as _t
        st = self._state(label)
        fails = 0 if ok else int(st.get("fails") or 0) + 1
        pause = 0.0 if ok else _t.time() + min(self.MAX_PAUSE_S, self.BASE_PAUSE_S * (2 ** (fails - 1)))
        self.db.x("INSERT INTO search_backends(label,paused_until,fails,ok,last_status,last_reason,ts) VALUES(?,?,?,?,?,?,datetime('now')) "
                  "ON CONFLICT(label) DO UPDATE SET paused_until=excluded.paused_until, fails=excluded.fails, ok=ok+excluded.ok, "
                  "last_status=excluded.last_status, last_reason=excluded.last_reason, ts=excluded.ts",
                  (label, pause, fails, 1 if ok else 0, status, (reason or "")[:200]))

    def search(self, query, limit=10):
        import time as _t
        tried = []
        empty = None
        for label, prov in self.backends:
            if self._state(label).get("paused_until", 0) > _t.time():
                tried.append(f"{label}: paused")
                continue
            res = prov.search(query, limit)
            if res.status == "ok" and res.hits:
                self._record(label, True, "ok", "")
                return self._res(query, "ok", res.hits, reason=f"backend={label}")
            if res.status == "ok":                     # answered, nothing found: not a failure, but let another backend try
                self._record(label, True, "ok_empty", "")
                empty = empty or label
                tried.append(f"{label}: no results")
                continue
            self._record(label, False, res.status, res.reason)
            tried.append(f"{label}: {res.status} {res.reason}"[:120])
        if empty:
            return self._res(query, "ok", [], reason="; ".join(tried))
        return self._res(query, "blocked", reason="all backends unavailable: " + "; ".join(tried))


class BraveApiProvider(SearchProvider):
    name, engine = "general_web", "brave_api"

    def available(self):
        return (bool(os.environ.get("BRAVE_API_KEY")), "BRAVE_API_KEY not set")

    def search(self, query, limit=10):
        ok, why = self.available()
        if not ok:
            return self._res(query, Status.BLOCKED_BY_ENVIRONMENT, reason=why)
        self.client.session.headers["X-Subscription-Token"] = os.environ["BRAVE_API_KEY"]
        fr = self.client.fetch("https://api.search.brave.com/res/v1/web/search", purpose="search", params={"q": query, "count": str(limit)})
        self.client.session.headers.pop("X-Subscription-Token", None)
        if fr.status != "ok":
            return self._fail(query, fr)
        data = json.loads(fr.content)
        hits = [SearchHit(r["url"], r.get("title", ""), r.get("description", ""), i + 1, "document" if guess_format(r["url"]) else "page",
                          guess_format(r["url"])) for i, r in enumerate(data.get("web", {}).get("results", [])[:limit])]
        return self._res(query, "ok", hits)


class LawGoKrFormProvider(SearchProvider):
    """Official document search: 국가법령정보센터 별표·서식 (statutory forms; HWP + PDF of the same blank form)."""
    name, engine = "official_search", "law.go.kr_byl"
    BASE = "https://www.law.go.kr/LSW/"
    TABS = {"ls": ("lsBylScListR.do", "lsBylContentsInfoR.do", "lsiSeq"),
            "admrul": ("admRulBylScListR.do", "admRulBylContentsInfoR.do", "admRulSeq")}

    def search(self, query, limit=10):
        hits: list[SearchHit] = []
        tabs = self.cfg.get("tabs", ["ls"])
        for tab in tabs:
            list_do, cont_do, seq_name = self.TABS[tab]
            fr = self.client.fetch(self.BASE + list_do, purpose="search",
                                   params={"q": query, "outmax": str(max(limit, 10)), "pg": str(self.cfg.get("page", 1)),
                                           "section": "bylNm", "tabType": "1"})
            if fr.status != "ok":
                if not hits:
                    return self._fail(query, fr)
                continue
            html = fr.content.decode("utf-8", "replace")
            items = re.findall(r"bylViewAll\('(\d+)','(\d+)'[^)]*\)[^>]*title=\"([^\"]*)\"", html)
            laws = re.findall(r'class="tx2">([^<]*)<', html)
            for i, (byl_seq, parent_seq, title) in enumerate(items[:limit]):
                if "서식" not in title.split("]")[0]:
                    continue                      # 별표(annex tables) are not forms
                fr2 = self.client.fetch(self.BASE + cont_do, purpose="search", params={"bylSeq": byl_seq, seq_name: parent_seq})
                if fr2.status != "ok":
                    continue
                h2 = fr2.content.decode("utf-8", "replace")
                seqs = {k: v for k, v in re.findall(r'id="(han|odt|pdf)FlSeq"[^>]*value="(\d*)"', h2)}
                group = f"law.go.kr:{tab}:{byl_seq}"
                for kind, fmt in (("han", "hwp"), ("pdf", "pdf")):
                    if seqs.get(kind):
                        hits.append(SearchHit(f"{self.BASE}flDownload.do?flSeq={seqs[kind]}", title, laws[i] if i < len(laws) else "",
                                              len(hits) + 1, "document", fmt,
                                              {"form_group": group, "byl_seq": byl_seq, "parent_seq": parent_seq, "tab": tab,
                                               "same_form_other_format": True,
                                               "landing_url": f"{self.BASE}{'lsBylInfoPLinkR' if tab == 'ls' else 'admRulBylInfoPLinkR'}.do?bylSeq={byl_seq}&{seq_name}={parent_seq}"}))
        return self._res(query, "ok", hits)


class GitHubProvider(SearchProvider):
    """Public API: repositories matching the query; lists document files with the repository licence."""
    name, engine = "public_api", "github_api"

    def search(self, query, limit=10):
        fr = self.client.fetch("https://api.github.com/search/repositories", purpose="search",
                               params={"q": query, "per_page": str(min(limit, 10))})
        if fr.status != "ok":
            return self._fail(query, fr)
        hits: list[SearchHit] = []
        for repo in json.loads(fr.content).get("items", [])[: self.cfg.get("max_repos", 3)]:
            lic = (repo.get("license") or {}).get("spdx_id")
            if not lic or lic == "NOASSERTION":
                continue                          # no licence -> no reuse right; skip listing files
            br = repo.get("default_branch", "main")
            ft = self.client.fetch(f"https://api.github.com/repos/{repo['full_name']}/git/trees/{br}", purpose="search", params={"recursive": "1"})
            if ft.status != "ok":
                continue
            for node in json.loads(ft.content).get("tree", []):
                p = node.get("path", "")
                if node.get("type") == "blob" and p.lower().endswith((".hwpx", ".hwp", ".odt", ".docx")) and 2000 < node.get("size", 0) < 20_000_000:
                    hits.append(SearchHit(f"https://raw.githubusercontent.com/{repo['full_name']}/{br}/{quote(p)}", p, repo.get("description") or "",
                                          len(hits) + 1, "document", guess_format(p),
                                          {"license_spdx": lic, "repo": repo["full_name"], "license_url": f"https://github.com/{repo['full_name']}/blob/{br}/LICENSE",
                                           "form_group": f"github:{repo['full_name']}:{os.path.dirname(p)}"}))
                    if len(hits) >= limit * 4:
                        break
        return self._res(query, "ok", hits)


class MediaWikiProvider(SearchProvider):
    """Public API: Wikimedia Commons file search with per-file licence metadata."""
    name, engine = "public_api", "wikimedia_commons"
    API = "https://commons.wikimedia.org/w/api.php"

    def search(self, query, limit=10):
        fr = self.client.fetch(self.API, purpose="search", params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:pdf|filetype:bitmap",
            "gsrnamespace": "6", "gsrlimit": str(limit), "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata"})
        if fr.status != "ok":
            return self._fail(query, fr)
        pages = json.loads(fr.content).get("query", {}).get("pages", {})
        hits = []
        for pg in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            ii = (pg.get("imageinfo") or [{}])[0]
            if not ii.get("url") or ii.get("mime") not in ("application/pdf", "image/png", "image/jpeg", "image/tiff"):
                continue
            em = ii.get("extmetadata", {})
            hits.append(SearchHit(ii["url"], pg.get("title", ""), "", len(hits) + 1, "document", guess_format(ii["url"]),
                                  {"license_short": em.get("LicenseShortName", {}).get("value"), "license_url": em.get("LicenseUrl", {}).get("value"),
                                   "landing_url": ii.get("descriptionurl"), "usage_terms": em.get("UsageTerms", {}).get("value")}))
        return self._res(query, "ok", hits)


class InternetArchiveProvider(SearchProvider):
    name, engine = "public_api", "internet_archive"

    def search(self, query, limit=10):
        fr = self.client.fetch("https://archive.org/advancedsearch.php", purpose="search", params={
            "q": f"title:({query}) AND mediatype:texts AND licenseurl:*", "fl[]": "identifier,title,licenseurl",
            "rows": str(limit), "output": "json"})
        if fr.status != "ok":
            return self._fail(query, fr)
        docs = json.loads(fr.content).get("response", {}).get("docs", [])
        hits = [SearchHit(f"https://archive.org/download/{d['identifier']}/{d['identifier']}.pdf", str(d.get("title", "")), "", i + 1,
                          "document", "pdf", {"license_url": d.get("licenseurl"), "landing_url": f"https://archive.org/details/{d['identifier']}"})
                for i, d in enumerate(docs)]
        return self._res(query, "ok", hits)


class SitemapProvider(SearchProvider):
    """Sitemap discovery: URLs from robots-declared or configured sitemaps whose path/slug contains the query."""
    name, engine = "sitemap", "sitemap_xml"

    def search(self, query, limit=10):
        hits: list[SearchHit] = []
        for sm in self.cfg.get("sitemaps", []):
            fr = self.client.fetch(sm, purpose="page")
            if fr.status != "ok":
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", fr.content.decode("utf-8", "replace"))
            for u in locs:
                if query.lower() in unquote(u).lower() or guess_format(u):
                    hits.append(SearchHit(u, "", "", len(hits) + 1, "document" if guess_format(u) else "page", guess_format(u)))
                if len(hits) >= limit:
                    break
        return self._res(query, "ok", hits)


class SiteSearchProvider(SearchProvider):
    """Official site-internal search via a configured URL template (``{query}`` placeholder)."""
    name, engine = "official_search", "site_search"

    def search(self, query, limit=10):
        hits: list[SearchHit] = []
        status, reason = "ok", ""
        for site in self.cfg.get("sites", []):
            url = site["url"].replace("{query}", quote(query))
            fr = self.client.fetch(url, purpose="search")
            if fr.status != "ok":
                status, reason = ("policy_denied" if fr.status.startswith("policy") else "error"), fr.reason
                continue
            status = "ok"
            for h in extract_links(fr.content, fr.final_url or url, same_domain_only=True):
                if h.kind == "document" or re.search(site.get("result_pattern", r"view|detail|bbs|board|nttId|seq="), h.url, re.I):
                    h.rank = len(hits) + 1
                    hits.append(h)
                if len(hits) >= limit:
                    break
        return self._res(query, status if hits or status != "ok" else "ok", hits, reason)


class LocalImportProvider(SearchProvider):
    """Local file import: files under configured directories whose name contains the query (or all if query='*')."""
    name, engine = "local_import", "filesystem"

    def search(self, query, limit=1000):
        hits = []
        for root in self.cfg.get("roots", []):
            base = Path(root["path"]).expanduser()
            if not base.exists():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.suffix.lower() in DOC_EXTS and (query == "*" or query.lower() in p.name.lower()):
                    hits.append(SearchHit(p.resolve().as_uri(), p.name, "", len(hits) + 1, "document", guess_format(p.name),
                                          {"rights": root.get("rights", {}), "form_group": f"local:{root.get('name', base.name)}:{p.parent.name}"}))
                    if len(hits) >= limit:
                        break
        return self._res(query, "ok", hits)


class RecordedResultsProvider(SearchProvider):
    """Import of search results recorded offline (e.g. assistant-run web search), one JSON object per line:
    ``{"engine":..., "query":..., "rank":..., "url":..., "title":...}``. Results are logged like any other provider."""
    name, engine = "recorded", "recorded_jsonl"

    def search(self, query, limit=50):
        hits = []
        engine = self.engine
        for f in self.cfg.get("files", []):
            fp = Path(f)
            if not fp.exists():
                continue
            for line in fp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("query") == query:
                    engine = r.get("engine", engine)
                    hits.append(SearchHit(r["url"], r.get("title", ""), r.get("snippet", ""), int(r.get("rank", len(hits) + 1)),
                                          "document" if guess_format(r["url"]) else "page", guess_format(r["url"])))
        res = self._res(query, "ok", hits[:limit])
        res.engine = engine
        return res


@functools.lru_cache(maxsize=4)
def _soup(html: bytes) -> BeautifulSoup:
    """Parsed page, shared by the analysis steps of one visit (read-only use; parsing was ~8x per page, CPU-bound)."""
    return BeautifulSoup(html, "html.parser")


def extract_links(html: bytes, base_url: str, same_domain_only: bool = False) -> list[SearchHit]:
    """Recursive-link discovery step: find document links (by extension, link text or download-like href).
    Callers may modify the returned hits; the cached result is copied."""
    return [dataclasses.replace(h, extra=dict(h.extra)) for h in _extract_links(bytes(html), base_url, same_domain_only)]


@functools.lru_cache(maxsize=16)
def _extract_links(html: bytes, base_url: str, same_domain_only: bool) -> tuple:
    soup = _soup(html)
    base_dom = (urlsplit(base_url).hostname or "").lower()
    out: dict[str, SearchHit] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        sp = urlsplit(url)
        if sp.scheme not in ("http", "https"):
            continue
        if same_domain_only and (sp.hostname or "").lower() != base_dom:
            continue
        text = a.get_text(" ", strip=True)[:200]
        title_attr = a.get("title", "") or ""
        alts = " ".join(img.get("alt", "") for img in a.find_all("img"))[:200]       # icon-only links carry the file name in alt
        if not text:
            text = alts if guess_format("", alts) else title_attr or alts
        elif title_attr and guess_format("", title_attr) and len(text) <= 12:     # icon text 'odt' / '.docx', real name in title
            text = title_attr
        fmt = guess_format(url, text + " " + title_attr + " " + alts)
        is_doc = bool(fmt) and (unquote(sp.path).lower().endswith(DOC_EXTS) or bool(DOWNLOAD_HINT_RX.search(url)) or a.has_attr("download")
                                or bool(re.search(r"(檔案下載|档案下载|파일\s*다운로드|ダウンロード|\bdownload\b)", title_attr, re.I)))
        if url in out and not out[url].title and text:          # thumbnail link first, titled link later: keep the title
            del out[url]
        if url not in out:
            out[url] = SearchHit(url, text, "", len(out) + 1, "document" if is_doc else "page", fmt if is_doc else None,
                                 {"query_keys": sorted(parse_qs(sp.query).keys())})
    return tuple(out.values())


EXAMPLE_RX = re.compile(r"((?:^|(?<=[\s_\-–(\[]))(?:모범\s*)?예시(?=\s*(?:$|[\s_\-–)\].(\[]))|"
                        r"(?:^|(?<=[\s_\-–(\[/]))completed(?=\s*(?:$|[)\].(\[–-])|\s+\(|\s+version)|"
                        r"(?<=/)(?:completed|filled|example|sample)[-_](?=[^/]*\.(?:docx|odt|hwpx|pdf|xlsx)$)|"
                        r"작성\s*예시|작성\s*례|기재\s*례|기재\s*예시|작성\s*사례|견본|예시\s*\)|\(\s*예시|\[\s*예시|예시\s*\]|_예시|예시_|"
                        r"記入例|記載例|記入見本|見本|作成例|填写示例|填写样例|样例|範例|范例|示例|填寫範本|填写范本|"
                        r"(?<![a-z])(?:worked|completed)\s+(?:example|form|version)s?(?![a-z])|example\s+of\s+(?:a\s+)?completed|"
                        r"(?<![a-z])(?:exemple|ejemplo|exemplo|beispiel|ausfüllbeispiel|musterbeispiel|esempio|voorbeeld|ingevuld|preenchido|contoh\s+pengisian)(?![a-z])|"
                        r"exemple\s+rempli|ejemplo\s+cumplimentado|ตัวอย่าง(?:การกรอก)?|"
                        r"(?<![a-z])(?:sample|example|specimen)(?![a-z])|filled[\s_-]*(?:in|out)?(?![a-z]))", re.I)
NAME_SIMILARITY_MIN = 0.8
LIBRARY_LINK_RX = re.compile(r"(様式|書式|記入例|記載例|作成例|申請書|届出書|서식|양식|작성\s*예시|민원\s*편람|기재례|"
                             r"表單|書表|範例|申請書|样表|表格下载|"
                             r"\bforms?\b|templates?|completed\s+example|\bformulare?\b|ausfüllbeispiel)", re.I)
POST_LINK_RX = re.compile(r"(view\.do|/view|read\.do|/read\b|Detail|selectNttInfo|selectBbsNttView|BD_selectBbs|amode=view|mode=view)", re.I)
_JS_DOWNLOAD_RX = re.compile(r"""['"]((?:\.{0,2}/|https?://)[^'"]*(?:down|file|attach)[^'"]*)['"]""", re.I)


_DOWNLOAD_HREF_RX = re.compile(r"(down|file|attach|fms)", re.I)
_ICON_FORMAT_RX = re.compile(r"(?<![a-z])(hwpx|hwp|docx|odt|pdf|doc|xlsx|xls)(?![a-z])", re.I)


_FORM_NOUN_END_RX = re.compile(r"(書|表|簿|届|單|单|証|票|서|표|부|대장|증|form|sheet|register|report)$", re.I)


def _names_another_form(a: str, b: str) -> bool:
    """'事業報告書' vs '事業報告書等提出書': what the longer name adds is itself a form name, so it is a different form
    ('여권발급신청서' vs '여권발급신청서작성요령' adds guidance wording and stays the same form)."""
    short, long_ = sorted((a, b), key=len)
    rest = long_.replace(short, "", 1)
    return len(rest) >= 2 and bool(_FORM_NOUN_END_RX.search(rest))


def pair_key(text: str) -> str:
    """Form name with publisher example markers, extensions, sizes and decoration removed (pairing key)."""
    t = unicodedata.normalize("NFKC", unquote(text or ""))          # full-width ＰＤＦ / １－１ fold to ASCII
    t = re.sub(r"[【\[(（]\s*[^】\])）]{0,20}(更新|개정|updated?|revised)\s*[】\])）]", " ", t, flags=re.I)   # revision stamps differ per file
    t = re.sub(r"\.(hwpx|hwp|pdf|docx|odt|doc)\b", " ", t, flags=re.I)
    t = re.sub(r"[\[(／/]?\s*[\d.,]+\s*(kb|mb|byte|bytes)\s*[\])]?", " ", t, flags=re.I)
    t = re.sub(r"(ファイル|파일|\bfiles?\b)", " ", t, flags=re.I)
    t = re.sub(r"(下載|下载|다운로드|downloads?)\s*[:：]?\s*\d+", " ", t, flags=re.I)                 # download counters
    t = re.sub(r"(t[ée]l[ée]charger|mod[èe]le\s+vierge|vierge|descargar|plantilla\s+en\s+blanco|herunterladen|leer|em\s+branco|en\s+blanco|"
               r"\bkosong\b|\bleeg\b|\bunduh\b|baixar|ดาวน์โหลด|แบบฟอร์มเปล่า)", " ", t, flags=re.I)
    t = re.sub(r"(檔案下載|档案下载|另開新視窗|另开新窗口|開新視窗|新視窗|空白表格|空白表單|空白|blank|"
               r"open\s*office\s*writer\s*格式|microsoft\s*office\s*word\s*格式|格式)", " ", t, flags=re.I)
    t = re.sub(r"(參考|参考|参照|填寫|填写|填表)\s*[-_]?\s*(?=範例|范例|範本|范本|样例|示例)", " ", t)   # "參考範例", "填寫範例" are one marker
    t = re.sub(r"(開啟|开启|打開)(?=\s*新)", " ", t)                                              # "(開啟新視窗)"
    t = EXAMPLE_RX.sub(" ", t)
    t = re.sub(r"(다운로드|미리보기|바로보기|첨부\s*파일|download|preview|양식|서식|様式|blank|template|\bform\b|"
               r"opens in new window|\bms word\b|\bword\b|\bexcel\b|一太郎|ワード|エクセル|\bopendocument\b|\bpdf\b|\bodt\b|\bdocx?\b|\bhtml\b|\d+\s*pages?)", " ", t, flags=re.I)
    t = re.sub(r"\(\s*\d+\s*\)$", " ", t.strip())                       # browser duplicate suffix "(1)"
    return re.sub(r"[\W_]+", "", t).lower()


FILE_ROLE_RX = re.compile(r"^(blank|empty|example|examples|sample|samples|template|templates|filled|completed|complete|worked|specimen|"
                          r"model|demo|en|eng|english|ko|kr|final|v\d+|\d+)$", re.I)


CONTENT_CHOICE_MAX = 8      # blanks offered to one example for the content choice (more: too ambiguous to fetch)


def file_key(url: str) -> str:
    """Pairing key from the file name (template sites that label every link 'Download DOCX'):
    'freelance-invoice-blank-1239dda63a0d.docx' and 'freelance-invoice-example-3c17863b483a.docx' -> 'freelanceinvoice'.
    Role words, language codes, versions and content hashes are dropped; '' when no real name remains."""
    name = unicodedata.normalize("NFKC", unquote(urlsplit(url).path).rsplit("/", 1)[-1]).lower()
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", name)
    toks = [t for t in re.split(r"[\s\-_.()+,]+", name) if t]
    toks = [t for t in toks if not FILE_ROLE_RX.match(t) and not re.fullmatch(r"[0-9a-f]{6,}", t)]
    key = "".join(toks)
    return key if len(key) >= 6 and re.search(r"[a-z\u3131-\ud7a3]{3}", key) else ""


LIST_STATE_PARAM_RX = re.compile(r"^(pageindex|pageunit|pageno|page|cpage|curpage|nowpage|page_no|pagesize|listcnt|recordcountperpage|"
                                 r"searchcondition|searchkeyword|searchcnd|searchwrd|searchtype|searchtext|searchword|searchfield|"
                                 r"sc|sw|skey|sval|srchkey|srchword|srchtext|keyfield|keyword|startdate|enddate|ordr|order|sort)$", re.I)
POST_ID_PARAM_RX = re.compile(r"^(idx|bidx|bcidx|bdidx|nttid|nttno|list_no|seq|no|num|articleno|article_?id|boardseq|gsgeul_no|datasid|"
                              r"peti_seq|wr_id|document_srl|post_?id|gosinttno|cffdnno|bod_sn|dataid|data_?no|board_?no)$", re.I)


def canonical_post_url(url: str) -> str:
    """A board post reached from list pages carries the list state (pageIndex, search terms): the same post under many
    URLs. Drop list-state parameters when the URL also names a post id; other URLs are returned unchanged."""
    sp = urlsplit(url)
    if not sp.query:
        return url
    params = parse_qsl(sp.query, keep_blank_values=True)
    if re.search(r"list", sp.path, re.I) or not any(POST_ID_PARAM_RX.match(k) and v for k, v in params):
        return url
    kept = [(k, v) for k, v in params if not LIST_STATE_PARAM_RX.match(k)]
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(kept), ""))


GENERIC_LINK_RX = re.compile(r"^\W*(pdf|word|excel|docx?|hwpx?|odt|xlsx?|一太郎|ワード|エクセル|(?:pdf|word|ワード|エクセル)\s*(?:形式|版|ファイル)|"
                             r"download|다운로드|ダウンロード|미리보기|바로보기|파일|file|"
                             r"here|click here|여기|こちら)\W*$", re.I)
_CONTAINERS = ["li", "td", "dd", "p"]


def _own_text(el) -> str:
    """Text of an element without nested lists / tables (the label of this list item or cell)."""
    parts = []
    for c in el.children:
        if getattr(c, "name", None) in ("ul", "ol", "table", "dl"):
            continue
        parts.append(c.get_text(" ", strip=True) if hasattr(c, "get_text") else str(c).strip())
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _inline_context(a) -> str:
    """Label of a link inside a block that lists many forms separated by <br> (``<strong>name</strong><br>label links<br>``):
    the nearest preceding bold form name + the text on the link's own line."""
    name, line, on_line = "", [], True
    for sib in a.previous_siblings:
        tag = getattr(sib, "name", None)
        if tag == "br":
            on_line = False
            continue
        if tag in ("strong", "b", "h3", "h4", "h5", "h6"):
            name = sib.get_text(" ", strip=True)
            break
        if not on_line:
            continue
        t = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
        if tag == "a" and GENERIC_LINK_RX.match(unicodedata.normalize("NFKC", t or "")):
            continue
        if t:
            line.append(t)
    return re.sub(r"\s+", " ", f"{name} {' '.join(reversed(line))}").strip()


def _multi_form_block(box) -> bool:
    links = [x for x in box.find_all("a", href=True) if guess_format(x["href"], x.get_text(" ", strip=True))]
    return len(links) > 3 and box.find("br") is not None and box.find(["strong", "b"]) is not None


def link_context(a) -> str:
    """For links labelled only 'Word' / 'PDF' / '다운로드': heading > parent item > row name > column header > own line."""
    box = a.find_parent(_CONTAINERS)
    if box is None:
        return ""
    if _multi_form_block(box):
        return _inline_context(a)[:300]
    bits = []
    head = box.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    if box.name == "li":
        up = box.find_parent("li")
        if up is not None:
            bits.append(_own_text(up))
    if box.name == "td":
        tr = box.find_parent("tr")
        cells = tr.find_all(["td", "th"], recursive=False) if tr is not None else []
        if cells and cells[0] is not box:
            bits.append(cells[0].get_text(" ", strip=True))
        table = box.find_parent("table")
        hdr = table.find("tr") if table is not None else None
        if hdr is not None and hdr is not tr and box in cells:
            hcells = hdr.find_all(["th", "td"], recursive=False)
            i = cells.index(box)
            if i < len(hcells):
                bits.append(hcells[i].get_text(" ", strip=True))
    line = _own_text(box)
    for x in box.find_all("a"):                       # sibling format links ('一太郎 Word PDF') are not part of the label
        t = x.get_text(" ", strip=True)
        if t and GENERIC_LINK_RX.match(unicodedata.normalize("NFKC", t)):
            line = line.replace(t, " ")
    if box.name == "p" and not pair_key(line):        # <td><p>Form name</p><p><a>様式（ワード）</a></p>: the name is the paragraph above
        prev = box.find_previous_sibling(lambda t: t.name in ("p", "div", "span", "strong", "b") and t.find("a") is None and t.get_text(strip=True))
        if prev is not None:
            line = prev.get_text(" ", strip=True)
    bits.append(line)
    # the section heading names the form only when nothing closer does (one heading per form); a heading shared by a
    # table of many forms would make every name look alike
    if head is not None and not any(pair_key(b) for b in bits):
        bits.insert(0, head.get_text(" ", strip=True))
    return re.sub(r"\s+", " ", " ".join(b for b in bits if b)).strip()[:300]


def page_document_links(html: bytes, base_url: str) -> list[SearchHit]:
    """Document links of a page, including board attachments behind ``javascript:f_down('./down.do?...')`` calls.
    Links whose text is only a format name get their label from the surrounding list item / table cell."""
    hits = [h for h in extract_links(html, base_url) if h.kind == "document"]
    seen = {h.url for h in hits}
    soup = _soup(bytes(html))
    # generic: a format name only ("Word"), or nothing left of the name once labels / sizes are removed ("作成例（ワード：38KB）")
    generic = {h.url for h in hits if not h.title or not pair_key(h.title)
               or GENERIC_LINK_RX.match(unicodedata.normalize("NFKC", re.sub(r"[\[(（].*?[\])）]", "", h.title)))}
    for a in soup.find_all("a", href=True):          # every link of a multi-form block needs the form name, labelled or not
        box = a.find_parent(_CONTAINERS)
        if box is not None and _multi_form_block(box):
            generic.add(urljoin(base_url, a["href"].strip()).split("#")[0])
    if generic:
        for a in soup.find_all("a", href=True):
            url = urljoin(base_url, a["href"].strip()).split("#")[0]
            if url in generic:
                ctx = link_context(a)
                if ctx:
                    for h in hits:
                        if h.url == url:
                            h.extra["context_label"] = ctx
                            h.title = f"{ctx} [{h.title}]" if h.title else ctx
                generic.discard(url)
    for a in soup.find_all("a", href=True):        # board download endpoints without extension: the file-type icon names it
        href = str(a["href"]).strip()                # (mma.go.kr: <a href="/boardFileDown.do?..."><img alt="hwpx 파일"> name</a>)
        if href.lower().startswith("javascript") or not _DOWNLOAD_HREF_RX.search(href):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if url in seen:
            continue
        icons = " ".join(f"{i.get('alt', '')} {str(i.get('src', '')).rsplit('/', 1)[-1]}" for i in a.find_all("img"))
        m = _ICON_FORMAT_RX.search(icons)
        text = a.get_text(" ", strip=True)
        if not m or not text:
            continue
        seen.add(url)
        hits.append(SearchHit(url, text[:200], "", len(hits) + 1, "document", m.group(1).lower(), {"via": "format_icon"}))
    for a in soup.find_all("a"):
        href = str(a.get("href", "")).strip()
        if not (href.lower().startswith("javascript") or "onclick" in a.attrs):
            continue
        m = _JS_DOWNLOAD_RX.search(href + " " + str(a.get("onclick", "")))
        if not m:
            continue
        url = urljoin(base_url, m.group(1).replace("&amp;", "&"))
        text = a.get_text(" ", strip=True) or a.get("title", "")
        fmt = guess_format(url, text)
        if url in seen or not fmt:
            continue
        seen.add(url)
        hits.append(SearchHit(url, text[:200], "", len(hits) + 1, "document", fmt, {"via": "javascript_download"}))
    return hits


class GovUkSearchProvider(SearchProvider):
    """GOV.UK site search API (public JSON API; OGL v3 content). Returns publication pages of document type ``form``."""
    name, engine = "official_search", "gov.uk_search_api"
    URL = "https://www.gov.uk/api/search.json"

    def search(self, query, limit=20):
        hits: list[SearchHit] = []
        for start in range(0, limit, 100):          # the API returns at most 100 results per request
            fr = self.client.fetch(self.URL, purpose="search", params={"q": query, "count": str(min(100, limit - start)), "start": str(start),
                                                                       "fields": "title,link,description",
                                                                       "filter_content_store_document_type": self.cfg.get("document_type", "form")})
            if fr.status != "ok":
                return self._fail(query, fr) if not hits else self._res(query, "ok", hits, f"stopped at start={start}: {fr.status}")
            res = json.loads(fr.content).get("results", [])
            hits.extend(SearchHit("https://www.gov.uk" + r["link"], r.get("title", ""), (r.get("description") or "")[:300], len(hits) + 1, "page")
                        for r in res if r.get("link", "").startswith("/"))
            if len(res) < 100:
                break
        return self._res(query, "ok", hits)


class PairedAttachmentProvider(SearchProvider):
    """Observed (blank form, filled example) pairs published side by side on one page.

    Public form libraries often attach a form and its "작성예시/작성례/기재례" (a filled specimen with fictitious values)
    to the same row or post. Every document link of a seed page is classified by its *publisher label* into
    ``blank`` / ``example``; links whose names agree after removing the label form a pair group. The publisher label
    is evidence, not proof: pairs are registered as ``observed_candidate`` and verified in the review queue."""
    name, engine = "observed_pair_pages", "page_attachments"

    def search(self, query, limit=10):
        pages = list(self.cfg.get("seed_pages", []))
        for f in self.cfg.get("seed_files", []) or []:
            p = path(f) if not os.path.isabs(f) else Path(f)
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        r = json.loads(line)
                        if query.startswith("pairs:r") and str(r.get("round", 1)) != query[len("pairs:r"):]:
                            continue                   # one discovery unit per recorded search round (resumable)
                        u = r.get("page") or r.get("url")
                        if u and not guess_format(u):
                            pages.append(u)
        if not query.startswith(("pairs:", "*pairs")):     # a search query: seed pages come from the configured search providers
            pages = []
            for sc in self.cfg.get("seed_search", []) or []:
                prov = PROVIDERS[sc["type"]](self.client, sc)
                res = prov.search(query, int(sc.get("limit", 20)))
                pages.extend(h.url for h in res.hits if h.kind == "page")
        hits: list[SearchHit] = []
        fails = 0
        max_posts = int(self.cfg.get("max_posts_per_seed", 30))
        queue = [(p, 0) for p in dict.fromkeys(pages)]
        visited: set[str] = set()
        host_pages: dict[str, int] = {}
        site_docs: dict[str, list] = {}
        while queue:
            page, depth = queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            fr = self.client.fetch(page, purpose="expand")
            if fr.status != "ok":
                fails += 1
                continue
            found = self.pairs_on_page(fr.content, page, self.cfg.get("exclude_formats", ()), self.cfg.get("pair_formats"))
            if self.cfg.get("cross_page", True):
                paired = {h.url for h in found}
                for d, role, key in self.labelled_docs(fr.content, page, self.cfg.get("exclude_formats", ())):
                    if d.url not in paired and key and (self.cfg.get("pair_formats") is None or d.guessed_format in self.cfg["pair_formats"]):
                        site_docs.setdefault(urlsplit(page).hostname, []).append((d, role, key, page))
            for h in found:
                h.rank = len(hits) + 1
                h.extra["seed_depth"] = depth
                hits.append(h)
            if depth == 0 and (not found or self.cfg.get("siblings")):   # board list / neighbouring posts of the same board
                queue.extend((u, 1) for u in self.post_links(fr.content, page)[:max_posts])
            crawl = self.cfg.get("crawl") or {}
            if crawl and depth < int(crawl.get("depth", 2)):   # form libraries: follow same-site links that name forms / examples
                host = urlsplit(page).hostname
                budget = int(crawl.get("max_pages_per_host", 60))
                for u in self.library_links(fr.content, page):
                    if host_pages.get(host, 0) >= budget:
                        break
                    if u not in visited:
                        host_pages[host] = host_pages.get(host, 0) + 1
                        queue.append((u, depth + 1))
        seen_urls = {h.url for h in hits}
        for h in self.cross_page_pairs(site_docs):
            if h.url not in seen_urls:
                h.rank = len(hits) + 1
                hits.append(h)
                seen_urls.add(h.url)
        status = "ok" if hits or fails < len(visited) or not visited else "error"
        return self._res(query, status, hits, f"{fails} of {len(visited)} pages not fetched" if fails else "")

    @staticmethod
    def labelled_docs(html: bytes, page: str, exclude_formats=()) -> list[tuple]:
        """(link, 'blank' | 'example', pairing key) for every document link of a page."""
        excluded = set(exclude_formats or [])
        out = []
        for h in page_document_links(html, page):
            if h.guessed_format in excluded:
                continue
            sp = urlsplit(h.url)
            role = "example" if EXAMPLE_RX.search(unquote(h.title + " " + sp.path + "?" + sp.query)) else "blank"
            out.append((h, role, pair_key(h.title or unquote(sp.path).rsplit("/", 1)[-1])))
        return out

    @staticmethod
    def cross_page_pairs(site_docs: dict) -> list[SearchHit]:
        """Examples whose blank is listed on another page of the same site (form list vs. example list). Stricter than the
        same-page rule: same file format and the same form name after removing labels (no similarity matching)."""
        out: dict[str, SearchHit] = {}
        for host, items in site_docs.items():
            blanks: dict[tuple, list] = {}
            for d, role, key, page in items:
                if role == "blank":
                    blanks.setdefault((key, d.guessed_format), []).append((d, page))
            for d, role, key, page in items:
                if role != "example" or len(key) < 4:
                    continue
                cands = [(b, bp) for b, bp in blanks.get((key, d.guessed_format), []) if bp != page]
                if len({b.url for b, _ in cands}) != 1:      # none, or ambiguous: no pair
                    continue
                b, bpage = cands[0]
                group = f"https://{host}/#site:{key}:{d.guessed_format}"
                for h, r, pg in ((b, "blank", bpage), (d, "example", page)):
                    x = out.get(h.url) or SearchHit(h.url, h.title, "", 0, "document", h.guessed_format, dict(h.extra))
                    links = x.extra.setdefault("pair_links", [])
                    if not any(lk["group"] == group for lk in links):
                        links.append({"group": group, "page": pg, "role": r, "key": key, "name_relation": "same_site_other_page",
                                      "link_text": h.title[:200], "other_page": bpage if r == "example" else page})
                    out[h.url] = x
        return list(out.values())

    @staticmethod
    def library_links(html: bytes, page: str) -> list[str]:
        """Same-host HTML links whose text or path names a form library, a form or a filled example."""
        host = urlsplit(page).hostname
        out = []
        for h in extract_links(html, page, same_domain_only=True):
            if h.kind != "page" or urlsplit(h.url).hostname != host or h.url == page:
                continue
            if LIBRARY_LINK_RX.search(unicodedata.normalize("NFKC", h.title + " " + unquote(urlsplit(h.url).path))):
                out.append(h.url)
        return list(dict.fromkeys(out))

    @staticmethod
    def post_links(html: bytes, page: str) -> list[str]:
        """Same-host links that look like posts of a board (view / read / detail pages)."""
        host = urlsplit(page).hostname
        out = []
        for h in extract_links(html, page, same_domain_only=True):
            if h.kind == "page" and urlsplit(h.url).hostname == host and h.url != page and POST_LINK_RX.search(h.url):
                out.append(h.url)
        return list(dict.fromkeys(out))

    @staticmethod
    def pairs_on_page(html: bytes, page: str, exclude_formats=(), pair_formats=None) -> list[SearchHit]:
        excluded = set(exclude_formats or [])
        docs = [h for h in page_document_links(html, page) if h.guessed_format not in excluded]
        return PairedAttachmentProvider.pair_docs(docs, page, pair_formats)

    @staticmethod
    def pair_docs(docs: list[SearchHit], page: str, pair_formats=None) -> list[SearchHit]:
        """Pair the documents of one page by publisher label and name (links, or files captured from buttons).
        Only when no example of the page matches a blank by name (files named by topic) are the page's blanks
        offered to its examples as a content choice."""
        return (PairedAttachmentProvider._pair_docs(docs, page, pair_formats, False)
                or PairedAttachmentProvider._pair_docs(docs, page, pair_formats, True))

    @staticmethod
    def _pair_docs(docs: list[SearchHit], page: str, pair_formats, content_choice: bool) -> list[SearchHit]:
        file_key_of = {h.url: file_key(h.extra.get("captured_name") or h.url) for h in docs}
        def labelled(h):                    # publisher label: link text or file path, never the host name
            sp = urlsplit(h.url)
            return bool(EXAMPLE_RX.search(unquote(h.title + " " + sp.path + "?" + sp.query)))
        role = {h.url: ("example" if labelled(h) else "blank") for h in docs}
        keys = {h.url: pair_key(h.title or unquote(urlsplit(h.url).path).rsplit("/", 1)[-1]) for h in docs}
        blanks = [h for h in docs if role[h.url] == "blank" and keys[h.url]]
        out: dict[str, SearchHit] = {}
        for e in (h for h in docs if role[h.url] == "example" and (keys[h.url] or file_key_of[h.url])):
            ke = keys[e.url]
            exact = [b for b in blanks if keys[b.url] == ke]
            relation = "same_name"
            if not exact:              # "여권발급신청서" vs "여권발급신청서 작성요령": one name contains the other (>= 4 chars)
                exact = [b for b in blanks if min(len(keys[b.url]), len(ke)) >= 4 and (keys[b.url] in ke or ke in keys[b.url])
                         and min(len(keys[b.url]), len(ke)) >= 0.5 * max(len(keys[b.url]), len(ke))
                         and not _names_another_form(keys[b.url], ke)]
                relation = "name_contains"
            if not exact:              # "선택의료급여기관(신규,변경)신청서" vs "(작성예시)선택의료급여기관 신청서": best similar name only
                # ranked by distinct name: the same blank offered as .odt and .pdf is one candidate, not a tie
                scored = sorted({(difflib.SequenceMatcher(None, k, ke).ratio(), k) for k in {keys[b.url] for b in blanks}}, key=lambda x: (-x[0], x[1]))
                if scored and scored[0][0] >= NAME_SIMILARITY_MIN and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                    exact, relation = [b for b in blanks if keys[b.url] == scored[0][1]], f"name_similar:{scored[0][0]:.2f}"
            if not exact:              # labels only ('modèle vierge' / 'exemple rempli'): the page's single blank of this format
                same_fmt_blanks = [b for b in blanks if b.guessed_format == e.guessed_format]
                same_fmt_examples = [x for x in docs if role[x.url] == "example" and x.guessed_format == e.guessed_format]
                if len(same_fmt_blanks) == 1 and len(same_fmt_examples) == 1:
                    exact, relation = same_fmt_blanks, "only_pair_on_page"
            if not exact:              # examples named by topic ('Home visit example with ES responses', 'Completed witness
                # statement (filled-in example)') next to several blanks: all same-format blanks of the page are offered
                # in one choice group and the pairing stage keeps the blank whose text the example contains most
                # (names alone chose wrongly: firsthr witness statement fills the 'Standard' form, not the 'Blank Format' one)
                cands = [b for b in docs if role[b.url] == "blank" and b.guessed_format == e.guessed_format]
                if content_choice and 1 < len(cands) <= CONTENT_CHOICE_MAX:
                    exact, relation = cands, "content_choice"
            if not exact and file_key_of[e.url]:          # generic link texts ('Download DOCX'): the publisher's file names pair them
                fe = file_key_of[e.url]
                cands = [b for b in docs if role[b.url] == "blank" and b.guessed_format == e.guessed_format and file_key_of[b.url]]
                same = [b for b in cands if file_key_of[b.url] == fe]
                if not same:
                    same = [b for b in cands if (file_key_of[b.url] in fe or fe in file_key_of[b.url])
                            and min(len(file_key_of[b.url]), len(fe)) >= 0.75 * max(len(file_key_of[b.url]), len(fe))]
                if len({file_key_of[b.url] for b in same}) == 1:
                    exact, relation = same, "file_name"
            if pair_formats is not None:              # same editable format on both sides only (no PDF, no conversion)
                exact = [b for b in exact if b.guessed_format == e.guessed_format and b.guessed_format in set(pair_formats)]
            for b in exact:
                kb = keys[b.url] or file_key_of[b.url]
                group = f"{canonical_post_url(page)}#{kb}"
                if relation == "content_choice":
                    kb = "choice:" + (ke or file_key_of[e.url] or e.url.rsplit("/", 1)[-1])
                    group = f"{canonical_post_url(page)}#{kb}"
                for h, r in ((b, "blank"), (e, "example")):
                    x = out.get(h.url) or SearchHit(h.url, h.title, "", 0, "document", h.guessed_format, dict(h.extra))
                    links = x.extra.setdefault("pair_links", [])
                    if not any(lk["group"] == group for lk in links):
                        links.append({"group": group, "page": page, "role": r, "key": kb, "name_relation": relation,
                                      "link_text": h.title[:200]})
                    out[h.url] = x
        return list(out.values())


PROVIDERS = {
    "duckduckgo_html": DuckDuckGoHtmlProvider, "searxng": SearxngProvider, "brave_api": BraveApiProvider,
    "law_go_kr": LawGoKrFormProvider, "github": GitHubProvider, "wikimedia_commons": MediaWikiProvider,
    "internet_archive": InternetArchiveProvider, "sitemap": SitemapProvider, "site_search": SiteSearchProvider,
    "local_import": LocalImportProvider, "recorded": RecordedResultsProvider, "paired_pages": PairedAttachmentProvider,
    "govuk_search": GovUkSearchProvider, "failover_web": FailoverSearchProvider,
}


def build_providers(client: HttpClient, cfg: dict) -> list[SearchProvider]:
    out = []
    for p in cfg.get("providers", []):
        if not p.get("enabled", True):
            continue
        cls = PROVIDERS[p["type"]]
        inst = cls(client, {"exclude_formats": cfg.get("exclude_formats", []), "pair_formats": cfg.get("pair_formats"), **p})
        inst.label = p.get("label", p["type"])
        out.append(inst)
    return out
