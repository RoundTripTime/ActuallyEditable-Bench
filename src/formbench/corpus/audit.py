"""Audit: corpus distributions, discovery bias, collection / policy outcomes, privacy and licence status, pair
quality, and the publishable manifest (binaries only where redistribution is permitted)."""
from __future__ import annotations

import shutil
from collections import Counter

from ..collection.discovery import blob_path, export_ext
from ..collection.metadata import PAIR_EVAL_LEVEL, validate_record
from ..util import DB, path, utcnow, write_json, write_text


def _c(items) -> dict:
    return dict(sorted(Counter(str(i) for i in items).items(), key=lambda kv: (-kv[1], kv[0])))


def _hhi(counts: dict) -> float:
    n = sum(counts.values())
    return round(sum((v / n) ** 2 for v in counts.values()), 4) if n else 0.0


def _pair_counts(db: DB) -> dict:
    from .pairing import pair_counts
    return pair_counts(db)


def run(db: DB) -> dict:
    docs = db.docs()
    ok = [d for d in docs if d.get("parser_status") == "ok"]
    cand = db.q("SELECT kind, fetch_status, source_domain, provider, crawl_query, policy_reason FROM candidates")
    doc_c = [r for r in cand if r["kind"] == "document"]
    page_c = [r for r in cand if r["kind"] == "page"]
    q = db.q("SELECT provider, engine, query, status, n_results FROM query_log")
    by_query = Counter(d.get("crawl_query") for d in docs)
    by_domain = Counter(d.get("source_domain") for d in docs)
    domains = Counter((d.get("classification") or {}).get("domain") for d in ok)
    all_domains = ["행정·민원", "취업·지원", "교육·시험", "연구·사업·과제", "의료·복지", "법률·계약", "회계·세무", "인사·노무", "건설·제조·품질·안전", "행사·참가·접수",
                   "점검·감사·일지", "견적·발주", "설문·조사", "회의·기록", "인증·증명"]
    rep = {
        "generated_at": utcnow(),
        "discovery": {
            "queries_run": len(q), "distinct_queries": len({r["query"] for r in q}),
            "provider_status": {p: _c(r["status"] for r in q if r["provider"] == p) for p in sorted({r["provider"] for r in q})},
            "hits_per_provider": {p: sum(r["n_results"] or 0 for r in q if r["provider"] == p) for p in sorted({r["provider"] for r in q})},
            "query_word_counts": _c(len(r["query"].split()) for r in q),
            "queries_with_zero_hits": sorted({f"{r['provider']}::{r['query']}" for r in q if r["status"] == "ok" and not r["n_results"]})[:80],
            "search_results_logged": db.one("SELECT count(*) AS n FROM search_log")["n"],
            "duplicate_search_results": db.one("SELECT count(*) AS n FROM search_log WHERE is_duplicate=1")["n"],
            "page_candidates": _c(r["fetch_status"] or "not_visited" for r in page_c),
            "distinct_domains_seen": len({r["source_domain"] for r in cand}),
        },
        "collection": {
            "document_candidates": len(doc_c), "outcomes": _c(r["fetch_status"] or "not_attempted" for r in doc_c),
            "blocked_or_held_rate": round(sum(1 for r in doc_c if r["fetch_status"] in ("policy_denied", "policy_review", "blocked")) / len(doc_c), 4) if doc_c else None,
            "failure_rate": round(sum(1 for r in doc_c if r["fetch_status"] in ("http_error", "error", "too_large", "not_a_document", "quarantined")) / len(doc_c), 4) if doc_c else None,
            "policy_reasons": _c((r["policy_reason"] or "")[:70] for r in doc_c if r["fetch_status"] in ("policy_denied", "policy_review", "blocked", "skipped_budget")),
            "held_domains": _c(r["source_domain"] for r in doc_c if r["fetch_status"] == "policy_review"),
            "http_requests": db.one("SELECT count(*) AS n FROM http_log")["n"],
            "http_status": _c(r["status"] for r in db.q("SELECT status FROM http_log")),
            "stopped_domains": [dict(r) for r in db.q("SELECT domain, stop_reason FROM domain_state WHERE stop_reason IS NOT NULL")],
        },
        "corpus": {
            "documents": len(docs), "parsed_ok": len(ok), "parser_status": _c(d.get("parser_status") for d in docs),
            "parser_errors": _c(d.get("error_code") for d in docs if d.get("error_code")),
            "by_format": _c(d.get("original_format") for d in docs), "converter_status": _c(f"{d.get('original_format')}:{d.get('converter_status')}" for d in docs),
            "by_source_domain": _c(d.get("source_domain") for d in docs), "source_domain_hhi": _hhi(by_domain),
            "by_language": _c(d.get("language") for d in ok), "page_count": _c(min(d.get("page_count") or 0, 10) for d in ok),
            "kind": _c((d.get("classification") or {}).get("kind") for d in ok), "structure": _c((d.get("classification") or {}).get("structure") for d in ok),
            "domain": dict(sorted(domains.items(), key=lambda kv: -kv[1])), "difficulty": _c((d.get("classification") or {}).get("difficulty") for d in ok),
            "document_families": len({d.get("document_family_id") for d in ok}), "template_families": len({d.get("template_family_id") for d in ok}),
            "non_canonical_duplicates": sum(1 for d in ok if d.get("is_canonical") is False),
            "record_validation_errors": sum(len(validate_record(d)) for d in docs),
        },
        "bias": {
            "documents_per_query": dict(sorted(by_query.items(), key=lambda kv: -kv[1])), "query_hhi": _hhi(by_query),
            "top_query_share": round(max(by_query.values()) / len(docs), 4) if docs else None,
            "top_domain_share": round(max(by_domain.values()) / len(docs), 4) if docs else None,
            "underrepresented_domains(<3 docs)": [d for d in all_domains if domains.get(d, 0) < 3],
            "missing_formats": [f for f in ("hwpx", "hwp", "docx", "odt", "pdf", "png", "jpeg", "tiff") if not any(d.get("original_format") == f for d in docs)],
            "note": "Source concentration is reported, not hidden: rights rules allow binary download only from reviewed sources.",
        },
        "rights": {"rights_status": _c(d.get("rights_status") for d in docs), "redistribution_allowed": _c(d.get("redistribution_allowed") for d in docs),
                   "robots_result": _c(d.get("robots_result") for d in docs), "tos_status": _c(d.get("tos_status") for d in docs),
                   "takedowns": [dict(r) for r in db.q("SELECT pattern, reason, added_at FROM takedown")]},
        "privacy": {"privacy_review": _c(d.get("privacy_review") for d in docs), "flagged_documents_excluded_from_eval": sum(1 for d in docs if str(d.get("privacy_review", "")).startswith("flagged")),
                    "completed_documents_detected": sum(1 for d in docs if d.get("is_completed"))},
        "security": {"quarantined": sum(1 for r in doc_c if r["fetch_status"] == "quarantined"), "macro_or_script_findings": sum(1 for d in docs if (d.get("security") or {}).get("has_macros")),
                     "av_status": _c((d.get("security") or {}).get("av_status") for d in docs)},
        "pairs": {"real_vs_synthetic": _pair_counts(db),
                  "pair_type_docs": _c(d.get("pair_type") for d in ok), "pairs": _c(r["pair_type"] for r in db.q("SELECT pair_type FROM pairs")),
                  "eval_eligibility_rule": PAIR_EVAL_LEVEL, "review_queue": _c(r["kind"] + ":" + r["status"] for r in db.q("SELECT kind, status FROM review_queue")),
                  "templates_with_synthetic_completions": sum(1 for d in ok if d.get("synthetic_status") == "synthetic_completions_generated")},
        "splits": _c(d.get("split") for d in ok),
        "eval_templates_by_split": _c(d.get("split") for d in ok if d.get("synthetic_status") == "synthetic_completions_generated"),
    }
    write_json(path("reports", "coverage", "corpus_audit.json"), rep)
    md = ["# Corpus audit", "", f"generated: {rep['generated_at']}", ""]
    for sec in ("discovery", "collection", "corpus", "bias", "rights", "privacy", "security", "pairs"):
        md.append(f"## {sec}\n")
        for k, v in rep[sec].items():
            md.append(f"- **{k}**: {v}")
        md.append("")
    md.append(f"## splits\n\n- documents: {rep['splits']}\n- evaluation templates: {rep['eval_templates_by_split']}\n")
    write_text(path("reports", "coverage", "corpus_audit.md"), "\n".join(md))
    return rep


