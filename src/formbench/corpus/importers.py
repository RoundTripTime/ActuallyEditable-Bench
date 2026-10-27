"""fingerprint -> parse -> classify -> privacy review for every fetched document (FormatAdapter orchestration).

HWP / DOCX / ODT are additionally converted to HWPX (derived files) so that they can serve as editable originals;
conversion status and tool versions are recorded per document.
"""
from __future__ import annotations

import re

from ..collection.discovery import blob_path
from ..collection.metadata import validate_record
from ..contracts.extract import extract_fields
from ..contracts.verify import DocView
from ..document_ir import fonts
from ..document_ir.normalize import native_object_stats, semantic_fingerprint, structure_fingerprint
from ..formats import docx as docx_fmt
from ..formats import hwp as hwp_fmt
from ..formats import images as img_fmt
from ..formats import odt as odt_fmt
from ..formats import pdf as pdf_fmt
from ..formats.hwpx import HwpxPackage
from ..formats.renderers import draw_layout, pdf_bytes, pdf_to_pngs
from ..util import DB, FormbenchError, Status, path, write_json
from . import classify as clf
from . import privacy

PARSER_VERSION = "1.0"


def derived_hwpx_path(doc_id: str):
    return path("data", "derived", "converted", f"{doc_id}.hwpx")


def hwpx_bytes_for(rec: dict) -> bytes | None:
    """Editable HWPX for a document: the original if it is HWPX, otherwise the recorded conversion (if any)."""
    if rec.get("original_format") == "hwpx":
        return HwpxPackage.load(blob_path(rec["sha256"]).read_bytes()).to_bytes()
    p = derived_hwpx_path(rec["document_id"])
    return p.read_bytes() if p.exists() else None


def _hwpx_features(data: bytes) -> dict:
    pkg = HwpxPackage.load(data)
    view = DocView(pkg)
    fields, groups, review = extract_fields(view)
    ir = view.ir
    stats = native_object_stats(ir, view.layout)
    text = "\n".join(p.text for p in ir.paras() if p.text.strip())
    value_like = sum(1 for t in ir.tables() for c in t.cells if c.text.strip() and re.search(r"\d{2,}|@", c.text) and len(c.text.strip()) < 40)
    png = pdf_to_pngs(pdf_bytes(draw_layout(view.layout, pkg)), 40)[:1]
    return {"text": text, "page_count": view.layout.n_pages, "stats": stats, "semantic_fingerprint": semantic_fingerprint(ir),
            "structure_fingerprint": structure_fingerprint(ir), "ref_issues": len([i for i in pkg.validate_refs() if i["severity"] == "error"]),
            "opaque": sorted({o["tag"] for o in ir.opaque}), "phash": img_fmt.phash(png[0]) if png else None,
            "fonts": sorted({f for fl in ir.fonts.values() for f in fl})[:20],
            "features": {"n_fields": len(fields), "n_review_fields": len(review), "n_groups": len(groups), "n_tables": stats.get("tables", 0),
                         "n_merged": stats.get("merged_cells", 0), "n_checks": sum(1 for f in fields if f["kind"] == "check"),
                         "n_long": sum(1 for f in fields if f["kind"] == "long_text"), "n_images": stats.get("shape_pic", 0),
                         "n_textboxes": stats.get("textboxes", 0), "pages": view.layout.n_pages, "n_value_like": value_like,
                         "field_kinds": {k: sum(1 for f in fields if f["kind"] == k) for k in sorted({f["kind"] for f in fields})}}}


