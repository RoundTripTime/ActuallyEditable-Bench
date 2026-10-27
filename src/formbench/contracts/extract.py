"""Rule-based field extraction from a blank template and task-contract construction (deterministic).

Extraction only reads the *blank original template*; the reconstruction baselines never see its output
(information-flow rule in AGENTS.md §5). Low-confidence fields go to the review queue and are not scored.
"""
from __future__ import annotations

import re

from ..completion.synthetic import GENERATOR_VERSION, data_type_of, persona_for, value_for
from ..document_ir.schema import Table
from .schema import SCHEMA_VERSION
from .verify import DocView, norm

EXTRACTOR_VERSION = "1.0"
MIN_CONFIDENCE = 0.75
MAX_FIELDS_PER_SCENARIO = 40
DEFAULT_PROTECTED = [{"type": "unchanged_text", "region_id": "all_non_target"}, {"type": "preserve_table_merges", "table_id": "*"},
                     {"type": "preserve_objects"}, {"type": "no_overlap"}, {"type": "no_extraneous_marks"}, {"type": "file_valid"}]
_LONG_RX = re.compile(r"사\s*유|내\s*용|동\s*기|소\s*개|비\s*고|의\s*견|계\s*획|목\s*적|요\s*약|개\s*요|특이|설\s*명|경\s*위|description|remarks|comments", re.I)


def _cell_empty(c) -> bool:
    return not c.text.strip() and not any(p.objects() for p in c.paras)


def _label_text(c) -> str:
    return re.sub(r"\s+", " ", " ".join(p.text.strip() for p in c.paras if p.text.strip()))[:40]


def _anchor(view: DocView, cell=None, para_nid: str | None = None, relation: str = "left") -> dict | None:
    nid = para_nid
    if cell is not None:
        nid = next((p.nid for p in cell.paras if p.text.strip()), None)
    if nid is None or nid not in view.para_box:
        return None
    tb = view.text_bbox(nid)
    if tb is None:
        return None
    same = [n for n in view.order if view.ntext.get(n) == view.ntext[nid]]
    cb = view.boxes.get(cell.nid) if cell is not None else None
    return {"text": view.text[nid].strip()[:60], "page": tb[0], "bbox": [round(v, 2) for v in tb[1:]], "relation": relation,
            "occurrence": same.index(nid), "n_occurrences": len(same),
            "container_bbox": [round(cb.x, 2), round(cb.y, 2), round(cb.x1, 2), round(cb.y1, 2)] if cb is not None else None}


def _kind_for(label: str, w: float, h: float) -> tuple[str, str]:
    dt = data_type_of(label)
    if h >= 60 or (h >= 40 and _LONG_RX.search(label or "")):
        return "long_text", "long"
    if dt in ("date", "birth"):
        return "date", dt
    if dt in ("money", "count"):
        return "number", dt
    return "text", dt


