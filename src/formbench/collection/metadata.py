"""Document record schema: every field required by AGENTS.md §3 is always present (None when unknown)."""
from __future__ import annotations

REQUIRED_FIELDS = [
    "document_id", "source_url", "source_domain", "crawl_query", "discovered_at", "downloaded_at", "original_format",
    "mime_type", "sha256", "file_size", "page_count", "language", "document_family_id", "template_family_id",
    "is_template", "is_completed", "pair_type", "pair_evidence", "license_url", "rights_status",
    "redistribution_allowed", "robots_result", "tos_status", "privacy_review", "synthetic_status", "parser_status",
    "converter_status", "version_info", "error_code",
]

PAIR_TYPES = ["observed_verified", "observed_candidate", "synthetic_from_native", "synthetic_from_converted", "unpaired"]
# which pair types may enter the main evaluation, and at which evidence level
PAIR_EVAL_LEVEL = {
    "observed_verified": "main", "synthetic_from_native": "main", "synthetic_from_converted": "main_converted_stratum",
    "observed_candidate": "excluded_until_verified", "unpaired": "excluded",
}


def new_record(**kw) -> dict:
    rec = {k: None for k in REQUIRED_FIELDS}
    rec.update({"pair_type": "unpaired", "pair_evidence": [], "redistribution_allowed": False, "privacy_review": "pending",
                "synthetic_status": "not_synthetic", "parser_status": "pending", "converter_status": "not_applicable",
                "version_info": {}, "is_template": None, "is_completed": None})
    rec.update(kw)
    return rec


def validate_record(rec: dict) -> list[str]:
    errs = [f"missing field: {k}" for k in REQUIRED_FIELDS if k not in rec]
    if rec.get("pair_type") not in PAIR_TYPES:
        errs.append(f"invalid pair_type: {rec.get('pair_type')}")
    if rec.get("redistribution_allowed") not in (True, False):
        errs.append("redistribution_allowed must be boolean")
    if not rec.get("document_id"):
        errs.append("document_id empty")
    if rec.get("sha256") and len(rec["sha256"]) != 64:
        errs.append("sha256 malformed")
    return errs
