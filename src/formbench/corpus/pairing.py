"""PairMatcher + SyntheticCompletion orchestration.

* Observed pairs: evidence-based candidates only (``observed_candidate``); nothing is promoted to
  ``observed_verified`` without a manual decision in the review queue.
* Synthetic pairs: frozen contracts whose completion was produced by replaying an optimal plan on the blank
  template with the editing backend, re-opened and verified (``synthetic_from_native`` / ``synthetic_from_converted``).
"""
from __future__ import annotations

from .. import resources
from ..completion.execution import replay_and_verify
from ..completion.synthetic import GENERATOR_VERSION
from ..contracts import annotations
from ..contracts.extract import build_contracts
from ..contracts.schema import validate_contract
from ..contracts.verify import DocView
from ..fixtures import signature_png
from ..formats.hwpx import HwpxPackage
from ..planning.costs import CostModel
from ..planning.search import Planner
from ..util import DB, cjson, path, read_json, sha256_bytes, utcnow, write_json
from .dedupe import jaccard, shingles
from .importers import hwpx_bytes_for

MAX_PAGES = 8


def assets() -> dict:
    return {"signature": signature_png()}


def eligible_templates(db: DB) -> list[dict]:
    out = []
    for d in db.docs():
        if d.get("parser_status") != "ok" or not d.get("is_template") or not str(d.get("privacy_review", "")).startswith("auto_cleared"):
            continue
        if d.get("original_format") not in ("hwpx", "hwp", "docx", "odt") or (d.get("page_count") or 99) > MAX_PAGES:
            continue
        if d.get("original_format") != "hwpx" and d.get("converter_status") != "ok":
            continue
        out.append(d)
    return out


def _synth_one(args) -> dict:
    doc_id, original_format, seed = args
    db = DB()
    rec = db.get_doc(doc_id)
    db.close()
    res = {"document_id": doc_id, "contracts": [], "rejected": [], "review_fields": [], "error": None}
    try:
        blank = hwpx_bytes_for(rec)
        view = DocView(HwpxPackage.load(blank))
        ptype = "synthetic_from_native" if original_format == "hwpx" else "synthetic_from_converted"
        contracts, summary = build_contracts(doc_id, view, ptype, seed)
        res["summary"] = {k: v for k, v in summary.items() if k != "review_queue"}
        res["review_fields"] = summary["review_queue"]
        out_dir = path("data", "pairs", doc_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "blank.hwpx").write_bytes(blank)
        for c in contracts:
            errs = validate_contract(c)
            if errs:
                res["rejected"].append({"task_id": c["task_id"], "reason": "schema", "detail": errs[:3]})
                continue
            plan = Planner(c, CostModel("uniform"), assets=assets(), time_limit_s=60).solve(blank)
            if not plan.completion_feasible:
                res["rejected"].append({"task_id": c["task_id"], "reason": "unsolvable_on_template", "detail": plan.reason[:200], "solver": plan.status})
                continue
            ex = replay_and_verify(blank, c, plan.plan, assets())
            if not ex.get("completion_success"):
                res["rejected"].append({"task_id": c["task_id"], "reason": "template_replay_failed", "detail": str(ex.get("error") or "goal not reached")[:200]})
                continue
            (out_dir / f"{c['scenario']}.completed.hwpx").write_bytes(ex["output_bytes"])
            c["oracle"] = {"completed_sha256": ex["output_sha256"], "template_sha256": sha256_bytes(blank), "template_cost_uniform": plan.cost,
                           "template_exact": plan.exact_minimum, "editor_backend": ex["backend"], "external_parse": ex.get("external_parse"),
                           "generator_version": GENERATOR_VERSION}
            p = annotations.freeze(c)
            res["contracts"].append({"task_id": c["task_id"], "scenario": c["scenario"], "path": str(p), "n_predicates": len(c["target_predicates"]),
                                     "pair_type": ptype, "completed_sha256": ex["output_sha256"], "cost": plan.cost, "exact": plan.exact_minimum})
    except Exception as e:      # noqa: BLE001 - recorded, never hidden
        res["error"] = f"{type(e).__name__}: {e}"[:300]
    return res


def _cfg_flag(name: str) -> bool:
    from ..util import load_yaml
    try:
        return bool((load_yaml(path("configs", "pairs.yaml")) or {}).get("observed", {}).get(name, False))
    except FileNotFoundError:
        return False