def process_document(rec: dict) -> dict:
    """Parse one document record in place; returns the normalized side-car dict."""
    data = blob_path(rec["sha256"]).read_bytes()
    fmt = rec["original_format"]
    norm: dict = {"document_id": rec["document_id"], "parser_version": PARSER_VERSION}
    text, feats, title = "", {}, (rec.get("link_text") or rec.get("filename") or "")
    rec["converter_status"] = "not_applicable"
    rec["version_info"] = {"parser": PARSER_VERSION, "fonts": fonts.environment()}
    try:
        if fmt == "hwpx":
            info = _hwpx_features(data)
        elif fmt in ("hwp", "docx", "odt"):
            if fmt == "hwp":
                meta = hwp_fmt.hwp_info(data)
                text = meta.get("text", "")
                norm["hwp"] = {k: v for k, v in meta.items() if k not in ("text", "preview_text")}
                conv, cstat, cdetail = hwp_fmt.hwp_to_hwpx(data)
                rec["version_info"]["converter"] = hwp_fmt.java_tool().get("versions", {})
            else:
                meta = docx_fmt.docx_info(data) if fmt == "docx" else odt_fmt.odt_info(data)
                text = meta.get("text", "")
                try:
                    conv, cinfo = (docx_fmt.docx_to_hwpx if fmt == "docx" else odt_fmt.odt_to_hwpx)(data)
                    cstat, cdetail = Status.OK, str(cinfo.get("conversion_losses", {}))
                except Exception as e:      # noqa: BLE001 - conversion failure is a recorded outcome
                    conv, cstat, cdetail = None, Status.FAILED, f"{type(e).__name__}: {e}"[:200]
                rec["version_info"]["converter"] = {"formbench_docx_odt": PARSER_VERSION}
            rec["converter_status"] = cstat
            norm["conversion_detail"] = cdetail
            info = None
            if conv is not None:
                try:
                    clean = HwpxPackage.load(conv).to_bytes()          # re-pack: stable zip layout for every consumer
                    info = _hwpx_features(clean)
                    out = derived_hwpx_path(rec["document_id"])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(clean)
                except Exception as e:      # noqa: BLE001
                    rec["converter_status"] = Status.FAILED
                    norm["conversion_detail"] = f"converted file unusable: {type(e).__name__}: {e}"[:200]
            if info is None:
                info = {"text": text, "page_count": meta.get("page_count"), "features": {"n_tables": meta.get("tables", 0), "pages": meta.get("page_count") or 1}}
        elif fmt == "pdf":
            meta = pdf_fmt.pdf_info(data)
            pngs = pdf_fmt.pdf_page_pngs(data, 40, 1) if not meta.get("encrypted") else []
            info = {"text": meta.get("text", ""), "page_count": meta["page_count"], "phash": img_fmt.phash(pngs[0]) if pngs else None,
                    "features": {"form_fields": meta.get("form_fields", 0), "pages": meta["page_count"], "is_scanned": meta.get("is_scanned"),
                                 "n_drawings": meta.get("n_drawings", 0)}}
            norm["pdf"] = {k: v for k, v in meta.items() if k not in ("text", "widgets")}
        elif fmt in ("png", "jpeg", "tiff"):
            meta = img_fmt.image_info(data)
            info = {"text": "", "page_count": meta["page_count"], "phash": img_fmt.phash(data), "features": {"pages": meta["page_count"], "is_scanned": True}}
            norm["image"] = meta
        else:
            rec["parser_status"] = Status.UNSUPPORTED
            rec["error_code"] = f"unsupported_format:{fmt}"
            return norm
        text = info.get("text") or text
        feats = info.get("features", {})
        rec["page_count"] = info.get("page_count")
        rec["parser_status"] = Status.OK
        rec["error_code"] = None
        norm.update({k: info[k] for k in ("stats", "semantic_fingerprint", "structure_fingerprint", "phash", "opaque", "ref_issues", "fonts") if k in info})
    except FormbenchError as e:
        rec["parser_status"], rec["error_code"] = Status.FAILED, e.code
        norm["error"] = str(e)[:300]
    except Exception as e:      # noqa: BLE001 - a parser crash on one document must not stop the corpus run
        rec["parser_status"], rec["error_code"] = Status.FAILED, f"parser_exception:{type(e).__name__}"
        norm["error"] = f"{type(e).__name__}: {e}"[:300]
    rec["language"] = clf.detect_language(text)
    cls = clf.classify(text, title, feats)
    rec["is_template"], rec["is_completed"] = cls["is_template"], cls["is_completed"]
    rec["classification"] = {k: cls[k] for k in ("kind", "is_form", "structure", "domain", "difficulty", "difficulty_score", "form_cues")}
    rec["privacy_review"], pdetail = privacy.review(text, cls["is_completed"], cls["is_template"],
                                                    source_hint=f"{rec.get('source_url') or ''} {rec.get('filename') or ''}")
    norm.update({"classification": cls, "privacy": pdetail, "text_chars": len(text), "text_shingle_source": re.sub(r"\s+", "", text)[:20000],
                 "title": title})
    return norm


def process_all(db: DB, limit: int | None = None, resume: bool = True, dry_run: bool = False, log=print) -> dict:
    stats = {"processed": 0, "ok": 0, "failed": 0, "skipped": 0, "record_errors": 0, "by_format": {}}
    for rec in db.docs():
        if resume and rec.get("parser_status") not in (None, "pending"):
            stats["skipped"] += 1
            continue
        if limit and stats["processed"] >= limit:
            break
        if dry_run:
            log(f"[dry-run] would parse {rec['document_id']} ({rec['original_format']})")
            continue
        if not blob_path(rec["sha256"]).exists():
            rec["parser_status"], rec["error_code"] = Status.FAILED, "blob_missing"
            db.put_doc(rec)
            continue
        norm = process_document(rec)
        write_json(path("data", "normalized", f"{rec['document_id']}.json"), norm, indent=None)
        errs = validate_record(rec)
        stats["record_errors"] += len(errs)
        db.put_doc(rec)
        stats["processed"] += 1
        stats["ok" if rec["parser_status"] == Status.OK else "failed"] += 1
        f = stats["by_format"].setdefault(rec["original_format"], {"ok": 0, "failed": 0})
        f["ok" if rec["parser_status"] == Status.OK else "failed"] += 1
        log(f"parse {rec['document_id']} {rec['original_format']} {rec['parser_status']} conv={rec['converter_status']} pages={rec['page_count']} "
            f"kind={rec.get('classification', {}).get('kind')} {rec.get('error_code') or ''}")
    return stats
