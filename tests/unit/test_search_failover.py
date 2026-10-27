"""Search backends fail over: a blocked / rate-limited backend is paused (shared state in the DB) and the next answers."""
from formbench.collection import search
from formbench.collection.search import FailoverSearchProvider, ProviderResult, SearchHit
from formbench.util import DB


class FakeProv:
    def __init__(self, label, outcomes):
        self.label, self.outcomes, self.calls = label, list(outcomes), 0

    def search(self, query, limit=10):
        self.calls += 1
        st = self.outcomes.pop(0) if self.outcomes else "ok"
        hits = [SearchHit(f"https://{self.label}.example/{query}", kind="page")] if st == "ok" else []
        return ProviderResult("general_web", self.label, query, st, hits, "" if st == "ok" else f"{self.label} said {st}")


def _provider(tmp_path, provs):
    db = DB(tmp_path / "t.sqlite")
    client = type("C", (), {"db": db})()
    fp = FailoverSearchProvider(client, {"backends": []})
    fp.backends = [(p.label, p) for p in provs]
    return fp, db


def test_blocked_backend_is_paused_and_the_next_answers(tmp_path):
    a, b = FakeProv("searxng-local", ["blocked"]), FakeProv("duckduckgo-html", ["ok", "ok"])
    fp, db = _provider(tmp_path, [a, b])
    r = fp.search("서식 작성예시")
    assert r.status == "ok" and r.hits[0].url.startswith("https://duckduckgo-html") and "backend=duckduckgo-html" in r.reason
    st = db.one("SELECT * FROM search_backends WHERE label='searxng-local'")
    assert st["fails"] == 1 and st["paused_until"] > 0 and st["last_status"] == "blocked"
    r2 = fp.search("記入例")                                   # paused backend is skipped, not asked again
    assert r2.status == "ok" and a.calls == 1 and b.calls == 2


def test_all_backends_down_is_reported_as_blocked(tmp_path):
    fp, _ = _provider(tmp_path, [FakeProv("x", ["error"]), FakeProv("y", ["blocked"])])
    r = fp.search("q")
    assert r.status == "blocked" and "x: error" in r.reason and "y: blocked" in r.reason


def test_searxng_html_answer_counts_as_blocked(tmp_path, monkeypatch):
    class R:
        status_code, content = 200, b"<html>bot check</html>"
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    p = search.SearxngProvider(None, {"base_url": "http://127.0.0.1:8888", "local": True})
    assert p.search("q").status == "blocked"