def observed_candidates(db: DB) -> list[dict]:
    """Evidence-based (template, completed) candidates inside a template family."""
    excluded = excluded_formats()
    docs = [d for d in db.docs() if d.get("parser_status") == "ok" and d.get("original_format") not in excluded]
    side = {}
    for d in docs:
        p = path("data", "normalized", f"{d['document_id']}.json")
        side[d["document_id"]] = shingles(read_json(p).get("text_shingle_source", "")) if p.exists() else frozenset()
    out = []
    by_fam: dict[str, list[dict]] = {}
    for d in docs:
        by_fam.setdefault(d.get("template_family_id") or d["document_id"], []).append(d)
    for members in by_fam.values():
        blanks = [d for d in members if d.get("is_template")]
        filled = [d for d in members if d.get("is_completed")]
        for t in blanks:
            for c in filled:
                st, sc = side[t["document_id"]], side[c["document_id"]]
                contain = len(st & sc) / len(st) if st else 0.0
                out.append({"template_id": t["document_id"], "completed_id": c["document_id"], "pair_type": "observed_candidate",
                            "evidence": {"same_template_family": True, "template_text_containment": round(contain, 3),
                                         "text_jaccard": round(jaccard(st, sc), 3), "same_source_domain": t.get("source_domain") == c.get("source_domain")},
                            "confidence": round(contain, 3)})
    return out


def synthetic_policy() -> dict:
    """configs/pairs.yaml: whether synthetic pairs are active or archived (archived = kept, not generated, not evaluated)."""
    from ..util import load_yaml
    p = path("configs", "pairs.yaml")
    cfg = (load_yaml(p) or {}).get("synthetic", {}) if p.exists() else {}
    return {"status": cfg.get("status", "active"), "use_for_evaluation": bool(cfg.get("use_for_evaluation", True)),
            "generate_new": bool(cfg.get("generate_new", True))}


def pair_formats() -> set:
    """Formats an observed pair may have (both sides the same format; configs/collection.yaml ``pair_formats``)."""
    from ..util import load_yaml
    p = path("configs", "collection.yaml")
    return set(((load_yaml(p) or {}) if p.exists() else {}).get("pair_formats") or EDITABLE)


def prune_observed(db: DB, dry_run: bool = False, log=print) -> dict:
    """Remove observed pairs whose two sides are not the same allowed format, and every document that was collected only
    for such pairs (record, raw bytes, normalized side-car, converted HWPX, review thumbnails). Discovery logs stay;
    the candidate rows are marked so the files are not fetched again."""
    from ..collection.discovery import blob_path
    from .importers import derived_hwpx_path
    allowed = pair_formats()
    drop, keep_docs = [], set()
    for r in db.q("SELECT pair_id, template_id, completed_id FROM pairs WHERE pair_type IN ('observed_candidate','observed_verified','observed_rejected')"):
        fb = (db.get_doc(r["template_id"]) or {}).get("original_format")
        fe = (db.get_doc(r["completed_id"]) or {}).get("original_format")
        if fb == fe and fb in allowed:
            keep_docs.update((r["template_id"], r["completed_id"]))
        else:
            drop.append(r["pair_id"])
    docs = [d for d in db.docs() if (d.get("provider") or "").startswith("observed_pair") and d["document_id"] not in keep_docs]
    out = {"pairs_removed": len(drop), "documents_removed": len(docs), "by_format": {}}
    for d in docs:
        out["by_format"][d.get("original_format")] = out["by_format"].get(d.get("original_format"), 0) + 1
    if dry_run:
        return dict(out, dry_run=True)
    for pid in drop:
        db.x("DELETE FROM pairs WHERE pair_id=?", (pid,))
        db.x("DELETE FROM review_queue WHERE item_id=?", (pid,))
    for d in docs:
        i = d["document_id"]
        db.x("DELETE FROM documents WHERE document_id=?", (i,))
        if db.one("SELECT 1 FROM documents WHERE sha256=?", (d["sha256"],)) is None:
            blob_path(d["sha256"]).unlink(missing_ok=True)
        for p in (path("data", "normalized", f"{i}.json"), derived_hwpx_path(i)):
            p.unlink(missing_ok=True)
        for t in path("reports", "review", "thumbs").glob(f"{i}_p*.png"):
            t.unlink()
        db.x("UPDATE candidates SET fetch_status='removed_pair_format_policy', document_id=NULL, policy_reason=? WHERE document_id=?",
             (f"removed: observed pairs are same-format {sorted(allowed)} only", i))
        db.x("DELETE FROM queue WHERE key=?", (i,))
    log(f"prune observed pairs: {out}")
    return out


def excluded_formats() -> set:
    """Formats the collection no longer takes (configs/collection.yaml ``exclude_formats``)."""
    from ..util import load_yaml
    p = path("configs", "collection.yaml")
    return set(((load_yaml(p) or {}) if p.exists() else {}).get("exclude_formats", []) or [])


EDITABLE = {"hwpx", "docx", "odt"}
CONTRADICTED_BELOW = 0.10            # share of blank text found in the example: below this the two share no form text
REAL_PAIR_TYPES = ("observed_verified", "observed_candidate", "observed_rejected", "observed_auto_rejected", "observed_excluded",
                   "observed_duplicate")