def _table_fields(view: DocView, t: Table, out: list, groups: list) -> None:
    grouped: set[str] = set()
    # --- repeat groups: header row + >=2 structurally identical empty rows
    rows: dict[int, list] = {}
    for c in t.cells:
        rows.setdefault(c.row, []).append(c)
    for r in rows:
        rows[r].sort(key=lambda c: c.col)
    sig = {r: tuple((c.col, c.colspan) for c in cs) if all(c.rowspan == 1 for c in cs) else None for r, cs in rows.items()}
    r = 0
    while r < t.n_rows:
        cs = rows.get(r)
        if not cs or sig[r] is None or len(cs) < 2 or not all(_cell_empty(c) for c in cs):
            r += 1
            continue
        end = r
        while end + 1 < t.n_rows and sig.get(end + 1) == sig[r] and all(_cell_empty(c) for c in rows[end + 1]):
            end += 1
        header = rows.get(r - 1) if r > 0 else None
        # header cells must be non-empty and cover every data column (same columns, or wider merged headers)
        if end > r and header and all(not _cell_empty(c) for c in header) and all(t.cell_at(r - 1, c.col) is not None and not _cell_empty(t.cell_at(r - 1, c.col)) for c in cs):
            boxes = [view.boxes.get(c.nid) for c in cs]
            if all(b is not None for b in boxes):
                gid = f"g{len(groups) + 1}"
                same_page = [rr for rr in range(r, end + 1) if all(view.boxes.get(c.nid) is not None and view.boxes[c.nid].page == boxes[0].page for c in rows[rr])]
                groups.append({"group_id": gid, "page": boxes[0].page, "columns": [[round(b.x, 2), round(b.x1, 2)] for b in boxes],
                               "first_row_top": round(boxes[0].y, 2), "row_height": round(boxes[0].h, 2), "existing_rows": len(same_page),
                               "bordered": all(c.bf.bottom.visible for c in cs), "header_labels": [_label_text(t.cell_at(r - 1, c.col)) for c in cs]})
                for ri, rr in enumerate(same_page):
                    for ci, c in enumerate(rows[rr]):
                        b = view.boxes[c.nid]
                        hc = t.cell_at(r - 1, c.col)
                        label = _label_text(hc)
                        out.append({"kind": "row_cell", "mode": "container", "data_type": data_type_of(label), "page": b.page,
                                    "region": [round(b.x, 2), round(b.y, 2), round(b.x1, 2), round(b.y1, 2)], "label": label,
                                    "anchor": _anchor(view, hc, relation="above"), "repeat": {"group": gid, "row": ri, "col": ci, "new_row": False},
                                    "confidence": 0.85, "initial_text": "", "_src": c.nid})
                        grouped.add(c.nid)
            r = end + 1
        else:
            r = end + 1
    # --- cells that carry their own label and leave room for the entry ("성명(한글) ______" style statutory forms)
    for c in t.cells:
        if c.nid in grouped or c.protect or _cell_empty(c) or any(p.objects() for p in c.paras):
            continue
        texts = [p for p in c.paras if p.text.strip()]
        if len(texts) != 1:
            continue
        p, raw = texts[0], texts[0].text
        lab = norm(raw)
        cb, tb = view.boxes.get(c.nid), view.text_bbox(p.nid)
        if cb is None or tb is None or not (1 <= len(lab) <= 14) or re.search(r"[□☐_\[\]]|[.다]$|^[\d\W]+$|[:：]\s*$", raw.strip()):
            continue
        tw = tb[3] - tb[1]
        free_right, free_below = cb.x1 - tb[3], cb.y1 - tb[4]
        if tb[1] - cb.x <= 14 and (free_right >= max(60.0, 1.2 * tw) or (free_below >= 1.5 * (tb[4] - tb[2]) and cb.w >= 80 and free_right >= 30)):
            n0 = len(out)
            kind, _dt = _kind_for(raw, cb.w, cb.h)
            _inline_field(view, p.nid, raw, len(raw), len(raw), kind, raw.strip(), 0.8, out)
            if len(out) > n0:
                out[-1]["_cell_label"] = True
    # --- ordinary label/value cells
    for c in t.cells:
        if c.nid in grouped or not _cell_empty(c) or c.protect:
            continue
        b = view.boxes.get(c.nid)
        if b is None or b.w < 25 or b.h < 9 or b.extra.get("split_rows"):
            continue
        label_cell, relation, conf = None, "left", 0.0
        k = c.col - 1
        steps = 0
        while k >= 0 and steps < 3:
            n = t.cell_at(c.row, k)
            if n is None:
                break
            if not _cell_empty(n):
                label_cell, relation, conf = n, "left", (0.9 if steps == 0 else 0.7)
                break
            k = n.col - 1
            steps += 1
        if label_cell is None and c.row > 0:
            n = t.cell_at(c.row - 1, c.col)
            if n is not None and not _cell_empty(n):
                label_cell, relation, conf = n, "above", 0.8
        if label_cell is None:
            continue
        label = _label_text(label_cell)
        if len(norm(label)) > 30 or len(norm(label)) < 1:
            conf = min(conf, 0.6)             # long text in the "label" cell is more likely an instruction than a label
        kind, dt = _kind_for(label, b.w, b.h)
        a = _anchor(view, label_cell, relation=relation)
        if a is None:
            continue
        out.append({"kind": kind, "mode": "container", "data_type": dt, "page": b.page,
                    "region": [round(b.x, 2), round(b.y, 2), round(b.x1, 2), round(b.y1, 2)], "label": label, "anchor": a, "repeat": None,
                    "confidence": conf, "initial_text": "", "_src": c.nid})


