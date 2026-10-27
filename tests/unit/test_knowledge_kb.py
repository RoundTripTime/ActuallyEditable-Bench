"""Knowledge base checker: quotes must occur verbatim in the hashed snapshot of their source (2026-10-16)."""
from formbench.knowledge import kb


def _setup(monkeypatch, quote, text):
    monkeypatch.setattr(kb, "load_sources", lambda: {"retrieval_layers": [{"id": "editor_operations"}], "source_catalog": [
        {"id": "s1", "layer": "editor_operations", "product": "P", "version": "1", "kind": "official_help_html",
         "priority": "critical", "url": "https://example.org/help", "topics": ["t"]}]})
    monkeypatch.setattr(kb, "load_facts", lambda: {"facts": [
        {"id": "f1", "editor": "word", "source": "s1", "concept": "c", "context": "x", "action": {"keys": "Tab"}, "ui_action": "key:Tab",
         "effect": "e", "quote": quote}]})
    monkeypatch.setattr(kb, "load_manifest", lambda: {"snapshots": {"s1": {"sha256": "abc"}}})
    monkeypatch.setattr(kb, "snapshot_text", lambda sid, man=None: text)


def test_quote_found_despite_whitespace_and_typography(monkeypatch):
    _setup(monkeypatch, "Move to the next cell. Tab key", "…  Move to the\nnext cell.   Tab key …")
    out = kb.verify()
    assert out["by_status"] == {"verified": 1} and not out["schema_errors"]


def test_altered_quote_is_reported(monkeypatch):
    _setup(monkeypatch, "Move to the next row. Tab key", "Move to the next cell. Tab key")
    assert kb.verify()["by_status"] == {"quote_missing": 1}


def test_missing_snapshot_is_not_verified(monkeypatch):
    _setup(monkeypatch, "anything", None)
    assert kb.verify()["by_status"] == {"not_fetched": 1}


def test_verify_is_deterministic(monkeypatch):
    _setup(monkeypatch, "Tab key", "Tab key")
    assert kb.verify()["hash"] == kb.verify()["hash"]


def test_real_catalog_schema_is_valid():
    assert kb.schema_errors() == []
