"""Task contracts from verified observed pairs (blank form, publisher's completed example).

The completed example is the oracle: it is read here, once, to derive the contract, and never given to a
reconstructor or a planner. Both documents are opened in their native format; their reference-layout views are
aligned container by container:

* tables (order + text similarity), rows inside matched tables (rows the example adds are row targets), cells of
  matched rows by grid column;
* body paragraphs and text boxes (monotone alignment by text similarity; an empty blank container may be matched in
  sequence, which is how empty input areas are recognised).

Inside a matched container the character difference of blank -> example gives the targets: ``insert`` (text added
at a position), ``replace`` (blank text such as ○○○ / ____ / □ replaced by the value), ``delete`` (blank text the
example removes). Example paragraphs without a blank counterpart become ``new_paragraph_after`` targets. Everything
that needs an action outside the admitted operator set (new text boxes / tables / images, edits that span
paragraphs, changed cell structure) is listed as an ``unsupported`` target with the reason -- reported, never
dropped. Every target carries its location in the blank's reference layout (region, anchor point), which is how a
plan on ANY document (original or reconstruction) finds where the value goes and how success is checked there."""
from __future__ import annotations

import difflib
import re
import unicodedata

from ..document_ir.schema import Shape
from ..util import jhash

EXTRACTION_VERSION = "obs-1.3"     # 1.1: token-level diff with white-space merging; in-order gap filling
VIEW_ONLY = 5_000_000
REVIEW_BELOW = 0.8          # share of the example's text in aligned containers below which a contract goes to review