def publish_manifest(db: DB) -> dict:
    """Public manifest: metadata, hashes and source links for every document; binaries only when redistribution is allowed
    and the privacy review cleared the file."""
    rel = path("data", "release")
    shutil.rmtree(rel, ignore_errors=True)
    (rel / "originals").mkdir(parents=True, exist_ok=True)
    lines, n_bin = [], 0
    import json
    for d in db.docs():
        rec = {k: d.get(k) for k in ("document_id", "source_url", "source_domain", "crawl_query", "discovered_at", "downloaded_at", "original_format", "mime_type",
                                     "sha256", "file_size", "page_count", "language", "document_family_id", "template_family_id", "is_template", "is_completed",
                                     "pair_type", "license_url", "rights_status", "redistribution_allowed", "robots_result", "tos_status", "privacy_review",
                                     "synthetic_status", "parser_status", "converter_status", "error_code", "split")}
        rec["classification"] = d.get("classification")
        publish = bool(d.get("redistribution_allowed")) and str(d.get("privacy_review", "")).startswith("auto_cleared") and blob_path(d["sha256"]).exists()
        rec["binary_included"] = publish
        if publish:
            shutil.copyfile(blob_path(d["sha256"]), rel / "originals" / (d["document_id"] + export_ext(d)))
            n_bin += 1
        lines.append(json.dumps(rec, ensure_ascii=False, sort_keys=True))
    write_text(rel / "manifest.jsonl", "\n".join(lines) + "\n")
    return {"documents": len(lines), "binaries_included": n_bin, "metadata_only": len(lines) - n_bin, "manifest": str(rel / "manifest.jsonl")}