def _span_region(view: DocView, nid: str, a: int, b: int) -> tuple | None:
    frs = view.para_frags.get(nid, [])
    rects = []
    for f in frs:
        lo, hi = max(a, f.start), min(max(b, a + 1), f.start + len(f.text))
        if a == b and f.start <= a <= f.start + len(f.text):
            x = f.x + f.xs[a - f.start]
            box = view.para_box[nid]
            rects.append((f.page, x, f.y, max(box.x1, x + 20), f.y1))
            break
        if hi > lo:
            rects.append((f.page, f.x + f.xs[lo - f.start], f.y, f.x + f.xs[hi - f.start], f.y1))
    if not rects:
        return None
    pg, y0 = rects[0][0], rects[0][2]
    line = [r for r in rects if r[0] == pg and abs(r[2] - y0) < 1.0]
    return pg, [round(min(r[1] for r in line), 2), round(y0, 2), round(max(r[3] for r in line), 2), round(max(r[4] for r in line), 2)]


def _inline_field(view, nid, raw, a, b, kind, label, conf, out, relation="inline") -> None:
    reg = _span_region(view, nid, a, b)
    if reg is None:
        return
    if not norm(raw[:a]) and not norm(raw[b:]):
        return
    tb = view.text_bbox(nid)
    same = [n for n in view.order if view.ntext.get(n) == view.ntext[nid]]
    out.append({"kind": kind, "mode": "inline", "data_type": data_type_of(label) if kind != "check" else "check", "page": reg[0], "region": reg[1],
                "label": label[:40], "anchor": {"text": "", "page": tb[0], "bbox": [round(v, 2) for v in tb[1:]], "relation": relation, "occurrence": same.index(nid),
                                                "n_occurrences": len(same), "container_bbox": None},
                "repeat": None, "confidence": conf, "initial_text": raw[a:b], "_span": (a, b), "_raw": raw, "_src": nid})


