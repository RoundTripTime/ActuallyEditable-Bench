"""Knowledge base of editor behaviour, grounded in official sources.

* ``sources.yaml``  catalog of official pages (user-curated, 2026-10-16).
* ``facts.yaml``    one entry per behaviour fact: editor, context, action, effect, and a short verbatim quote of the
                    source page that states it.
* snapshots        ``formbench kb fetch`` downloads every catalog page through the policy-gated HttpClient (robots,
                    terms, rate limits, research User-Agent), stores the bytes by sha256 under data/knowledge/ (never
                    redistributed) and the visible text next to it.
* verification     ``formbench kb verify`` checks every fact deterministically: its source exists, the snapshot was
                    fetched, and the quote occurs in the snapshot text (NFKC, whitespace-insensitive). A fact that fails
                    is reported and may not be cited by the operator contract.

Fact status values: ``verified`` (quote found), ``quote_missing`` (snapshot does not contain the quote),
``not_fetched`` (no snapshot), ``cited_not_snapshotted`` (paywalled source cited by reference only)."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from ..util import jhash, load_yaml, path, read_json, sha256_bytes, utcnow, write_json

KB_VERSION = "1.1"                      # 1.1: empirical probe facts (evidence: probe)
OK_STATUSES = ("verified", "cited_not_snapshotted", "probe_verified")
EDITORS = ("hangul", "word", "writer")
FACT_KEYS = ("id", "editor", "source", "concept", "context", "action", "ui_action", "effect", "quote")
PROBE_FACT_KEYS = ("id", "editor", "probe", "concept", "ui_action", "effect", "observed_match")
SOURCE_KEYS = ("id", "layer", "product", "version", "kind", "priority", "url", "topics")


def _dir() -> Path:
    return path("data", "knowledge")


def load_sources() -> dict:
    return load_yaml(path("configs", "knowledge", "sources.yaml"))


def load_facts() -> dict:
    p = path("configs", "knowledge", "facts.yaml")
    return load_yaml(p) if Path(p).exists() else {"facts": []}


def norm(text: str) -> str:
    """Comparison form: NFKC, typographic quotes/dashes folded, whitespace removed (layout-independent match)."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                                   " ": " "}))
    return re.sub(r"\s+", "", t)


def visible_text(html: bytes) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


def manifest_path() -> Path:
    return _dir() / "snapshots.json"


def load_manifest() -> dict:
    p = manifest_path()
    return read_json(p) if p.exists() else {"kb_version": KB_VERSION, "snapshots": {}}


def fetch(client, only: list[str] | None = None, render_fallback: bool = True, log=print) -> dict:
    """Snapshot every catalog page (or ``only``). Pages whose static HTML carries almost no text (script-drawn help)
    are read through headless Chrome as a fallback; the rendered DOM is snapshotted and marked as such."""
    from ..collection import render_page
    cat = load_sources()
    man = load_manifest()
    out = {"fetched": 0, "failed": 0, "rendered": 0, "skipped": 0}
    for s in cat["source_catalog"]:
        if only and s["id"] not in only:
            continue
        if s.get("kind") == "peer_reviewed_article_doi":
            out["skipped"] += 1                          # paywalled: cited by reference, never fetched
            continue
        fr = client.fetch(s["url"], purpose="page")
        rec = {"url": s["url"], "fetched_at": utcnow(), "status": fr.status, "http_status": fr.http_status,
               "reason": (fr.reason or "")[:300], "final_url": fr.final_url}
        if fr.status == "ok" and fr.content:
            body, how = fr.content, "static"
            text = visible_text(body)
            if render_fallback and len(norm(text)) < 400:
                dom = render_page.render(s["url"], client.ua)
                if dom and len(norm(visible_text(dom))) > len(norm(text)):
                    body, how, text = dom, "rendered_dom", visible_text(dom)
                    out["rendered"] += 1
            sha = sha256_bytes(body)
            d = _dir() / s["id"]
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{sha}.html").write_bytes(body)
            (d / f"{sha}.txt").write_text(text, encoding="utf-8")
            rec.update({"sha256": sha, "text_sha256": sha256_bytes(text.encode()), "bytes": len(body), "text_chars": len(text),
                        "capture": how})
            out["fetched"] += 1
        else:
            out["failed"] += 1
        man["snapshots"][s["id"]] = rec
        log(f"kb fetch {s['id']}: {rec['status']} {rec.get('capture', '')} {rec.get('text_chars', '')}")
    man["kb_version"] = KB_VERSION
    write_json(manifest_path(), man)
    return out


def snapshot_text(source_id: str, man: dict | None = None) -> str | None:
    man = man or load_manifest()
    rec = man["snapshots"].get(source_id) or {}
    if not rec.get("sha256"):
        return None
    p = _dir() / source_id / f"{rec['sha256']}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else None


