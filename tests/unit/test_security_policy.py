"""Hostile-document defences (test #13), robots / policy behaviour (test #14), contract consistency (test #10)."""
import io
import zipfile

import pytest

from conftest import contracts_for
from formbench import fixtures
from formbench.collection.fetch import HttpClient
from formbench.collection.policy import PolicyGate, RobotsRules
from formbench.contracts.schema import validate_contract
from formbench.corpus.privacy import review, scan_pii
from formbench.corpus.security import scan_bytes, sniff_format
from formbench.formats.hwpx import HwpxPackage
from formbench.util import DB, FormbenchError


def _rezip(data: bytes, mutate) -> bytes:
    zi = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        entries = {n: zi.read(n) for n in zi.namelist()}
        mutate(entries)
        for n, d in entries.items():
            zo.writestr(n, d)
    return out.getvalue()


def _rejects(data: bytes, code: str):
    with pytest.raises(FormbenchError) as ei:
        HwpxPackage.load(data)
    assert ei.value.code == code, ei.value.code


def test_13_xxe_and_doctype_are_rejected():
    base = fixtures.build("simple_form")
    def m(e):
        e["Contents/section0.xml"] = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + e["Contents/section0.xml"].split(b"?>", 1)[1]
    _rejects(_rezip(base, m), "xml_doctype_or_entity")