def _para_fields(view: DocView, p, out: list) -> None:
    raw = p.text
    if not raw.strip() or p.nid not in view.para_box or view.para_box[p.nid].extra.get("role") != "body":
        return
    for m in re.finditer(r"_{3,}", raw):
        label = re.split(r"[\s,;·]+", raw[:m.start()].strip())[-1] if raw[:m.start()].strip() else ""
        _inline_field(view, p.nid, raw, m.start(), m.end(), "text", label.rstrip(":： "), 0.85 if label else 0.6, out)
    for m in re.finditer(r"[□☐]", raw):
        label = re.split(r"\s{2,}|[□☐]", raw[m.end():].strip())[0] if raw[m.end():].strip() else ""
        _inline_field(view, p.nid, raw, m.start(), m.end(), "check", label.strip(), 0.85 if label.strip() else 0.6, out)
    for m in re.finditer(r"\[(\s{1,3})\]", raw):
        label = re.split(r"\s{2,}|\[", raw[m.end():].strip())[0] if raw[m.end():].strip() else ""
        _inline_field(view, p.nid, raw, m.start(1), m.end(1), "check", label.strip(), 0.85 if label.strip() else 0.6, out)
    for m in re.finditer(r"([가-힣A-Za-z][가-힣A-Za-z ()]{0,14})[:：]( {3,})(?=\S)", raw):
        _inline_field(view, p.nid, raw, m.start(2), m.end(2), "text", m.group(1).strip(), 0.8, out)
    m = re.search(r"([가-힣A-Za-z][가-힣A-Za-z ()]{0,14})[:：]\s*$", raw)
    if m and len(norm(raw)) <= 20:
        _inline_field(view, p.nid, raw, len(raw), len(raw), "text", m.group(1).strip(), 0.8, out)
    # click-here (누름틀) fields
    begins = [i for i in p.inlines if i.kind == "field_begin" and i.meta.get("type") == "CLICK_HERE"]
    ends = {i.meta.get("begin"): i.meta["start"] for i in p.inlines if i.kind == "field_end"}
    for i in begins:
        a, b = i.meta["start"], ends.get(i.meta.get("id"), i.meta["start"])
        md = re.search(r"Direction:wstring:\d+:(.*?)\s+HelpState", i.meta.get("command", ""))
        label = (md.group(1).strip() if md else "") or re.split(r"[\s,;·]+", raw[:a].strip())[-1].rstrip(":： ") if (md or raw[:a].strip()) else ""
        n0 = len(out)
        _inline_field(view, p.nid, raw, a, b, "text", label, 0.9, out)
        if len(out) > n0:
            out[-1]["_field_ctrl"] = i.meta.get("nid")
    # signature / stamp place: blank area left of "(서명 또는 인)" / "(인)"
    for m in re.finditer(r"\((?:서명 또는 인|서명 또는 날인|인|서명)\)", raw):
        k = m.start()
        j = k
        while j > 0 and raw[j - 1].isspace():
            j -= 1
        reg_end = _span_region(view, p.nid, k, k + 1)
        reg_start = _span_region(view, p.nid, j, j + 1) if j < k else None
        if reg_end and reg_start and reg_end[0] == reg_start[0] and reg_end[1][0] - reg_start[1][0] >= 50:
            x1 = reg_end[1][0] - 3
            y = reg_end[1][1]
            tb = view.text_bbox(p.nid)
            same = [n for n in view.order if view.ntext.get(n) == view.ntext[p.nid]]
            own_cell = view.owner(p.nid)
            cb = view.boxes.get(own_cell) if own_cell else None
            out.append({"kind": "image", "mode": "container", "data_type": "signature", "page": reg_end[0],
                        "region": [round(x1 - 46, 2), round(y - 5, 2), round(x1, 2), round(y + 15, 2)], "label": "서명",
                        "anchor": {"text": raw.strip()[:60], "page": tb[0], "bbox": [round(v, 2) for v in tb[1:]], "relation": "inline",
                                   "occurrence": same.index(p.nid), "n_occurrences": len(same),
                                   "container_bbox": [round(cb.x, 2), round(cb.y, 2), round(cb.x1, 2), round(cb.y1, 2)] if cb is not None and cb.kind == "cell" else None},
                        "repeat": None, "confidence": 0.8, "initial_text": "", "_src": p.nid})
            break


def _form_controls(view: DocView, out: list) -> None:
    for b in view.layout.boxes:
        if b.kind == "form" and b.extra.get("form") in ("checkBtn", "radioBtn"):
            s = view.ir.nodes.get(b.nid)
            out.append({"kind": "check", "mode": "container", "data_type": "check", "page": b.page,
                        "region": [round(b.x, 2), round(b.y, 2), round(b.x1, 2), round(b.y1, 2)], "label": (s.props.get("caption") if s else "") or "",
                        "anchor": None, "repeat": None, "confidence": 0.9, "initial_text": "", "control": b.extra.get("form"), "_src": b.nid})