# Certain-fail rules learned from the human review (2026-10-15: 220 verified / 102 rejected). A rule is admitted only if
# no verified pair satisfies it; matching pairs become observed_auto_rejected (never over a human decision; a reviewer
# can still verify one). Thresholds are frozen here and versioned.
AUTO_REJECT_VERSION = "1.0"
AUTO_REJECT_RULES = {
    "texts_differ": {"criterion": "share of the blank's text found in the example < 0.35", "basis": "verified 0/220, rejected 10/102"},
    "not_filled": {"criterion": "fill check no_fill / guidance_only (corpus/fillcheck.py)", "basis": "verified 0/220, rejected 12/102"},
}
AUTO_REJECT_CONTAINMENT = 0.35


def _containment(db_sides: dict, a: str, b: str) -> float | None:
    """Share of the blank's text shingles that re-appear in the completed example (None if either has no text)."""
    sa, sb = db_sides.get(a), db_sides.get(b)
    if not sa or not sb:
        return None
    return round(len(sa & sb) / len(sa), 3)


def _fill(blank_id: str, example_id: str) -> dict | None:
    """Fill check of a (blank, example) pair from the normalized texts (corpus/fillcheck.py); None without texts."""
    from .fillcheck import check
    pb, pe = path("data", "normalized", f"{blank_id}.json"), path("data", "normalized", f"{example_id}.json")
    if not (pb.exists() and pe.exists()):
        return None
    tb, te = read_json(pb).get("text_shingle_source", ""), read_json(pe).get("text_shingle_source", "")
    if not tb or not te:
        return None
    return check(tb, te)


CONTENT_PAIR_MIN = 0.8               # unlabelled pair: share of the blank's text that the completed file must contain
CONTENT_PAIR_SAME_ABOVE = 0.97       # ... and the completed file must add text (not the same file twice)


def _content_pair(docs: dict, sides: dict, group: str, members: list) -> dict | None:
    """Strategy S4.6: files of one publication unit (same post / folder) without a publisher label. A (blank, completed)
    candidate only when one file contains nearly all the other's text and adds text of its own; evidence grade 'weak'
    until a person verifies it (the label that would have said 'example' is missing)."""
    allowed = pair_formats()
    best = None
    for ai, al in members:
        for bi, bl in members:
            if ai == bi or docs[ai].get("original_format") != docs[bi].get("original_format") or docs[ai].get("original_format") not in allowed:
                continue
            sa, sb = sides.get(ai), sides.get(bi)
            if not sa or not sb or len(sa) < 20:
                continue
            fwd, back = len(sa & sb) / len(sa), len(sa & sb) / len(sb)
            if fwd >= CONTENT_PAIR_MIN and back < CONTENT_PAIR_SAME_ABOVE and len(sb) > len(sa):
                key = (round(fwd, 3), -round(back, 3), ai, bi)
                if best is None or key > best[0]:
                    best = (key, ai, al, bi, bl)
    if best is None:
        return None
    (fwd, nback, _, _), bi, bl, ei, el = best
    fill = _fill(bi, ei)
    if fill and fill["verdict"] != "filled":
        return None
    b, e = docs[bi], docs[ei]
    return {"template_id": bi, "completed_id": ei, "pair_type": "observed_candidate", "confidence": round(0.2 + 0.3 * fwd, 3),
            "evidence": {"source": "publisher_unit_content", "page": el.get("page"), "group": group, "publisher_label_example": None,
                         "publisher_label_blank": None, "name_relation": "content_same_unit", "template_text_containment": fwd,
                         "completed_text_containment": -nback, "same_template_family": bool(b.get("template_family_id")) and
                         b.get("template_family_id") == e.get("template_family_id"), "formats": [b.get("original_format"), e.get("original_format")],
                         "same_source_domain": b.get("source_domain") == e.get("source_domain"),
                         "rights": [b.get("rights_status"), e.get("rights_status")], "redistribution_allowed": False,
                         "grade": "weak", "alternate_format_pairs": []}}