def _n(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def _ws(s: str) -> str:
    return " ".join(_n(s).split())


def _native(nid: str) -> bool:
    try:
        return int(nid.split("n")[-1]) < VIEW_ONLY
    except ValueError:
        return True


def sim(a: str, b: str) -> float:
    """Similarity of a blank container text a and an example container text b: how much of a re-appears in b
    (labels survive filling), tempered by plain ratio; 0 when a is empty."""
    a, b = _ws(a), _ws(b)
    if not a:
        return 0.0
    if a == b:
        return 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    contain = sum(m.size for m in sm.get_matching_blocks()) / len(a)
    return 0.7 * contain + 0.3 * sm.ratio()


def align(A: list, B: list, score, gap: float = 0.0, min_score: float = 0.45, seq_bonus: float = 0.05) -> list[tuple]:
    """Monotone alignment maximising the summed score of matched pairs; a pair is matchable when its score is at
    least ``min_score`` or both sides are empty-ish (score() returns None to forbid). Returns (i, j) pairs."""
    n, m = len(A), len(B)
    S = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best, arg = -1e9, None
            if i > 0 and S[i - 1][j] + gap > best:
                best, arg = S[i - 1][j] + gap, "up"
            if j > 0 and S[i][j - 1] + gap > best:
                best, arg = S[i][j - 1] + gap, "left"
            if i > 0 and j > 0:
                sc = score(A[i - 1], B[j - 1])
                if sc is not None and (sc >= min_score or sc == 0.0):
                    v = S[i - 1][j - 1] + sc + seq_bonus
                    if v > best:
                        best, arg = v, "diag"
            S[i][j], back[i][j] = best, arg
    out, i, j = [], n, m
    while i > 0 or j > 0:
        a = back[i][j]
        if a == "diag":
            out.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif a == "up":
            i -= 1
        else:
            j -= 1
    return out[::-1]


def fill_gaps(pairs: list[tuple], n: int, m: int) -> list[tuple]:
    """Between consecutive matched pairs, unmatched blank / example items are paired in order (a placeholder
    paragraph the example rewrote entirely is still the same input area); surplus items stay unmatched."""
    out, pi, pj = [], -1, -1
    for (i, j) in pairs + [(n, m)]:
        gi, gj = list(range(pi + 1, i)), list(range(pj + 1, j))
        out += list(zip(gi, gj))
        if i < n and j < m:
            out.append((i, j))
        pi, pj = i, j
    return sorted(set(out))


TOKEN_RX = re.compile(r"\w+|\s+|[^\w\s]", re.U)


def token_opcodes(B: str, E: str) -> list[tuple]:
    """Diff of blank -> example on word / space / symbol tokens; non-equal blocks separated only by white space are
    merged, so one filled value is one target. Returns (op, i1, i2, j1, j2) in character offsets."""
    tb = [(m.start(), m.end()) for m in TOKEN_RX.finditer(B)]
    te = [(m.start(), m.end()) for m in TOKEN_RX.finditer(E)]
    sm = difflib.SequenceMatcher(None, [B[a:b] for a, b in tb], [E[a:b] for a, b in te], autojunk=False)
    ops = [list(o) for o in sm.get_opcodes()]
    merged: list = []
    for o in ops:
        if o[0] != "equal" and len(merged) >= 2 and merged[-1][0] == "equal" and merged[-2][0] != "equal":
            eq = merged[-1]
            if all(not B[tb[k][0]:tb[k][1]].strip() for k in range(eq[1], eq[2])):
                prev = merged.pop(-2)
                merged.pop()
                merged.append(["replace", prev[1], o[2], prev[3], o[4]])
                continue
        if o[0] != "equal" and merged and merged[-1][0] != "equal":
            prev = merged.pop()
            merged.append(["replace", prev[1], o[2], prev[3], o[4]])
            continue
        merged.append(o)

    def cb(k, toks, text_len):
        return toks[k][0] if k < len(toks) else text_len
    out = []
    for op, i1, i2, j1, j2 in merged:
        ci1, ci2 = cb(i1, tb, len(B)), cb(i2, tb, len(B))
        cj1, cj2 = cb(j1, te, len(E)), cb(j2, te, len(E))
        if op != "equal":                         # trim characters both sides share at the ends of the change
            while ci1 < ci2 and cj1 < cj2 and B[ci1] == E[cj1]:
                ci1, cj1 = ci1 + 1, cj1 + 1
            while ci1 < ci2 and cj1 < cj2 and B[ci2 - 1] == E[cj2 - 1]:
                ci2, cj2 = ci2 - 1, cj2 - 1
            if ci1 == ci2 and cj1 == cj2:
                continue
        if op == "replace" and ci1 == ci2:
            op = "insert"
        elif op == "replace" and cj1 == cj2:
            op = "delete"
        out.append((op, ci1, ci2, cj1, cj2))
    return out


# ----------------------------------------------------------------------------- containers of a view
def _para_box(lay, nid):
    return next((b for b in lay.boxes if b.kind == "para" and b.nid == nid), None)


def _box(lay, nid, kind):
    return next((b for b in lay.boxes if b.kind == kind and b.nid == nid), None)


def char_point(lay, para_nid: str, offset: int):
    """(page, x, y) of the caret position before character ``offset`` of a paragraph in the layout."""
    frs = [f for f in lay.frags if f.para == para_nid and not f.generated]
    for f in frs:
        if f.start <= offset <= f.start + len(f.text):
            i = offset - f.start
            return (f.page, round(f.x + f.xs[min(i, len(f.xs) - 1)], 2), round(f.y + f.h / 2, 2))
    if frs:
        f = frs[-1] if offset > frs[-1].start else frs[0]
        return (f.page, round(f.x1 if offset > f.start else f.x, 2), round(f.y + f.h / 2, 2))
    b = _para_box(lay, para_nid)
    if b is None:
        return None
    return (b.page, round(b.x + min(3.0, b.w / 2), 2), round(b.y + min(b.h / 2, 6.0), 2))


def _region(b):
    return None if b is None else {"page": b.page, "x": round(b.x, 2), "y": round(b.y, 2), "w": round(b.w, 2), "h": round(b.h, 2)}


def containers(sess) -> dict:
    ir, lay = sess.ir, sess.layout
    body, tables, boxes = [], [], []
    for sec in ir.sections:
        for p in sec.paras:
            if _native(p.nid):
                body.append({"kind": "body", "paras": [p.nid], "texts": [p.text], "text": p.text,
                             "region": _region(_para_box(lay, p.nid))})
    for ti, t in enumerate(ir.tables()):
        rows: dict[int, list] = {}
        for c in sorted(t.cells, key=lambda c: (c.row, c.col)):
            rows.setdefault(c.row, []).append({"kind": "cell", "table": ti, "row": c.row, "col": c.col, "colspan": c.colspan,
                                               "rowspan": c.rowspan, "nid": c.nid, "paras": [p.nid for p in c.paras],
                                               "texts": [p.text for p in c.paras], "text": "\n".join(p.text for p in c.paras),
                                               "region": _region(_box(lay, c.nid, "cell"))})
        tables.append({"index": ti, "nid": t.nid, "rows": [rows[r] for r in sorted(rows)],
                       "text": " ".join(c["text"] for r in sorted(rows) for c in rows[r]), "region": _region(_box(lay, t.nid, "table"))})
    for s in ir.shapes():
        if isinstance(s, Shape) and s.is_textbox and _native(s.nid):
            b = _box(lay, s.nid, "textbox")
            boxes.append({"kind": "textbox", "nid": s.nid, "paras": [p.nid for p in s.paras], "texts": [p.text for p in s.paras],
                          "text": "\n".join(p.text for p in s.paras), "region": _region(b),
                          "pos": (b.page, b.y, b.x) if b else (0, 0, 0)})
    boxes.sort(key=lambda b: b["pos"])
    pics = sum(1 for s in ir.shapes() if s.kind == "pic")
    return {"body": body, "tables": tables, "textboxes": boxes, "pictures": pics}


# ----------------------------------------------------------------------------- targets
def _offset_to_para(cont: dict, off: int) -> tuple[int, int]:
    """Container text offset -> (paragraph index, offset in paragraph); a position at a paragraph boundary belongs
    to the end of the earlier paragraph."""
    pos = 0
    for i, t in enumerate(cont["texts"]):
        if off <= pos + len(t):
            return i, off - pos
        pos += len(t) + 1
    return len(cont["texts"]) - 1, len(cont["texts"][-1])


def container_targets(cont: dict, ex_text: str, lay, where: dict, next_id) -> tuple[list, list]:
    B, E = _n(cont["text"]), _n(ex_text)
    if B == E or _ws(B) == _ws(E):
        return [], []
    targets, unsupported = [], []
    for op, i1, i2, j1, j2 in token_opcodes(B, E):
        if op == "equal":
            continue
        old, val = B[i1:i2], E[j1:j2]
        if not old.strip() and not val.strip():
            continue                                   # white-space only difference
        kind = "insert" if i1 == i2 else ("delete" if j1 == j2 else "replace")
        pa, oa = _offset_to_para(cont, i1)
        pb, ob = _offset_to_para(cont, i2)
        t = {"id": next_id(), "kind": kind, "old": old, "value": val, "container": dict(where, kind=cont["kind"]),
             "expected_container_text": E, "region": cont["region"]}
        if "\n" in old or pa != pb:
            t["reason"] = "edit spans paragraphs (selection across paragraphs is not admitted)"
            unsupported.append(t)
            continue
        para = cont["paras"][pa]
        t["para"], t["range"] = para, [oa, ob]
        t["anchor"] = char_point(lay, para, oa)
        t["anchor_end"] = char_point(lay, para, ob) if kind != "insert" else None
        if t["anchor"] is None:
            t["reason"] = "container not visible in the reference layout"
            unsupported.append(t)
            continue
        targets.append(t)
    return targets, unsupported


def build_observed_contract(pair_id: str, blank_sess, example_sess, meta: dict) -> dict:
    cb, ce = containers(blank_sess), containers(example_sess)
    lay = blank_sess.layout
    counter = iter(range(1, 1_000_000))

    def next_id():
        return f"t{next(counter):03d}"
    targets, unsupported, rows = [], [], []
    ex_total = sum(len(_ws(c["text"])) for c in ce["body"] + ce["textboxes"]) + sum(len(_ws(t["text"])) for t in ce["tables"])
    aligned_chars = 0
    # ---- tables
    tpairs = align(cb["tables"], ce["tables"], lambda a, b: sim(a["text"], b["text"]) if a["text"].strip() else 0.0)
    matched_ex_tables = {j for _, j in tpairs}
    for i, j in tpairs:
        tb, te = cb["tables"][i], ce["tables"][j]
        aligned_chars += len(_ws(te["text"]))
        rsig = lambda r: " ".join(c["text"] for c in r)  # noqa: E731
        rpairs = align(tb["rows"], te["rows"], lambda a, b: sim(rsig(a), rsig(b)) if rsig(a).strip() else 0.0)
        rpairs = fill_gaps(rpairs, len(tb["rows"]), len(te["rows"])) if len(tb["rows"]) == len(te["rows"]) else rpairs
        matched_rows = {jj for _, jj in rpairs}
        for ri, rj in rpairs:
            rb, re_ = tb["rows"][ri], te["rows"][rj]
            if [(c["col"], c["colspan"]) for c in rb] != [(c["col"], c["colspan"]) for c in re_]:
                # the example's grid differs (another save of the same form): cells of the row aligned in order by text
                cp = align(rb, re_, lambda a, b: sim(a["text"], b["text"]) if a["text"].strip() else 0.0)
                cp = fill_gaps(cp, len(rb), len(re_)) if len(rb) == len(re_) else cp
                pairs = [(rb[a], re_[b]) for a, b in cp]
                for b_ in sorted(set(range(len(re_))) - {b for _, b in cp}):
                    if _ws(re_[b_]["text"]) and not any(_ws(re_[b_]["text"]) == _ws(x["text"]) for x in rb):
                        unsupported.append({"id": next_id(), "kind": "cell_unmatched", "value": _n(re_[b_]["text"]),
                                            "region": rb[0]["region"], "container": {"table": i, "row": rb[0]["row"]},
                                            "reason": "an example cell has no counterpart in the blank row"})
            else:
                pairs = list(zip(rb, re_))
            for c_b, c_e in pairs:
                where = {"table": i, "row": c_b["row"], "col": c_b["col"], "cell": c_b["nid"]}
                tg, un = container_targets(c_b, c_e["text"], lay, where, next_id)
                targets += tg
                unsupported += un
        # rows the example adds (after a matched blank row)
        last_b = None
        added: list[tuple] = []
        for rj, re_ in enumerate(te["rows"]):
            m = next((ri for ri, jj in rpairs if jj == rj), None)
            if m is not None:
                last_b = m
            else:
                added.append((last_b, re_))
        groups: dict = {}
        for after, re_ in added:
            groups.setdefault(after, []).append(re_)
        for after, rs in groups.items():
            if after is None:
                unsupported.append({"id": next_id(), "kind": "row_insert_above_first", "region": tb["region"],
                                    "container": {"table": i}, "reason": "rows added above the first row"})
                continue
            ref_row = tb["rows"][after]
            row_t = {"id": next_id(), "kind": "rows_added", "table": i, "table_nid": tb["nid"], "after_row": ref_row[0]["row"],
                     "count": len(rs), "region": tb["region"], "cells": []}
            for k, r in enumerate(rs):
                for c in r:
                    if _ws(c["text"]):
                        col_ref = next((cb_ for cb_ in ref_row if cb_["col"] == c["col"]), None)
                        row_t["cells"].append({"id": next_id(), "new_row": k, "col": c["col"], "value": _n(c["text"]),
                                               "col_region": col_ref["region"] if col_ref else None})
            if any("\n" in x["value"] for x in row_t["cells"]):
                row_t["note"] = "multi-line cell values are typed with line breaks"
            rows.append(row_t)
        for ri in range(len(tb["rows"])):
            if ri not in {a for a, _ in rpairs} and any(_ws(c["text"]) for c in tb["rows"][ri]):
                unsupported.append({"id": next_id(), "kind": "row_removed", "region": tb["rows"][ri][0]["region"],
                                    "container": {"table": i, "row": ri}, "reason": "the example removes a row with text"})
    for j, te in enumerate(ce["tables"]):
        if j not in matched_ex_tables and _ws(te["text"]):
            unsupported.append({"id": next_id(), "kind": "table_added", "region": te["region"], "container": {"example_table": j},
                                "reason": "the example adds a table (creating a table is not admitted)"})
    # ---- text boxes
    bpairs = align(cb["textboxes"], ce["textboxes"], lambda a, b: sim(a["text"], b["text"]) if a["text"].strip() else 0.0)
    bpairs = fill_gaps(bpairs, len(cb["textboxes"]), len(ce["textboxes"]))
    for i, j in bpairs:
        aligned_chars += len(_ws(ce["textboxes"][j]["text"]))
        cbx = cb["textboxes"][i]
        tg, un = container_targets(cbx, ce["textboxes"][j]["text"], lay, {"textbox": cbx["nid"]}, next_id)
        targets += tg
        unsupported += un
    for j, bx in enumerate(ce["textboxes"]):
        if j not in {jj for _, jj in bpairs} and _ws(bx["text"]):
            unsupported.append({"id": next_id(), "kind": "textbox_added", "value": _n(bx["text"]), "region": bx["region"],
                                "container": {"example_textbox": j}, "reason": "the example adds a text box (not admitted)"})
    # ---- body paragraphs
    ppairs = align(cb["body"], ce["body"], lambda a, b: sim(a["text"], b["text"]) if a["text"].strip() else 0.0)
    ppairs = fill_gaps(ppairs, len(cb["body"]), len(ce["body"]))
    matched_b = {i for i, _ in ppairs}
    for i, j in ppairs:
        aligned_chars += len(_ws(ce["body"][j]["text"]))
        cp = cb["body"][i]
        tg, un = container_targets(cp, ce["body"][j]["text"], lay, {"body_para": cp["paras"][0]}, next_id)
        targets += tg
        unsupported += un
    last = None
    for j, pe in enumerate(ce["body"]):
        m = next((i for i, jj in ppairs if jj == j), None)
        if m is not None:
            last = m
            continue
        if not _ws(pe["text"]):
            continue
        if last is None:
            unsupported.append({"id": next_id(), "kind": "paragraph_added_at_start", "value": _n(pe["text"]), "region": None,
                                "reason": "a paragraph added before the first paragraph"})
            continue
        prev = cb["body"][last]
        anchor = char_point(lay, prev["paras"][0], len(prev["text"]))
        targets.append({"id": next_id(), "kind": "new_paragraph_after", "old": "", "value": _n(pe["text"]),
                        "container": {"kind": "body", "body_para": prev["paras"][0]}, "para": prev["paras"][0],
                        "range": [len(prev["text"]), len(prev["text"])], "anchor": anchor, "anchor_end": None,
                        "region": prev["region"], "expected_container_text": _n(pe["text"])})
        aligned_chars += len(_ws(pe["text"]))
    for i, pb in enumerate(cb["body"]):
        if i not in matched_b and _ws(pb["text"]):
            unsupported.append({"id": next_id(), "kind": "paragraph_removed", "old": _n(pb["text"]), "region": pb["region"],
                                "container": {"body_para": pb["paras"][0]}, "reason": "the example removes a paragraph"})
    if ce["pictures"] > cb["pictures"]:
        unsupported.append({"id": next_id(), "kind": "image_added", "count": ce["pictures"] - cb["pictures"], "region": None,
                            "reason": "the example adds pictures (photo / signature): inserting an image is not admitted"})
    confidence = round(aligned_chars / ex_total, 3) if ex_total else 0.0
    n_values = len(targets) + sum(len(r["cells"]) for r in rows)
    status = "ok" if n_values else "no_targets"
    if status == "ok" and confidence < REVIEW_BELOW:
        status = "needs_review"
    c = {"schema_version": "2.0", "contract_kind": "observed_pair_diff", "extraction_version": EXTRACTION_VERSION,
         "task_id": pair_id, **meta, "status": status, "alignment_confidence": confidence,
         "targets": targets, "rows": rows, "unsupported": unsupported,
         "stats": {"targets": len(targets), "row_groups": len(rows), "row_cells": sum(len(r["cells"]) for r in rows),
                   "unsupported": len(unsupported), "kinds": _count(t["kind"] for t in targets)}}
    c["contract_hash"] = jhash({k: v for k, v in c.items() if k != "contract_hash"})
    return c


def _count(it) -> dict:
    out: dict = {}
    for k in it:
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
