"""Task contract JSON Schema (v1.0) plus semantic consistency checks."""
from __future__ import annotations

import jsonschema

from ..collection.metadata import PAIR_TYPES

SCHEMA_VERSION = "1.0"
CHECKED_GLYPHS = ["■", "☑", "☒", "✔", "✓", "√", "V", "v", "○", "●", "▣", "Ｖ"]
UNCHECKED_GLYPHS = ["□", "☐", " ", ""]
FIELD_KINDS = ["text", "long_text", "date", "number", "check", "row_cell", "image"]
TARGET_TYPES = ["field_text_equals", "field_checked", "table_row_count_at_least", "image_in_region"]
PROTECTED_TYPES = ["unchanged_text", "preserve_table_merges", "preserve_objects", "no_overlap", "no_extraneous_marks", "file_valid"]

_RECT = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
CONTRACT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "task_id", "template_id", "scenario", "fields", "target_predicates", "protected_predicates",
                 "allowed_reflow", "annotations_verified", "pair_type", "page_sizes"],
    "additionalProperties": True,
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "task_id": {"type": "string", "minLength": 1},
        "template_id": {"type": "string", "minLength": 1},
        "scenario": {"type": "string"},
        "allowed_reflow": {"type": "boolean"},
        "annotations_verified": {"type": "boolean"},
        "pair_type": {"enum": PAIR_TYPES},
        "page_sizes": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}, "minItems": 1},
        "min_font_pt": {"type": "number", "minimum": 1},
        "fields": {"type": "array", "items": {
            "type": "object",
            "required": ["field_id", "kind", "mode", "page", "region", "label", "anchor", "confidence"],
            "properties": {
                "field_id": {"type": "string", "minLength": 1},
                "kind": {"enum": FIELD_KINDS},
                "mode": {"enum": ["container", "inline"]},
                "data_type": {"type": "string"},
                "page": {"type": "integer", "minimum": 0},
                "region": _RECT,
                "label": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "initial_text": {"type": "string"},
                "para_pattern": {"type": "array", "items": {"type": "string"}},
                "slot": {"type": "integer", "minimum": 0},
                "gap_lead": {"type": "integer", "minimum": 0},
                "anchor": {"type": ["object", "null"], "properties": {
                    "text": {"type": "string"}, "page": {"type": "integer"}, "bbox": _RECT, "relation": {"enum": ["left", "above", "inline", "none"]},
                    "occurrence": {"type": "integer", "minimum": 0}, "container_bbox": {"anyOf": [_RECT, {"type": "null"}]}}},
                "repeat": {"type": ["object", "null"], "properties": {
                    "group": {"type": "string"}, "row": {"type": "integer", "minimum": 0}, "col": {"type": "integer", "minimum": 0},
                    "new_row": {"type": "boolean"}}},
                "state_transition": {"type": "object"},
            }}},
        "repeat_groups": {"type": "array", "items": {
            "type": "object", "required": ["group_id", "page", "columns", "first_row_top", "row_height", "existing_rows"],
            "properties": {"group_id": {"type": "string"}, "page": {"type": "integer"}, "columns": {"type": "array", "items": {
                "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}, "minItems": 1},
                "first_row_top": {"type": "number"}, "row_height": {"type": "number", "exclusiveMinimum": 0},
                "existing_rows": {"type": "integer", "minimum": 0}, "bordered": {"type": "boolean"}}}},
        "target_predicates": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["type"],
            "properties": {"type": {"enum": TARGET_TYPES}, "field_id": {"type": "string"}, "table_id": {"type": "string"},
                           "value": {"type": ["string", "boolean", "integer"]}, "asset": {"type": "string"},
                           "accept": {"type": "array", "items": {"type": "string"}}},
            "allOf": [
                {"if": {"properties": {"type": {"const": "field_text_equals"}}}, "then": {"required": ["field_id", "value"], "properties": {"value": {"type": "string", "minLength": 1}}}},
                {"if": {"properties": {"type": {"const": "field_checked"}}}, "then": {"required": ["field_id", "value"], "properties": {"value": {"type": "boolean"}}}},
                {"if": {"properties": {"type": {"const": "table_row_count_at_least"}}}, "then": {"required": ["table_id", "value"], "properties": {"value": {"type": "integer", "minimum": 1}}}},
                {"if": {"properties": {"type": {"const": "image_in_region"}}}, "then": {"required": ["field_id", "asset"]}},
            ]}},
        "protected_predicates": {"type": "array", "items": {
            "type": "object", "required": ["type"], "properties": {"type": {"enum": PROTECTED_TYPES}, "region_id": {"type": "string"},
                                                                  "table_id": {"type": "string"}}}},
    },
}


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def validate_contract(c: dict) -> list[str]:
    """Schema + semantic checks. Returns a list of error strings (empty = valid)."""
    errs = [f"schema: {'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
            for e in sorted(jsonschema.Draft202012Validator(CONTRACT_SCHEMA).iter_errors(c), key=lambda e: list(map(str, e.absolute_path)))]
    if errs:
        return errs
    ids = [f["field_id"] for f in c["fields"]]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        errs.append(f"duplicate field_id: {dup}")
    fields = {f["field_id"]: f for f in c["fields"]}
    groups = {g["group_id"] for g in c.get("repeat_groups", [])}
    for f in c["fields"]:
        x0, y0, x1, y1 = f["region"]
        if not (x1 > x0 and y1 > y0):
            errs.append(f"{f['field_id']}: empty region")
        if f["page"] >= len(c["page_sizes"]) and not (f.get("repeat") or {}).get("new_row"):
            errs.append(f"{f['field_id']}: page out of range")
        if f.get("repeat") and f["repeat"].get("group") not in groups:
            errs.append(f"{f['field_id']}: unknown repeat group")
    fl = [f for f in c["fields"] if f["mode"] == "container" and not (f.get("repeat") or {}).get("new_row")]      # new rows push content down
    for i in range(len(fl)):
        for j in range(i + 1, len(fl)):
            if fl[i]["page"] == fl[j]["page"] and _iou(fl[i]["region"], fl[j]["region"]) > 0.5:
                errs.append(f"field regions collide: {fl[i]['field_id']} / {fl[j]['field_id']}")
    seen: dict[str, object] = {}
    for p in c["target_predicates"]:
        fid = p.get("field_id")
        if p["type"] in ("field_text_equals", "field_checked", "image_in_region"):
            if fid not in fields:
                errs.append(f"predicate references unknown field: {fid}")
                continue
            if fid in seen and seen[fid] != (p["type"], p.get("value")):
                errs.append(f"conflicting predicates for field {fid}")
            seen[fid] = (p["type"], p.get("value"))
            kind = fields[fid]["kind"]
            if p["type"] == "field_checked" and kind != "check":
                errs.append(f"{fid}: field_checked on non-check field")
            if p["type"] == "image_in_region" and kind != "image":
                errs.append(f"{fid}: image_in_region on non-image field")
        elif p["type"] == "table_row_count_at_least" and p["table_id"] not in groups:
            errs.append(f"predicate references unknown repeat group: {p['table_id']}")
    if c["pair_type"] not in ("observed_verified", "synthetic_from_native", "synthetic_from_converted"):
        errs.append(f"pair_type {c['pair_type']} is not eligible for scoring")
    if not c["annotations_verified"]:
        errs.append("annotations not verified: contract must be frozen before evaluation")
    return errs