def page_pair_candidates(db: DB) -> list[dict]:
    """(blank, example) pairs evidenced by a publisher page that lists both, labelled by the publisher (작성예시, 記入例,
    completed example, ...). Confidence combines the label/name relation with text containment; it never verifies."""
    excluded = excluded_formats()
    docs = {d["document_id"]: d for d in db.docs() if d.get("observed_pair_links") and d.get("original_format") not in excluded}
    sides = {}
    for i in docs:
        p = path("data", "normalized", f"{i}.json")
        sides[i] = shingles(read_json(p).get("text_shingle_source", "")) if p.exists() else frozenset()
    groups: dict[str, dict[str, list]] = {}
    for i, d in docs.items():
        for lk in d["observed_pair_links"]:
            groups.setdefault(lk["group"], {"blank": [], "example": [], "member": []})[lk["role"]].append((i, lk))
    out, contradicted, unfilled = [], [], []
    for g, roles in sorted(groups.items()):
        if roles["member"]:                            # unlabelled files of one publication unit: decided by their texts
            c = _content_pair(docs, sides, g, roles["member"])
            if c:
                out.append(c)
            continue
        # one representative pair per publisher form: same editable format first (docx/hwpx/odt), then an editable
        # blank, then anything; the other file formats of the same form are kept as alternates in the evidence
        allowed = pair_formats()
        combos = [(bi, bl, ei, el) for bi, bl in roles["blank"] for ei, el in roles["example"] if bi != ei
                  and docs[bi].get("original_format") == docs[ei].get("original_format") and docs[bi].get("original_format") in allowed]
        if not combos:
            continue
        def rank(c):                                   # within a format class the blank the example contains most
            fb, fe = docs[c[0]].get("original_format"), docs[c[2]].get("original_format")
            return (0 if fb == fe and fb in EDITABLE else 1 if fb in EDITABLE else 2 if fe in EDITABLE else 3,
                    -(_containment(sides, c[0], c[2]) or 0.0), c[0], c[2])
        combos.sort(key=rank)
        alternates = [{"template_id": c[0], "completed_id": c[2], "formats": [docs[c[0]].get("original_format"), docs[c[2]].get("original_format")]}
                      for c in combos[1:]]
        bi, bl, ei, el = combos[0]
        choice = None
        if el.get("name_relation") == "content_choice":   # record the runner-up so a reviewer sees how clear the choice was
            ranked = [(_containment(sides, c[0], c[2]), c[0]) for c in combos if c[2] == ei]
            choice = {"chosen": bi, "candidates": [{"template_id": t, "containment": v} for v, t in ranked[:8]]}
        b, e = docs[bi], docs[ei]
        contain = _containment(sides, bi, ei)
        fill = _fill(bi, ei)
        reason = None
        if contain is not None and contain < AUTO_REJECT_CONTAINMENT:
            contradicted.append({"group": g, "template_id": bi, "completed_id": ei, "containment": contain})
            reason = "texts_differ"                    # the texts say these are different documents
        elif fill and fill["verdict"] != "filled":     # the 'example' is the blank with a stamp or with guidance text only
            unfilled.append({"group": g, "template_id": bi, "completed_id": ei, **fill})
            reason = "not_filled"
        rel = el.get("name_relation", "")
        same_fam = bool(b.get("template_family_id")) and b.get("template_family_id") == e.get("template_family_id")
        score = {"same_name": 0.5, "name_contains": 0.4}.get(rel, 0.3) + (0.4 * contain if contain is not None else 0.0) + (0.1 if same_fam else 0.0)
        grade = "strong" if rel == "same_name" and (contain or 0) >= 0.6 else "medium" if (contain or 0) >= 0.4 or rel == "same_name" else "weak"
        out.append({"template_id": bi, "completed_id": ei, "pair_type": "observed_auto_rejected" if reason else "observed_candidate",
                    "confidence": round(min(score, 1.0), 3),
                    "evidence": {"source": "publisher_page", "fill_check": fill,
                                 **({"auto_review": {"rule": reason, "version": AUTO_REJECT_VERSION, **AUTO_REJECT_RULES[reason]}} if reason else {}), "page": el["page"], "group": g, **({"blank_page": bl["page"]} if bl.get("page") != el["page"] else {}), "publisher_label_example": el.get("link_text"),
                                 "publisher_label_blank": bl.get("link_text"), "name_relation": rel,
                                 "template_text_containment": contain, "same_template_family": same_fam,
                                 "formats": [b.get("original_format"), e.get("original_format")],
                                 "same_source_domain": b.get("source_domain") == e.get("source_domain"),
                                 "rights": [b.get("rights_status"), e.get("rights_status")],
                                 "redistribution_allowed": bool(b.get("redistribution_allowed") and e.get("redistribution_allowed")),
                                 "grade": grade, "alternate_format_pairs": alternates, **({"content_choice": choice} if choice else {})}})
    page_pair_candidates.last_contradicted = contradicted
    page_pair_candidates.last_unfilled = unfilled
    return _apply_country_exclusion(out)


EXCLUSION_NOTE = "user 2026-10-15: documents of this country are no longer reviewed or collected ('일본 문서는 더 이상 검토하지 않을거야. 모두 제외해줘')"


def _apply_country_exclusion(cands: list[dict]) -> list[dict]:
    """Machine candidates / auto-rejections whose evidence page is in an excluded country become observed_excluded
    (kept, reversible, out of the review queue). Human decisions are never touched (record_observed skips them)."""
    from ..collection.pair_explorer import coverage_cell
    from ..util import load_yaml
    from urllib.parse import urlsplit
    try:
        cfg = load_yaml(path("configs", "collection.yaml"))
    except FileNotFoundError:
        return cands
    excl = {c.upper() for c in (cfg.get("exclude_countries") or [])}
    langs = set(cfg.get("include_languages") or [])
    if not excl and not langs:
        return cands
    db = DB() if langs else None
    for c in cands:
        if c["pair_type"] not in ("observed_candidate", "observed_auto_rejected"):
            continue
        ev = c.get("evidence") or {}
        host = (urlsplit(ev.get("page") or "").hostname or "").lower()
        country = coverage_cell(host)[0] if host else ""
        reason = note = None
        if country == "JP" and "JP" in excl:
            reason, note = f"country:{country}", EXCLUSION_NOTE
        elif langs:
            lang = pair_language(db, c["template_id"], c["completed_id"], country)
            if lang not in langs:
                reason, note = f"language:{lang}", LANGUAGE_NOTE
        if reason:
            ev["excluded"] = {"reason": reason, "note": note, "was": c["pair_type"]}
            c["pair_type"] = "observed_excluded"
    return cands


