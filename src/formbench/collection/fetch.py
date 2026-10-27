"""Polite HTTP client and Downloader.

Features: policy gate before every request, per-domain delay, backoff, hard stop on 403/429/CAPTCHA,
streaming with size cap, .part cleanup, sha256 content-addressed store, ETag/Last-Modified revalidation,
full HTTP log, offline replay from cache.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from ..corpus.security import MIME_BY_FORMAT, scan_bytes
from ..util import DB, Status, path, sha256_bytes, utcnow
from .policy import PolicyDecision, PolicyGate

DEFAULT_UA = "formbench-research/0.1 (academic form-editability benchmark; contact: {contact})"
CAPTCHA_RX = re.compile(r"captcha|are you a robot|unusual traffic|비정상적인 접근|자동입력 방지", re.I)


@dataclass
class FetchResult:
    url: str
    status: str                       # ok | not_modified | policy_denied | policy_review | http_error | blocked | too_large | error | offline_miss
    http_status: int | None = None
    content: bytes | None = None
    sha256: str | None = None
    content_type: str | None = None
    filename: str | None = None
    final_url: str | None = None
    from_cache: bool = False
    reason: str = ""
    policy: PolicyDecision | None = None


def _base_bundle() -> bytes:
    """certifi (current) + the system store: the AIA fallback bundle must never be older than the default list."""
    import certifi
    out = Path(certifi.where()).read_bytes()
    sysb = Path("/etc/ssl/certs/ca-certificates.crt")
    return out + (b"\n" + sysb.read_bytes() if sysb.exists() else b"")


class HttpClient:
    def __init__(self, db: DB, policy: PolicyGate | None = None, contact: str = "unset", offline: bool = False,
                 max_bytes: int = 30 * 1024 * 1024, timeout: float = 30.0, max_retries: int = 2, session=None,
                 store_dir: Path | None = None, sleep=time.sleep):
        self.db = db
        self.offline = offline
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.session = session or requests.Session()
        self.extra_ca = path("data", "cache", "tls", "ca_with_aia_intermediates.pem")
        # default verification: requests' current CA list (certifi). The AIA bundle is only a fallback after an SSL error
        # (2026-10-15: a bundle built from an older system store rejected the new Let's Encrypt chain, Root YR).
        self.ssl_fallback = session is None
        self.ua = DEFAULT_UA.format(contact=contact)
        self.store_dir = store_dir or path("data", "raw", "sha256")
        self.cache_dir = path("data", "cache", "http")
        self.policy = policy or PolicyGate(db, fetch_robots=self._fetch_robots, offline=offline)
        if self.policy.fetch_robots is None:
            self.policy.fetch_robots = self._fetch_robots

    # ------------------------------------------------------------------ storage
    def blob_path(self, sha: str, base: Path | None = None) -> Path:
        return (base or self.store_dir) / sha[:2] / sha

    def _store(self, data: bytes, base: Path | None = None) -> str:
        sha = sha256_bytes(data)
        p = self.blob_path(sha, base)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".part")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        return sha

    def cleanup_partials(self) -> int:
        n = 0
        for base in (self.store_dir, self.cache_dir):
            if base.exists():
                for p in base.rglob("*.part"):
                    p.unlink(missing_ok=True)
                    n += 1
        return n

    def _log(self, url, method, status, nbytes, t0, etag=None, lm=None, from_cache=False, error=None):
        self.db.x("INSERT INTO http_log(ts,url,method,status,bytes,elapsed_ms,etag,last_modified,from_cache,error) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (utcnow(), url, method, status, nbytes, int((time.monotonic() - t0) * 1000),
                                                 etag, lm, int(from_cache), error))

    # ------------------------------------------------------------------ TLS: incomplete chains
    def _add_aia_intermediate(self, url: str) -> bool:
        """Server omitted its intermediate certificate: download it from the leaf's AIA 'CA Issuers' URL (what browsers do)
        and verify against system roots + collected intermediates. Verification stays on; nothing is trusted blindly."""
        import ssl

        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        sp = urlsplit(url)
        try:
            pem = ssl.get_server_certificate((sp.hostname, sp.port or 443), timeout=15)
            leaf = x509.load_pem_x509_certificate(pem.encode())
            aia = leaf.extensions.get_extension_for_oid(x509.oid.ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
            urls = [d.access_location.value for d in aia if d.access_method == x509.oid.AuthorityInformationAccessOID.CA_ISSUERS]
        except Exception:      # noqa: BLE001
            return False
        added = False
        for u in urls:
            try:
                r = requests.get(u, timeout=15, headers={"User-Agent": self.ua})
                data = r.content
                cert = x509.load_der_x509_certificate(data) if not data.lstrip().startswith(b"-----") else x509.load_pem_x509_certificate(data)
            except Exception:      # noqa: BLE001
                continue
            self.extra_ca.parent.mkdir(parents=True, exist_ok=True)
            if not self.extra_ca.exists():
                self.extra_ca.write_bytes(_base_bundle())
            block = cert.public_bytes(Encoding.PEM)
            if block not in self.extra_ca.read_bytes():
                with open(self.extra_ca, "ab") as fh:
                    fh.write(b"\n" + block)
            added = True
        return added

    def _verify_fallback(self) -> str | None:
        return str(self.extra_ca) if self.ssl_fallback and self.extra_ca.exists() else None

    # ------------------------------------------------------------------ robots
    def _fetch_robots(self, robots_url: str) -> tuple[int | None, str]:
        t0 = time.monotonic()
        try:
            try:
                r = self.session.get(robots_url, headers={"User-Agent": self.ua}, timeout=min(self.timeout, 15), allow_redirects=True)
            except requests.exceptions.SSLError:
                self._add_aia_intermediate(robots_url)
                fb = self._verify_fallback()
                if not fb:
                    raise
                r = self.session.get(robots_url, headers={"User-Agent": self.ua}, timeout=min(self.timeout, 15), allow_redirects=True, verify=fb)
            self._log(robots_url, "GET", r.status_code, len(r.content), t0)
            return r.status_code, r.text if r.status_code < 300 else ""
        except requests.RequestException as e:
            self._log(robots_url, "GET", None, 0, t0, error=type(e).__name__)
            return None, ""

    # ------------------------------------------------------------------ main fetch
    def fetch(self, url: str, purpose: str = "page", method: str = "GET", params: dict | None = None,
              data: dict | None = None, use_cache: bool = True, referer: str | None = None) -> FetchResult:
        key = url if not params else url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self.db.one("SELECT * FROM http_cache WHERE url=?", (key,)) if use_cache else None
        cache_base = self.store_dir if purpose == "document" else self.cache_dir
        if self.offline:
            if cached and cached["sha256"] and self.blob_path(cached["sha256"], cache_base).exists():
                body = self.blob_path(cached["sha256"], cache_base).read_bytes()
                hdrs = json.loads(cached["headers"] or "{}")
                return FetchResult(url, "ok", cached["status"], body, cached["sha256"], cached["content_type"],
                                   hdrs.get("filename"), hdrs.get("final_url", url), True, "offline cache")
            return FetchResult(url, "offline_miss", reason="offline mode and no cached response")
        dec = self.policy.check(key, purpose=purpose)
        if dec.decision != "allow":
            return FetchResult(url, "policy_denied" if dec.decision == "deny" else "policy_review", reason=dec.reason, policy=dec)
        domain = dec.domain
        headers = {"User-Agent": self.ua, "Accept-Language": "ko,en;q=0.8"}
        if referer:
            headers["Referer"] = referer
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        attempt = 0
        verify = None                                  # None: the session default (certifi)
        while True:
            self.policy.wait_turn(domain, dec.crawl_delay, sleep=self.sleep)
            t0 = time.monotonic()
            try:
                kw = {"verify": verify} if verify else {}
                r = self.session.request(method, url, params=params, data=data, headers=headers, timeout=self.timeout,
                                         stream=True, allow_redirects=True, **kw)
            except requests.RequestException as e:
                self._log(key, method, None, 0, t0, error=type(e).__name__)
                if isinstance(e, requests.exceptions.SSLError) and verify is None:
                    self._add_aia_intermediate(url)    # server without its intermediate: add it (verification stays on)
                    verify = self._verify_fallback()
                    if verify:
                        attempt += 1
                        continue
                attempt += 1
                if attempt > self.max_retries:
                    return FetchResult(url, "error", reason=f"network error: {type(e).__name__}", policy=dec)
                self.sleep(min(60, dec.crawl_delay * (2 ** attempt)))
                continue
            sc = r.status_code
            if sc == 304 and cached and self.blob_path(cached["sha256"], cache_base).exists():
                r.close()
                self._log(key, method, sc, 0, t0, cached["etag"], cached["last_modified"], True)
                body = self.blob_path(cached["sha256"], cache_base).read_bytes()
                hdrs = json.loads(cached["headers"] or "{}")
                return FetchResult(url, "ok", 200, body, cached["sha256"], cached["content_type"], hdrs.get("filename"),
                                   hdrs.get("final_url", url), True, "revalidated (304)", dec)
            if sc in (401, 403, 429) or sc == 451:
                r.close()
                self._log(key, method, sc, 0, t0, error="blocked")
                # never circumvent: stop the whole domain for this run and report
                self.policy.stop_domain(domain, f"http {sc} at {utcnow()}")
                return FetchResult(url, "blocked", sc, reason=f"http {sc}: access refused; domain stopped, no retry", policy=dec)
            if sc >= 500:
                r.close()
                self._log(key, method, sc, 0, t0, error="server error")
                attempt += 1
                if attempt > self.max_retries:
                    return FetchResult(url, "http_error", sc, reason="server error after retries", policy=dec)
                ra = r.headers.get("Retry-After")
                self.sleep(min(120, float(ra) if ra and ra.isdigit() else dec.crawl_delay * (2 ** attempt)))
                continue
            if sc >= 400:
                r.close()
                self._log(key, method, sc, 0, t0, error="client error")
                return FetchResult(url, "http_error", sc, reason=f"http {sc}", policy=dec)
            # final-URL policy re-check after redirects (cross-domain redirect must also be permitted)
            if r.url and urlsplit(r.url).hostname and urlsplit(r.url).hostname.lower() != domain:
                d2 = self.policy.check(r.url, purpose=purpose)
                if d2.decision != "allow":
                    r.close()
                    self._log(key, method, sc, 0, t0, error="redirect policy")
                    return FetchResult(url, "policy_denied" if d2.decision == "deny" else "policy_review",
                                       sc, reason=f"redirect target not permitted: {d2.reason}", policy=d2)
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > self.max_bytes:
                r.close()
                self._log(key, method, sc, 0, t0, error="too large")
                return FetchResult(url, "too_large", sc, reason=f"content-length {clen} > limit", policy=dec)
            buf = bytearray()
            try:
                for chunk in r.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        r.close()
                        self._log(key, method, sc, len(buf), t0, error="too large")
                        return FetchResult(url, "too_large", sc, reason="stream exceeded size limit", policy=dec)
            except requests.RequestException as e:
                self._log(key, method, sc, len(buf), t0, error=type(e).__name__)
                attempt += 1
                if attempt > self.max_retries:
                    return FetchResult(url, "error", sc, reason="stream interrupted", policy=dec)
                continue
            body = bytes(buf)
            ctype = r.headers.get("Content-Type", "")
            if "html" in ctype and CAPTCHA_RX.search(body[:20000].decode("utf-8", "replace")) and len(body) < 30000:
                self._log(key, method, sc, len(body), t0, error="captcha")
                self.policy.stop_domain(domain, f"captcha/anti-bot page at {utcnow()}")
                return FetchResult(url, "blocked", sc, reason="captcha / anti-automation page; not circumvented", policy=dec)
            fname = _filename_from_headers(r.headers.get("Content-Disposition")) or unquote(urlsplit(r.url).path.rsplit("/", 1)[-1])
            sha = self._store(body, cache_base)
            etag, lm = r.headers.get("ETag"), r.headers.get("Last-Modified")
            self._log(key, method, sc, len(body), t0, etag, lm)
            self.db.x("INSERT OR REPLACE INTO http_cache(url,etag,last_modified,sha256,content_type,fetched_at,status,headers) "
                      "VALUES(?,?,?,?,?,?,?,?)", (key, etag, lm, sha, ctype, utcnow(), sc,
                                                  json.dumps({"filename": fname, "final_url": r.url}, ensure_ascii=False)))
            return FetchResult(url, "ok", sc, body, sha, ctype, fname, r.url, False, "", dec)


def _filename_from_headers(cd: str | None) -> str | None:
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", cd, re.I)
    if m:
        try:
            return unquote(m.group(2), encoding=m.group(1) or "utf-8")
        except LookupError:
            return unquote(m.group(2))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if not m:
        return None
    raw = m.group(1).strip()
    if "%" in raw:
        return unquote(raw)
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.encode("latin-1").decode(enc)
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return raw


class Downloader:
    """Fetch a document candidate, sniff + security-scan it, and return storage facts (never trusts extension)."""

    def __init__(self, client: HttpClient, run_av: bool = True):
        self.client = client
        self.run_av = run_av

    def download(self, url: str, referer: str | None = None, declared_name: str | None = None) -> dict:
        res = self.client.fetch(url, purpose="document", referer=referer)
        out = {"url": url, "fetch_status": res.status, "http_status": res.http_status, "reason": res.reason,
               "downloaded_at": utcnow(), "policy": res.policy.to_json() if res.policy else None}
        if res.status != "ok" or res.content is None:
            return out
        name = res.filename or declared_name or ""
        ext = os.path.splitext(name)[1] if name else os.path.splitext(urlsplit(url).path)[1]
        rep = scan_bytes(res.content, declared_ext=ext or None, declared_mime=res.content_type, run_av=self.run_av)
        out.update({"sha256": res.sha256, "file_size": len(res.content), "filename": name, "content_type": res.content_type,
                    "final_url": res.final_url, "original_format": rep.format, "mime_type": MIME_BY_FORMAT.get(rep.format, "application/octet-stream"),
                    "security": rep.to_json(), "from_cache": res.from_cache})
        if rep.format == "html":
            out["fetch_status"] = "not_a_document"
            out["reason"] = "server returned HTML (login wall or viewer page), not a document"
        elif not rep.ok:
            # move to quarantine: keep bytes out of the usable store
            src = self.client.blob_path(res.sha256)
            qdir = path("data", "quarantine")
            qdir.mkdir(parents=True, exist_ok=True)
            if src.exists():
                os.replace(src, qdir / res.sha256)
            out["fetch_status"] = Status.QUARANTINED
            out["reason"] = "; ".join(f["code"] for f in rep.findings if f["severity"] == "block")
        return out