def test_13_path_traversal_zip_bomb_encrypted_and_corrupt_packages_are_blocked():
    base = fixtures.build("simple_form")
    _rejects(_rezip(base, lambda e: e.__setitem__("../../evil.xml", b"<x/>")), "path_traversal")
    _rejects(_rezip(base, lambda e: e.__setitem__("Contents/pad.xml", b"<a>" + b"A" * 6_000_000 + b"</a>")), "zip_bomb_ratio")
    _rejects(_rezip(base, lambda e: e.__setitem__("META-INF/manifest.xml", b'<m xmlns:o="urn:x"><o:encryption-data/></m>')), "encrypted_package")
    _rejects(base[: len(base) // 2], "not_hwpx")
    broken = _rezip(base, lambda e: e.__setitem__("Contents/section0.xml", b"<hs:sec><unclosed>"))
    with pytest.raises(FormbenchError) as ei:
        _ = HwpxPackage.load(broken).sections
    assert ei.value.code == "xml_malformed"
    _rejects(_rezip(base, lambda e: e.pop("Contents/header.xml")), "hwpx_structure_invalid")


def test_13_external_references_and_macros_are_reported_never_followed():
    base = fixtures.build("simple_form")
    data = _rezip(base, lambda e: (e.__setitem__("Scripts/JScriptVersion", b"1"), e.__setitem__("Scripts/DefaultJScript.js", b"alert(1)"),
                                   e.__setitem__("Contents/extra.xml", b'<a href="http://attacker.invalid/payload.bin"/>')))
    rep = scan_bytes(data)
    assert rep.has_macros and any("attacker.invalid" in u for u in rep.external_refs)
    assert sniff_format(b"%PDF-1.7 ...") == "pdf" and sniff_format(b"<html><body>login</body></html>") == "html"
    assert any(f["code"] == "extension_mismatch" for f in scan_bytes(base, declared_ext=".pdf").findings)


ROBOTS = """
User-agent: *
Disallow: /private/
Allow: /private/public-forms/
Disallow: /*.hwp$
Crawl-delay: 7

User-agent: formbench-research
Disallow: /search
"""


def test_robots_rfc9309_longest_match_wildcards_and_agent_groups():
    r = RobotsRules.parse(ROBOTS)
    assert r.allowed("/private/x", "otherbot")[0] is False
    assert r.allowed("/private/public-forms/a.pdf", "otherbot")[0] is True          # longest match wins
    assert r.allowed("/files/a.hwp", "otherbot")[0] is False and r.allowed("/files/a.hwpx", "otherbot")[0] is True
    assert r.allowed("/private/x", "formbench-research/0.1")[0] is True               # specific group replaces '*'
    assert r.allowed("/search?q=1", "formbench-research/0.1")[0] is False
    assert r.delay_for("otherbot") == 7
    assert RobotsRules.parse("").allowed("/anything")[0] is True


class _Resp:
    def __init__(self, status, body=b"", headers=None, url=""):
        self.status_code, self.content, self.headers, self.url, self.text = status, body, headers or {}, url, body.decode("utf-8", "replace")

    def iter_content(self, n):
        yield self.content

    def close(self):
        pass


class _Session:
    """Fake transport: scripted responses per URL prefix; records every request."""
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def _find(self, url):
        for k, v in self.routes.items():
            if url.startswith(k):
                return v(url) if callable(v) else v
        return _Resp(404, url=url)

    def get(self, url, **kw):
        self.calls.append(url)
        return self._find(url)

    def request(self, method, url, **kw):
        self.calls.append(url)
        r = self._find(url)
        r.url = r.url or url
        return r


CFG = {"default": {"tos_status": "unreviewed", "allow_page_fetch": True, "allow_document_fetch": False, "crawl_delay": 0.0},
       "domains": {"ok.example": {"allow_document_fetch": True, "tos_status": "reviewed_allow", "redistribution_allowed": True, "rights_status": "test"},
                   "limited.example": {"allow_document_fetch": True}, "down.example": {"allow_document_fetch": True}}, "classes": []}


def _client(tmp_path, routes):
    db = DB(tmp_path / "t.sqlite")
    sess = _Session(routes)
    c = HttpClient(db, contact="test", session=sess, store_dir=tmp_path / "raw", sleep=lambda s: None)
    c.policy = PolicyGate(db, config=CFG, fetch_robots=c._fetch_robots)
    c.cache_dir = tmp_path / "cache"
    return c, sess, db


def test_14_disallowed_paths_429_and_unreachable_robots_are_not_fetched(tmp_path):
    doc = fixtures.build("simple_form")
    c, sess, db = _client(tmp_path, {
        "https://ok.example/robots.txt": _Resp(200, b"User-agent: *\nDisallow: /blocked/\n"),
        "https://ok.example/forms/a.hwpx": _Resp(200, doc, {"Content-Type": "application/hwp+zip", "ETag": "v1"}),
        "https://limited.example/robots.txt": _Resp(404),
        "https://limited.example/": _Resp(429),
        "https://down.example/robots.txt": _Resp(503),
        "https://unknown.example/robots.txt": _Resp(200, b""),
    })
    assert c.fetch("https://ok.example/forms/a.hwpx", purpose="document").status == "ok"
    r = c.fetch("https://ok.example/blocked/b.hwpx", purpose="document")
    assert r.status == "policy_denied" and "robots" in r.reason and not any("blocked/b.hwpx" in u for u in sess.calls)
    r = c.fetch("https://limited.example/x.hwpx", purpose="document")
    assert r.status == "blocked" and r.http_status == 429
    n = len(sess.calls)
    again = c.fetch("https://limited.example/y.hwpx", purpose="document")              # domain is stopped: no retry, no new request
    assert again.status == "policy_denied" and "stopped" in again.reason and len(sess.calls) == n
    r = c.fetch("https://down.example/x.hwpx", purpose="document")
    assert r.status == "policy_review" and "unreachable" in r.reason                    # RFC 9309: 5xx => treat as disallow
    r = c.fetch("https://unknown.example/a.hwpx", purpose="document")
    assert r.status == "policy_review" and "rights unclear" in r.reason                 # publicly accessible != downloadable
    assert r.policy.publicly_accessible is True and r.policy.redistribution_allowed is False
    assert db.one("SELECT count(*) AS n FROM http_log")["n"] >= 5


def test_14_conditional_requests_and_offline_replay(tmp_path):
    doc = fixtures.build("simple_form")
    state = {"n": 0}
    def serve(url):
        state["n"] += 1
        return _Resp(200, doc, {"ETag": "v1", "Content-Type": "application/hwp+zip"}) if state["n"] == 1 else _Resp(304)
    c, sess, db = _client(tmp_path, {"https://ok.example/robots.txt": _Resp(404), "https://ok.example/a.hwpx": serve})
    a = c.fetch("https://ok.example/a.hwpx", purpose="document")
    b = c.fetch("https://ok.example/a.hwpx", purpose="document")
    assert a.status == b.status == "ok" and b.from_cache and a.sha256 == b.sha256
    c.offline = True
    off = c.fetch("https://ok.example/a.hwpx", purpose="document")
    assert off.status == "ok" and off.from_cache and off.content == doc
    assert c.fetch("https://ok.example/never-seen.hwpx", purpose="document").status == "offline_miss"


def test_14_takedown_stops_recollection_and_removes_derived_files(sandbox):
    from formbench.collection import discovery
    from formbench.collection.metadata import new_record
    from formbench.util import path, sha256_bytes
    db = DB()
    data = fixtures.build("simple_form")
    sha = sha256_bytes(data)
    blob = discovery.blob_path(sha)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(data)
    rec = new_record(document_id="doc-x", source_url="https://ok.example/forms/a.hwpx", source_domain="ok.example", sha256=sha, original_format="hwpx")
    db.put_doc(rec)
    (path("data", "normalized")).mkdir(parents=True)
    (path("data", "normalized") / "doc-x.json").write_text("{}")
    db.x("INSERT INTO candidates(url,kind,source_domain) VALUES(?,?,?)", ("https://ok.example/forms/a.hwpx", "document", "ok.example"))
    out = discovery.apply_takedown(db, "https://ok.example/forms/*", "owner request", log=lambda s: None)
    assert out["documents"] == 1 and out["blobs"] == 1 and out["derived"] == 1
    assert not blob.exists() and db.get_doc("doc-x") is None
    gate = PolicyGate(db, config=CFG, fetch_robots=lambda u: (404, ""))
    d = gate.check("https://ok.example/forms/a.hwpx", "document")
    assert d.decision == "deny" and "takedown" in d.reason


def test_10_colliding_fields_and_conflicting_predicates_fail_contract_validation():
    c = contracts_for(fixtures.build("simple_form"))["short"]
    assert validate_contract(c) == []
    dup = dict(c, fields=c["fields"] + [dict(c["fields"][0])])
    assert any("duplicate field_id" in e for e in validate_contract(dup))
    clash = dict(c, fields=c["fields"] + [dict(c["fields"][0], field_id="other")])
    assert any("regions collide" in e for e in validate_contract(clash))
    p0 = c["target_predicates"][0]
    conflict = dict(c, target_predicates=c["target_predicates"] + [dict(p0, value=p0["value"] + "X")])
    assert any("conflicting predicates" in e for e in validate_contract(conflict))
    assert any("unknown field" in e for e in validate_contract(dict(c, target_predicates=[dict(p0, field_id="nope")])))
    assert any("not verified" in e for e in validate_contract(dict(c, annotations_verified=False)))
    assert any("not eligible" in e for e in validate_contract(dict(c, pair_type="observed_candidate")))
    assert validate_contract({"schema_version": "1.0"})


def test_privacy_review_holds_filled_documents_and_pii():
    assert scan_pii("주민등록번호 900101-1234567 연락처 010-2345-6789") == {"kr_resident_registration_number": 1, "phone_number": 1}
    assert scan_pii("예시: 010-0000-0101, persona01@example.com") == {}                 # synthetic markers are not personal data
    assert review("성명: 김아무개 010-2345-6789", False, True)[0] == "flagged_requires_manual_review"
    assert review("성명:      (인)", False, True)[0] == "auto_cleared_blank_template"
    assert review("", True, False)[0] == "flagged_requires_manual_review"


def test_14_robots_disallow_is_overridden_for_research_but_not_access_controls(tmp_path):
    # project decision 2026-10-15: robots.txt Disallow is a request to crawlers; research fetches go ahead slowly and are
    # recorded. Hosts whose terms forbid research use stay denied, and 403/429 still stop the domain.
    doc = fixtures.build("simple_form")
    cfg = dict(CFG, robots_disallow="override_research", robots_override_delay=10.0, research_use_forbidden=["noresearch.example"],
               domains={**CFG["domains"], "walled.example": {"allow_document_fetch": True}, "noresearch.example": {"allow_document_fetch": True}})
    db = DB(tmp_path / "t.sqlite")
    sess = _Session({
        "https://ok.example/robots.txt": _Resp(200, b"User-agent: *\nDisallow: /\n"),
        "https://ok.example/forms/a.hwpx": _Resp(200, doc, {"Content-Type": "application/hwp+zip"}),
        "https://noresearch.example/robots.txt": _Resp(200, b"User-agent: *\nDisallow: /\n"),
        "https://walled.example/robots.txt": _Resp(200, b"User-agent: *\nDisallow: /\n"),
        "https://walled.example/x.hwpx": _Resp(403),
    })
    c = HttpClient(db, contact="test", session=sess, store_dir=tmp_path / "raw", sleep=lambda s: None)
    c.policy = PolicyGate(db, config=cfg, fetch_robots=c._fetch_robots)
    c.cache_dir = tmp_path / "cache"
    r = c.fetch("https://ok.example/forms/a.hwpx", purpose="document")
    assert r.status == "ok" and r.policy.robots_result == "disallowed_overridden_research" and r.policy.crawl_delay >= 10.0
    assert r.policy.redistribution_allowed is False
    r = c.fetch("https://noresearch.example/a.hwpx", purpose="document")
    assert r.status == "policy_denied" and "robots" in r.reason
    r = c.fetch("https://walled.example/x.hwpx", purpose="document")
    assert r.status == "blocked" and r.http_status == 403