LANGUAGE_NOTE = "user 2026-10-15: only Korean and English documents ('앞으로 한국어와 영어 자료만 수집하자 ... 한국어나 영어가 아닌 자료는 모두 탈락')"
ENGLISH_COUNTRIES = {"US", "UK", "IE", "CA", "AU", "NZ", "ZA", "IN", "SG", "PH"}


def pair_language(db: DB, blank_id: str, example_id: str, country: str = "") -> str:
    """ko / en / other language code of a pair: the blank's detected language, else the example's, else the source
    country (KR -> ko, English-speaking -> en). 'ko+en' counts as ko."""
    for i in (blank_id, example_id):
        lang = ((db.get_doc(i) or {}).get("language") or "unknown")
        if lang != "unknown":
            return "ko" if lang.startswith("ko") else lang
    return "ko" if country == "KR" else "en" if country in ENGLISH_COUNTRIES else f"unknown@{country or '?'}"


def record_observed(db: DB, cands: list[dict]) -> dict:
    """Insert new candidates; never overwrite a reviewer's decision (verified / rejected stay as they are)."""
    n = 0
    keep = {f"{c['template_id']}~{c['completed_id']}" for c in cands}
    stale = [r["pair_id"] for r in db.q("SELECT pair_id FROM pairs WHERE pair_type IN ('observed_candidate','observed_auto_rejected','observed_excluded')")
             if r["pair_id"] not in keep]
    for pid in stale:                                  # superseded machine candidates / rejections (never a reviewer decision)
        db.x("DELETE FROM pairs WHERE pair_id=? AND pair_type IN ('observed_candidate','observed_auto_rejected','observed_excluded')", (pid,))
        db.x("DELETE FROM review_queue WHERE item_id=? AND status='open'", (pid,))
    for c in cands:
        pid = f"{c['template_id']}~{c['completed_id']}"
        row = db.one("SELECT pair_type FROM pairs WHERE pair_id=?", (pid,))
        if row is not None and row["pair_type"] in ("observed_verified", "observed_rejected", "observed_duplicate"):
            continue                                   # human decisions and duplicate marks are not overwritten here
        db.x("INSERT OR REPLACE INTO pairs(pair_id,template_id,completed_id,pair_type,evidence,confidence,eval_eligible,created_at) VALUES(?,?,?,?,?,?,?,?)",
             (pid, c["template_id"], c["completed_id"], c["pair_type"], cjson(c["evidence"]), c["confidence"], 0, utcnow()))
        if c["pair_type"] == "observed_candidate" and db.one("SELECT 1 FROM review_queue WHERE item_id=?", (pid,)) is None:
            annotations.enqueue_review(db, "observed_pair_candidate", pid, c)
        elif c["pair_type"] in ("observed_auto_rejected", "observed_excluded"):
            db.x("DELETE FROM review_queue WHERE item_id=? AND status='open'", (pid,))
        n += 1
    return {"candidates_recorded": n, "stale_candidates_removed": len(stale)}


def review_observed(db: DB, pair_id: str, decision: str, reviewer: str, note: str = "") -> dict:
    """Human decision on an observed pair: ``verified`` (same template family, the example is a completion of the blank,
    no real personal data) or ``rejected``. Only this function creates ``observed_verified``."""
    row = db.one("SELECT * FROM pairs WHERE pair_id=?", (pair_id,))
    if row is None or row["pair_type"] not in REAL_PAIR_TYPES:
        raise ValueError(f"no observed pair {pair_id}")
    if decision not in ("verified", "rejected"):
        raise ValueError("decision must be verified|rejected")
    if not reviewer.strip():
        raise ValueError("reviewer is required")
    import json
    ev = json.loads(row["evidence"] or "{}")
    ev["review"] = {"decision": decision, "reviewer": reviewer, "note": note, "at": utcnow()}
    ptype = "observed_verified" if decision == "verified" else "observed_rejected"
    db.x("UPDATE pairs SET pair_type=?, evidence=?, eval_eligible=? WHERE pair_id=?", (ptype, cjson(ev), 0, pair_id))
    if db.one("SELECT 1 FROM review_queue WHERE item_id=?", (pair_id,)) is not None:
        annotations.resolve_review(db, pair_id, "accepted" if decision == "verified" else "rejected")
    for doc_id, role in ((row["template_id"], "template"), (row["completed_id"], "completed")):
        d = db.get_doc(doc_id)
        if d is None:
            continue
        if decision == "verified":
            d["is_template" if role == "template" else "is_completed"] = True
            if role == "completed":
                d["is_template"] = False
            d["pair_type"] = "observed_verified" if d.get("pair_type") in (None, "unpaired", "observed_candidate") else d["pair_type"]
        db.put_doc(d)
    return {"pair_id": pair_id, "pair_type": ptype}