def extract_fields(view: DocView) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (fields, repeat_groups, review_queue). Fields are numbered in reading order."""
    raw: list[dict] = []
    groups: list[dict] = []
    for t in view.ir.tables():
        _table_fields(view, t, raw, groups)
    for p in view.ir.paras():
        _para_fields(view, p, raw)
    _form_controls(view, raw)
    raw.sort(key=lambda f: (f["page"], round(f["region"][1], 0), f["region"][0], f["kind"]))
    # drop container fields that collide with an earlier one (nested tables etc.)
    kept: list[dict] = []
    for f in raw:
        if f["mode"] == "container" and any(g["mode"] == "container" and g["page"] == f["page"] and _iou(g["region"], f["region"]) > 0.5 for g in kept):
            continue
        kept.append(f)
    # inline fields: keep non-overlapping spans per paragraph (higher confidence wins)
    by_para: dict[str, list] = {}
    for f in kept:
        if f["mode"] == "inline":
            by_para.setdefault(f["_src"], []).append(f)
    dropped = set()
    for fl in by_para.values():
        chosen: list = []
        for f in sorted(fl, key=lambda f: (-f["confidence"], f["_span"][0])):
            if all((f["_span"][1] <= g["_span"][0] or f["_span"][0] >= g["_span"][1]) and f["_span"] != g["_span"] for g in chosen):
                chosen.append(f)
            else:
                dropped.add(id(f))
    kept = [f for f in kept if id(f) not in dropped]
    fields, review = [], []
    for i, f in enumerate(kept):
        f["field_id"] = f"f{i + 1:03d}"
        if f["anchor"] is None:
            f["anchor"] = {"text": "", "page": f["page"], "bbox": f["region"], "relation": "none", "occurrence": 0, "n_occurrences": 0, "container_bbox": None}
        (fields if f["confidence"] >= MIN_CONFIDENCE else review).append(f)
    # paragraph pattern: literal text between the scored inline fields of the same paragraph (whitespace-insensitive)
    by_para = {}
    for f in fields:
        if f["mode"] == "inline":
            by_para.setdefault(f["_src"], []).append(f)
    for fl in by_para.values():
        fl.sort(key=lambda f: f["_span"][0])
        raw, segs, last = fl[0]["_raw"], [], 0
        for f in fl:
            segs.append(norm(raw[last:f["_span"][0]]))
            last = f["_span"][1]
        segs.append(norm(raw[last:]))
        ambiguous = any(not sg for sg in segs[1:-1])
        for k, f in enumerate(fl):
            f["para_pattern"], f["slot"] = segs, k
            if f["_span"][0] == f["_span"][1]:
                j = f["_span"][0]
                while j > 0 and raw[j - 1].isspace():
                    j -= 1
                f["gap_lead"] = f["_span"][0] - j
            if ambiguous:
                f["confidence"] = 0.5
    from .verify import Verifier
    for fl in by_para.values():
        segs = fl[0]["para_pattern"]
        matches = [n for n in view.order if view.para_box[n].extra.get("role") in ("body", None) and Verifier.match_pattern(view.text[n], segs) is not None]
        for f in fl:
            f["anchor"]["occurrence"] = matches.index(f["_src"]) if f["_src"] in matches else 0
            f["anchor"]["n_occurrences"] = len(matches)
            if f["_src"] not in matches:
                f["confidence"] = 0.5
    review += [f for f in fields if f["confidence"] < MIN_CONFIDENCE]
    fields = [f for f in fields if f["confidence"] >= MIN_CONFIDENCE]
    return fields, groups, review


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _public(f: dict) -> dict:
    return {k: v for k, v in f.items() if not k.startswith("_")}


def build_contracts(template_id: str, view: DocView, pair_type: str, seed: int, extra_rows: int = 2, long_chars: int = 220,
                    verified: bool = True, verification_method: str = "rule_extraction_v1+template_replay") -> tuple[list[dict], dict]:
    """One contract per applicable writing scenario. Scenarios that the form does not offer are not forced."""
    fields, groups, review = extract_fields(view)
    persona = persona_for(template_id, seed)
    pages = [[round(w, 2), round(h, 2)] for w, h in view.layout.pages]
    base = {"schema_version": SCHEMA_VERSION, "template_id": template_id, "pair_type": pair_type, "allowed_reflow": True,
            "annotations_verified": verified, "page_sizes": pages, "min_font_pt": 7.0,
            "verification": {"method": verification_method, "extractor_version": EXTRACTOR_VERSION, "generator_version": GENERATOR_VERSION},
            "values_provenance": {"persona_id": persona["persona_id"], "seed": seed, "synthetic": True},
            "protected_predicates": DEFAULT_PROTECTED}
    contracts = []

    def mk(scn: str, flds: list[dict], preds: list[dict], grps: list[dict] | None = None, extra_fields: list[dict] | None = None) -> None:
        if not preds:
            return
        all_fields = [_public(f) for f in fields] + [_public(f) for f in (extra_fields or [])]
        contracts.append({**base, "task_id": f"{template_id}--{scn}", "scenario": scn, "fields": all_fields, "repeat_groups": grps if grps is not None else groups,
                          "target_predicates": preds})

    short = [f for f in fields if f["kind"] in ("text", "date", "number")][:MAX_FIELDS_PER_SCENARIO]
    mk("short", short, [{"type": "field_text_equals", "field_id": f["field_id"], "value": value_for(f, persona, seed)} for f in short])
    longs = [f for f in fields if f["kind"] == "long_text"][:5]
    mk("long", longs, [{"type": "field_text_equals", "field_id": f["field_id"], "value": value_for(f, persona, seed, long_chars)} for f in longs])
    checks = [f for f in fields if f["kind"] == "check"]
    chosen, seen_src = [], set()
    for f in checks:                                  # one box per paragraph / choice group
        if f["_src"] not in seen_src:
            seen_src.add(f["_src"])
            chosen.append(f)
    mk("check", chosen, [{"type": "field_checked", "field_id": f["field_id"], "value": True} for f in chosen[:MAX_FIELDS_PER_SCENARIO]])
    if groups:
        g = groups[0]
        gf = [f for f in fields if (f.get("repeat") or {}).get("group") == g["group_id"]]
        n_exist = g["existing_rows"]
        last = [f for f in gf if f["repeat"]["row"] == n_exist - 1]
        new_fields = []
        for k in range(1, extra_rows + 1):
            for f in last:
                nf = dict(f)
                nf["field_id"] = f"{f['field_id']}_r{n_exist - 1 + k}"
                nf["region"] = [f["region"][0], round(f["region"][1] + k * g["row_height"], 2), f["region"][2], round(f["region"][3] + k * g["row_height"], 2)]
                nf["repeat"] = {"group": g["group_id"], "row": n_exist - 1 + k, "col": f["repeat"]["col"], "new_row": True}
                new_fields.append(nf)
        row_fields = gf + new_fields
        preds = [{"type": "table_row_count_at_least", "table_id": g["group_id"], "value": n_exist + extra_rows}]
        preds += [{"type": "field_text_equals", "field_id": f["field_id"], "value": value_for({**f, "field_id": f["field_id"]}, persona, seed)} for f in row_fields]
        mk("rows", row_fields, preds, extra_fields=new_fields)
    imgs = [f for f in fields if f["kind"] == "image"][:1]
    mk("image", imgs, [{"type": "image_in_region", "field_id": f["field_id"], "asset": "signature"} for f in imgs])
    summary = {"n_fields": len(fields), "n_review": len(review), "n_groups": len(groups), "scenarios": [c["scenario"] for c in contracts],
               "kinds": {k: sum(1 for f in fields if f["kind"] == k) for k in sorted({f["kind"] for f in fields})},
               "review_queue": [{"field_id": f["field_id"], "label": f["label"], "kind": f["kind"], "confidence": f["confidence"], "region": f["region"],
                                 "page": f["page"]} for f in review]}
    return contracts, summary