def renderer_fidelity(db: DB, limit: int | None = None) -> dict:
    """Cross-renderer check of the internal reference renderer against (a) the publisher's own PDF of the same statutory
    form and (b) LibreOffice + H2Orestart, where available. Conditions differ by construction (other renderer, other
    fonts), so these numbers are reported separately and never mixed with same-condition visual scores."""
    from ..experiment.statistics import quantiles
    from ..formats.pdf import pdf_page_pngs
    from ..metrics.visual import visual_similarity
    docs = db.docs()
    by_group: dict[str, list[dict]] = {}
    for d in docs:
        if d.get("form_group"):
            by_group.setdefault(d["form_group"], []).append(d)
    rows = []
    for members in by_group.values():
        hwp = next((d for d in members if d["original_format"] in ("hwp", "hwpx")), None)
        pdf = next((d for d in members if d["original_format"] == "pdf"), None)
        if not hwp or not pdf:
            continue
        ref = path("data", "derived", "render", hwp["document_id"], "reference.pdf")
        if not ref.exists() or not blob_path(pdf["sha256"]).exists():
            continue
        if limit and len(rows) >= limit:
            break
        try:
            mine = pdf_page_pngs(ref.read_bytes(), 72)
            official = pdf_page_pngs(blob_path(pdf["sha256"]).read_bytes(), 72)
            vs = visual_similarity(official, mine, {"renderer": "publisher_pdf", "dpi": 72}, {"renderer": "internal", "dpi": 72})
            row = {"document_id": hwp["document_id"], "official_pdf": pdf["document_id"], "pages_official": len(official), "pages_internal": len(mine),
                   "ssim": vs["ssim"], "ink_iou": vs["ink_iou"], "page_count_match": vs["page_count_match"]}
            lo = path("data", "derived", "render", hwp["document_id"], "libreoffice.pdf")
            if lo.exists():
                v2 = visual_similarity(official, pdf_page_pngs(lo.read_bytes(), 72), {"renderer": "publisher_pdf", "dpi": 72}, {"renderer": "libreoffice", "dpi": 72})
                row.update({"lo_ssim": v2["ssim"], "lo_page_count_match": v2["page_count_match"]})
            rows.append(row)
        except Exception as e:      # noqa: BLE001
            rows.append({"document_id": hwp["document_id"], "error": f"{type(e).__name__}: {e}"[:160]})
    ok = [r for r in rows if "ssim" in r]
    lo_meta = []
    for p in sorted(path("data", "derived", "render").glob("*/render.json")):
        import json
        m = json.loads(p.read_text(encoding="utf-8")).get("libreoffice")
        if m:
            lo_meta.append(m)
    out = {"note": "cross-renderer comparison (different renderer and fonts): not comparable with same-condition scores",
           "pairs": len(rows), "errors": len(rows) - len(ok),
           "internal_vs_publisher_pdf": {"ssim": quantiles([r["ssim"] for r in ok]), "ink_iou": quantiles([r["ink_iou"] for r in ok]),
                                         "page_count_match": sum(1 for r in ok if r["page_count_match"]), "n": len(ok)},
           "libreoffice_vs_publisher_pdf": {"ssim": quantiles([r["lo_ssim"] for r in ok if "lo_ssim" in r]),
                                            "page_count_match": sum(1 for r in ok if r.get("lo_page_count_match")), "n": sum(1 for r in ok if "lo_ssim" in r)},
           "libreoffice_import": {"attempted": len(lo_meta), "ok": sum(1 for m in lo_meta if m.get("status") == "ok"),
                                  "failed": sum(1 for m in lo_meta if m.get("status") == "failed"),
                                  "internal_vs_libreoffice_ssim": quantiles([m["cross_renderer"]["ssim"] for m in lo_meta if m.get("cross_renderer")])},
           "rows": rows}
    write_json(path("reports", "coverage", "renderer_fidelity.json"), out)
    return {k: v for k, v in out.items() if k != "rows"}