def pair_counts(db: DB) -> dict:
    """Real (observed) pairs and synthetic pairs, always reported separately."""
    import json
    real: dict = {}
    for r in db.q("SELECT pair_type, evidence, template_id, completed_id FROM pairs WHERE pair_type IN ('observed_verified','observed_candidate','observed_rejected','observed_auto_rejected','observed_excluded','observed_duplicate')"):
        ev = json.loads(r["evidence"] or "{}")
        b = db.get_doc(r["template_id"]) or {}
        e = db.get_doc(r["completed_id"]) or {}
        key = r["pair_type"]
        x = real.setdefault(key, {"pairs": 0, "by_format": {}, "by_grade": {}, "by_domain": {}, "template_families": set()})
        x["pairs"] += 1
        f = f"{b.get('original_format')}->{e.get('original_format')}"
        x["by_format"][f] = x["by_format"].get(f, 0) + 1
        g = ev.get("grade", "n/a")
        x["by_grade"][g] = x["by_grade"].get(g, 0) + 1
        dom = b.get("source_domain")
        x["by_domain"][dom] = x["by_domain"].get(dom, 0) + 1
        x["template_families"].add(b.get("template_family_id") or r["template_id"])
        x.setdefault("_domains", []).append(dom)
        x.setdefault("by_language", {})
        lang = b.get("language") or "unknown"
        x["by_language"][lang] = x["by_language"].get(lang, 0) + 1
        x.setdefault("by_form_domain", {})
        fd = (b.get("classification") or {}).get("domain") or "unknown"
        x["by_form_domain"][fd] = x["by_form_domain"].get(fd, 0) + 1
    for x in real.values():
        x["template_families"] = len(x["template_families"])
        doms = x.pop("_domains", [])
        shares = [v / len(doms) for v in x["by_domain"].values()] if doms else []
        x["diversity"] = {"source_domains": len(x["by_domain"]), "source_domain_hhi": round(sum(v * v for v in shares), 4) if shares else None,
                          "largest_domain_share": round(max(shares), 4) if shares else None,
                          "pairs_per_template_family": round(x["pairs"] / x["template_families"], 3) if x["template_families"] else None}
    synth = {r["pair_type"]: r["n"] for r in db.q("SELECT pair_type, count(*) AS n FROM pairs WHERE pair_type LIKE 'synthetic%' GROUP BY pair_type")}
    excluded = excluded_formats()
    legacy = sum(1 for d in db.docs() if d.get("observed_pair_links") and d.get("original_format") in excluded)
    return {"real_pairs": {k: real.get(k, {"pairs": 0}) for k in REAL_PAIR_TYPES}, "synthetic_pairs": synth,
            "excluded_formats": sorted(excluded), "observed_pair_documents_in_excluded_formats": legacy,
            "synthetic_policy": synthetic_policy(),
            "note": "observed_verified requires a recorded human decision (formbench corpus review-pairs); candidates are not counted as verified"}