def schema_errors() -> list[str]:
    errs = []
    cat = load_sources()
    ids = [s.get("id") for s in cat.get("source_catalog", [])]
    if len(ids) != len(set(ids)):
        errs.append("duplicate source ids")
    layers = {l["id"] for l in cat.get("retrieval_layers", [])}
    for s in cat.get("source_catalog", []):
        for k in SOURCE_KEYS:
            if k not in s:
                errs.append(f"source {s.get('id')}: missing {k}")
        if s.get("layer") not in layers:
            errs.append(f"source {s.get('id')}: unknown layer {s.get('layer')}")
    fids = []
    probe_ids = set()
    try:
        from .probes import load_probes
        probe_ids = {p["id"] for p in load_probes()["probes"]}
    except FileNotFoundError:
        pass
    for f in load_facts().get("facts", []):
        fids.append(f.get("id"))
        if f.get("evidence") == "probe":
            for k in PROBE_FACT_KEYS:
                if f.get(k) in (None, "", {}):
                    errs.append(f"fact {f.get('id')}: missing {k}")
            if f.get("probe") not in probe_ids:
                errs.append(f"fact {f.get('id')}: unknown probe {f.get('probe')}")
            if f.get("editor") not in EDITORS:
                errs.append(f"fact {f.get('id')}: editor must be one of {EDITORS}")
            continue
        for k in FACT_KEYS:
            if not f.get(k):
                errs.append(f"fact {f.get('id')}: missing {k}")
        if f.get("source") not in ids:
            errs.append(f"fact {f.get('id')}: unknown source {f.get('source')}")
        if f.get("editor") not in EDITORS:
            errs.append(f"fact {f.get('id')}: editor must be one of {EDITORS}")
        if len(f.get("quote", "")) > 400:
            errs.append(f"fact {f.get('id')}: quote longer than 400 characters (quote briefly, cite the page)")
    if len(fids) != len(set(fids)):
        errs.append("duplicate fact ids")
    return errs


def verify() -> dict:
    """Deterministic check of every fact against its snapshot. Same inputs -> same result and hash."""
    cat = {s["id"]: s for s in load_sources()["source_catalog"]}
    man = load_manifest()
    facts = load_facts().get("facts", [])
    texts: dict[str, str | None] = {}
    res = []
    from . import probes as P
    latest = {e: P.latest(e) for e in EDITORS}
    for f in facts:
        if f.get("evidence") == "probe":
            run = latest.get(f["editor"])
            r = ((run or {}).get("results") or {}).get(f["probe"])
            if not r or "observed" not in r:
                st = "probe_missing"
            else:
                st = "probe_verified" if P.judge(f["observed_match"], r["observed"]) else "probe_mismatch"
            res.append({"id": f["id"], "editor": f["editor"], "source": f"probe:{f['probe']}", "status": st,
                        "snapshot_sha256": (run or {}).get("driver_sha256"), "product": (run or {}).get("product"),
                        "ran_at": (run or {}).get("ran_at")})
            continue
        src = cat.get(f.get("source"), {})
        if src.get("kind") == "peer_reviewed_article_doi":
            st = "cited_not_snapshotted"
        else:
            if f["source"] not in texts:
                t = snapshot_text(f["source"], man)
                texts[f["source"]] = norm(t) if t is not None else None
            t = texts[f["source"]]
            st = "not_fetched" if t is None else ("verified" if norm(f["quote"]) in t else "quote_missing")
        res.append({"id": f["id"], "editor": f["editor"], "source": f["source"], "status": st,
                    "snapshot_sha256": (man["snapshots"].get(f["source"]) or {}).get("sha256")})
    by = {}
    for r in res:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"kb_version": KB_VERSION, "facts": len(res), "by_status": by, "schema_errors": schema_errors(),
            "results": res, "hash": jhash([(r["id"], r["status"], r["snapshot_sha256"]) for r in res])}


def verified_fact_ids() -> set[str]:
    return {r["id"] for r in verify()["results"] if r["status"] in OK_STATUSES}


def concept_coverage(ver: dict | None = None) -> list[dict]:
    """Per concept: editors with a verified fact, the normalized actions they share, and stated effect differences.

    status
      common            all three editors have a verified fact, they share at least one ui_action, no effect differs
      conditional       as common, but a fact states a differing effect: the contract must give the condition under
                        which the editors agree (e.g. Tab into an *empty* cell), or the action is not admitted
      no_shared_action  all editors covered, but by different actions (the same effect needs different gestures)
      gap               some editor has no verified fact: not admissible to the common subset"""
    ver = ver or verify()
    ok = {r["id"] for r in ver["results"] if r["status"] in OK_STATUSES}
    by: dict[str, dict] = {}
    for f in load_facts().get("facts", []):
        c = by.setdefault(f.get("concept", "?"), {"concept": f.get("concept", "?"), "actions": {}, "facts": [], "differences": []})
        if f["id"] in ok:
            c["actions"].setdefault(f["editor"], set()).update(a.strip() for a in str(f.get("ui_action", "")).split("|") if a.strip())
            c["facts"].append(f["id"])
            if f.get("effect_detail"):
                c["differences"].append(f"{f['editor']}: {f['effect_detail']}")
    out = []
    for c in sorted(by.values(), key=lambda x: x["concept"]):
        missing = [e for e in EDITORS if e not in c["actions"]]
        shared = sorted(set.intersection(*c["actions"].values())) if not missing else []
        if missing:
            status = "gap"
        elif not shared:
            status = "no_shared_action"
        else:
            status = "conditional" if c["differences"] else "common"
        out.append({"concept": c["concept"], "status": status, "editors": sorted(c["actions"]), "missing": missing,
                    "shared_actions": shared, "actions": {e: sorted(v) for e, v in sorted(c["actions"].items())},
                    "facts": c["facts"], "differences": c["differences"]})
    return out