def run(db: DB, include_synthetic: bool = False, seed: int = 20261014, limit: int | None = None, dry_run: bool = False, resume: bool = True,
        workers: int | None = None, log=print) -> dict:
    stats = {"observed_candidates": 0, "observed_verified": 0, "templates_eligible": 0, "templates_with_contract": 0, "contracts_frozen": 0,
             "contracts_rejected": 0, "review_fields": 0, "errors": 0, "by_scenario": {}, "by_pair_type": {}, "rejected_reasons": {}}
    # observed pairs = pairs the publisher put side by side (page / unit evidence). The template-family guesser
    # (observed_candidates) paired classifier-labelled documents across institutions and formats without publisher
    # evidence; off since 2026-10-15 (181 such candidates, 125 cross-institution, 10 cross-format).
    cands = (observed_candidates(db) if _cfg_flag("family_candidates") else []) + page_pair_candidates(db)
    stats["observed_candidates"] = len(cands)
    stats["page_pairs_contradicted_by_text"] = len(getattr(page_pair_candidates, "last_contradicted", []))
    stats["page_pairs_not_filled_in"] = {v: sum(1 for u in getattr(page_pair_candidates, "last_unfilled", []) if u["verdict"] == v) for v in ("no_fill", "guidance_only")}
    if not dry_run:
        record_observed(db, cands)
        stats["dedupe"] = dedupe_observed(db)
    stats["observed_verified"] = db.one("SELECT count(*) AS n FROM pairs WHERE pair_type='observed_verified'")["n"]
    stats["pair_counts"] = pair_counts(db)
    if not include_synthetic:
        return stats
    if not synthetic_policy()["generate_new"]:
        stats["synthetic"] = "archived: generation disabled by configs/pairs.yaml (existing synthetic pairs are kept unchanged)"
        return stats
    todo = eligible_templates(db)
    stats["templates_eligible"] = len(todo)
    if resume:
        todo = [d for d in todo if db.q_status("pair", d["document_id"]) != "done"]
    if limit:
        todo = todo[:limit]
    if dry_run:
        for d in todo:
            log(f"[dry-run] would synthesise completions for {d['document_id']}")
        return stats
    jobs = [(d["document_id"], d["original_format"], seed) for d in todo]
    for o in resources.in_order(resources.run_jobs(_synth_one, jobs, workers=resources.plan_workers(workers), log=log, label="pair")):
        res = o.result if o.status == "ok" else {"document_id": jobs[o.index][0], "error": f"{o.status}: {o.error.splitlines()[0] if o.error else ''}"[:300]}
        doc = db.get_doc(res["document_id"])
        if res["error"]:
            stats["errors"] += 1
            db.q_done("pair", res["document_id"], "error", res["error"])
            log(f"pair {res['document_id']} ERROR {res['error']}")
            continue
        for c in res["contracts"]:
            db.x("INSERT OR REPLACE INTO pairs(pair_id,template_id,completed_id,pair_type,evidence,confidence,eval_eligible,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (c["task_id"], res["document_id"], c["completed_sha256"], c["pair_type"],
                  cjson({"seed": seed, "generator_version": GENERATOR_VERSION, "replayed_and_verified": True, "template_cost": c["cost"],
                         "template_exact": c["exact"]}), 1.0, 1, utcnow()))
            stats["contracts_frozen"] += 1
            stats["by_scenario"][c["scenario"]] = stats["by_scenario"].get(c["scenario"], 0) + 1
            stats["by_pair_type"][c["pair_type"]] = stats["by_pair_type"].get(c["pair_type"], 0) + 1
        for r in res["rejected"]:
            stats["contracts_rejected"] += 1
            stats["rejected_reasons"][r["reason"]] = stats["rejected_reasons"].get(r["reason"], 0) + 1
            annotations.enqueue_review(db, "contract_rejected", r["task_id"], r)
        for f in res["review_fields"]:
            stats["review_fields"] += 1
            annotations.enqueue_review(db, "field_low_confidence", f"{res['document_id']}:{f['field_id']}", f)
        if res["contracts"]:
            stats["templates_with_contract"] += 1
            doc["pair_type"] = res["contracts"][0]["pair_type"]
            doc["pair_evidence"] = [{"task_id": c["task_id"], "completed_sha256": c["completed_sha256"]} for c in res["contracts"]]
            doc["synthetic_status"] = "synthetic_completions_generated"
        doc["contract_summary"] = res.get("summary")
        db.put_doc(doc)
        db.q_done("pair", res["document_id"], "done")
        log(f"pair {res['document_id']} contracts={len(res['contracts'])} rejected={len(res['rejected'])} review_fields={len(res['review_fields'])}")
    write_json(path("reports", "coverage", "pairing_summary.json"), {"last_invocation": stats, "totals": totals(db)})
    return stats


def totals(db: DB) -> dict:
    """Cumulative pairing state recomputed from the store (independent of how many resumable invocations were needed)."""
    import json
    pairs = db.q("SELECT pair_id, pair_type, eval_eligible FROM pairs")
    rq = db.q("SELECT kind, payload FROM review_queue")
    rejected = [json.loads(r["payload"]) for r in rq if r["kind"] == "contract_rejected"]
    reasons: dict = {}
    for r in rejected:
        key = r["reason"] + (":" + r["solver"] if r.get("solver") else "")
        reasons[key] = reasons.get(key, 0) + 1
    by_scn: dict = {}
    for r in pairs:
        if "--" in r["pair_id"]:
            scn = r["pair_id"].rsplit("--", 1)[1]
            by_scn[scn] = by_scn.get(scn, 0) + 1
    return {"templates_eligible": len(eligible_templates(db)), "templates_with_contract": len({r["pair_id"].rsplit("--", 1)[0] for r in pairs if "--" in r["pair_id"]}),
            "contracts_frozen": sum(1 for r in pairs if r["eval_eligible"]), "by_scenario": dict(sorted(by_scn.items())),
            "by_pair_type": {t: sum(1 for r in pairs if r["pair_type"] == t) for t in sorted({r["pair_type"] for r in pairs})},
            "observed_verified": sum(1 for r in pairs if r["pair_type"] == "observed_verified"),
            "observed_candidates": sum(1 for r in pairs if r["pair_type"] == "observed_candidate"),
            "contracts_rejected": len(rejected), "rejected_reasons": dict(sorted(reasons.items())),
            "low_confidence_fields_in_review_queue": sum(1 for r in rq if r["kind"] == "field_low_confidence")}


# ----------------------------------------------------------------------------------------------- duplicates
DEDUPE_VERSION = "1.0"
DUP_TEXT_SAME = 0.60          # character-bigram Jaccard of the blanks: the same form (any publisher)
DUP_TEXT_FAMILY = 0.35        # ... with a page layout this close (perceptual hash distance) = the same template, other title
DUP_PHASH_MAX = 14
KEEP_ORDER = {"observed_verified": 0, "observed_candidate": 1}


def _bigrams(text: str) -> frozenset:
    """Similarity units: character bigrams for CJK / Hangul text, word bigrams for Latin-script text (character
    bigrams of English are shared by almost any two English documents: 11 unrelated forms were grouped, 2026-10-15)."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "").lower()
    letters = re.sub(r"[\W\d_]+", "", t)
    if letters and sum(c.isascii() for c in letters) / len(letters) > 0.5:
        words = re.findall(r"[a-zà-ÿ]{2,}", t)
        return frozenset(f"{a} {b}" for a, b in zip(words, words[1:]))
    return frozenset(letters[i:i + 2] for i in range(len(letters) - 1))


def _phash_distance(a: str | None, b: str | None) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def dedupe_observed(db: DB) -> dict:
    """User requirement 2026-10-15: no duplicate pairs, in collection or in review. Among verified and open pairs,
    blanks that are the same form (bigram Jaccard >= 0.60) or the same template under another title (>= 0.35 and
    page-layout phash distance <= 14) form one group; one representative stays (verified first, then grade,
    containment, pair id), the others become observed_duplicate with the representative and the evidence. A review
    decision on a duplicate is kept inside its evidence (reversible)."""
    import json
    rows = [dict(r) for r in db.q("SELECT pair_id, pair_type, template_id, evidence FROM pairs WHERE pair_type IN "
                                  "('observed_verified','observed_candidate','observed_duplicate')")]
    info = {}
    for r in rows:
        p = path("data", "normalized", f"{r['template_id']}.json")
        side = read_json(p) if p.exists() else {}
        info[r["pair_id"]] = (_bigrams(side.get("text_shingle_source", "")), side.get("phash"))
    # earlier duplicates are re-evaluated from their original state
    for r in rows:
        if r["pair_type"] == "observed_duplicate":
            r["pair_type"] = (json.loads(r["evidence"] or "{}").get("duplicate") or {}).get("was", "observed_candidate")
    elig = [r for r in rows if r["pair_type"] in KEEP_ORDER and info[r["pair_id"]][0]]
    par = {r["pair_id"]: r["pair_id"] for r in elig}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    why = {}
    for i, a in enumerate(elig):
        ba, ha = info[a["pair_id"]]
        for b in elig[i + 1:]:
            bb, hb = info[b["pair_id"]]
            inter = len(ba & bb)
            if not inter:
                continue
            j = inter / len(ba | bb)
            d = _phash_distance(ha, hb)
            if j >= DUP_TEXT_SAME or (j >= DUP_TEXT_FAMILY and d <= DUP_PHASH_MAX):
                par[find(a["pair_id"])] = find(b["pair_id"])
                why[(a["pair_id"], b["pair_id"])] = {"bigram_jaccard": round(j, 3), "phash_distance": d,
                                                     "rule": "same_form" if j >= DUP_TEXT_SAME else "same_template"}
    groups: dict = {}
    for r in elig:
        groups.setdefault(find(r["pair_id"]), []).append(r)
    marked = restored = 0
    for g in groups.values():
        def key(r):
            ev = json.loads(r["evidence"] or "{}")
            return (KEEP_ORDER[r["pair_type"]], {"strong": 0, "medium": 1, "weak": 2}.get(ev.get("grade"), 3),
                    -(ev.get("template_text_containment") or 0), r["pair_id"])
        g.sort(key=key)
        rep = g[0]
        for r in g:
            ev = json.loads(r["evidence"] or "{}")
            if r is rep or len(g) == 1:
                if "duplicate" in ev:                  # was a duplicate, now representative or alone again
                    ev.pop("duplicate")
                    db.x("UPDATE pairs SET pair_type=?, evidence=? WHERE pair_id=?", (r["pair_type"], cjson(ev), r["pair_id"]))
                    restored += 1
                continue
            link = why.get((r["pair_id"], rep["pair_id"])) or why.get((rep["pair_id"], r["pair_id"])) or {"rule": "same_group"}
            ev["duplicate"] = {"of": rep["pair_id"], "was": r["pair_type"], "version": DEDUPE_VERSION, **link}
            db.x("UPDATE pairs SET pair_type='observed_duplicate', evidence=? WHERE pair_id=?", (cjson(ev), r["pair_id"]))
            db.x("DELETE FROM review_queue WHERE item_id=? AND status='open'", (r["pair_id"],))
            marked += 1
    return {"duplicate_groups": sum(1 for g in groups.values() if len(g) > 1), "pairs_marked_duplicate": marked, "restored": restored}
