"""Experiment edit, analysis: what the editing time is spent on (paper §4.2).

Re-runs the edit block edit model (src/formbench/editing/cost.py, KLM seconds) on the same inputs as scripts/evaluate_cost.py and splits
the time of the chosen edit plan into edit tasks:
  table_fill  표 칸 채우기        cell text typed or changed (existing cells, new cells, cells of an added table)
  para_fill   문단 채우기         paragraph and text box text typed or changed, new paragraphs (Enter, caret)
  table_build 표 만들기와 구조 변경 table insert dialog, rows/columns, merge/split, column width, row height, borders
  para_format 문단 서식           alignment, paragraph dialog (indent, spacing, line spacing), table indent,
                                 base paragraph format set once, batch-format saving (negative)
  char_format 글자 서식           font, size, bold/italic/underline, re-applying formats of retyped runs, base character format
  objects     글상자와 그림        text box / picture insertion, moving, resizing, moving to another page
  delete      삭제               deleting blocks (a run of flow blocks counts once)
  page        쪽 설정             page size, margins, orientation, columns
Every operation of the plan is decomposed with the same formulas as cost.py; the script asserts that the parts add up to
the operation's cost and that the document total equals document_cost() (so the split is exact, not an estimate).
Output: reports/experiments/main/edit_tasks.json (+ edit_tasks_rows.csv). Resumable per task in data/runs/edit-tasks/ (-2: before the ODT-direct GPT-6-Luna and Docling labels).
Also splits the time by operation (edit = keep/change incl. base format and batch saving, add, delete, page setup).
The recomputed totals match the stored main costs except 48 of 2,568 records (14 tasks) stored before the last cost.py
edit; the per-label ratio medians are identical to three decimals.
Usage: FORMBENCH_COST_UNITS=klm python scripts/edit_breakdown.py [--workers N] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import difflib
import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("FORMBENCH_COST_UNITS", "klm")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from formbench.util import obs_name, path, write_json  # noqa: E402

CATS = ["table_fill", "para_fill", "table_build", "para_format", "char_format", "objects", "delete", "page"]
LABELS = ["original", "oracle__template_odt", "vector_pdf__word_pdf", "vector_pdf__hwp_pdf", "png_150__gpt6luna_odt",
          "vector_pdf__docling_odt", "vector_pdf__mineru", "png_150__image_only"]
EPS = 1e-6


# ------------------------------------------------------------------------------------------ decomposition (mirrors cost.py)
def _add(acc, cat, v):
    if v:
        acc[cat] += v


def text_parts(d_raw, e_raw, e_runs, text_cat, acc):
    """cost.text_edit split into typing/selection (text_cat) and re-applied run formats (char_format)."""
    from formbench.editing import cost as K
    from formbench.editing.blocks import norm
    a, b = K._nf(d_raw), K._nf(e_raw)
    if norm(a) == norm(b):
        return 0.0
    ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != "equal"]
    if not ops:
        return 0.0
    t, cf = K.C()["text"], K.C()["char_format"]
    bounds = []
    if e_runs:
        off = 0
        for txt, _ in e_runs:
            off += len(K._nf(txt)) + 1
            bounds.append(off)

    def runs_in(j1, j2):
        return sum(1 for x in bounds if j1 < x < j2)

    def group(g0, g1):
        j1, j2 = ops[g0][3], ops[g1 - 1][4]
        if all(o[0] == "delete" for o in ops[g0:g1]):
            return t["select_range"] + t["delete"], 0.0
        return t["select_range"] + K.typed(b[j1:j2]), (cf["select_range"] + cf["bold"]) * runs_in(j1, j2)
    k = len(ops)
    best = [(0.0, 0.0, 0.0)] + [(K.INF, 0.0, 0.0)] * k          # (total, text, format), first minimum as in cost.py
    for i in range(1, k + 1):
        for j in range(i):
            tx, fm = group(j, i)
            cand = (best[j][0] + tx + fm, best[j][1] + tx, best[j][2] + fm)
            if cand[0] < best[i][0]:
                best[i] = cand
    _add(acc, text_cat, best[k][1])
    _add(acc, "char_format", best[k][2])
    return best[k][0]


def para_format_parts(d_ps, e_ps, acc, tol=1.0):
    from formbench.editing import cost as K
    v = K.para_format(d_ps, e_ps, tol)
    _add(acc, "para_format", v)
    return v


def char_format_parts(d_runs, e_runs, acc):
    from formbench.editing import cost as K
    v = K.char_format(d_runs, e_runs)
    _add(acc, "char_format", v)
    return v


def para_edit_parts(d, e, off, acc):
    from formbench.editing import cost as K
    tc = text_parts(d[0], e[0], e[2], "para_fill", acc)
    f = K.C()["para_format"]
    fc = f["align"] if d[1] and e[1] and d[1]["align"] != e[1]["align"] else 0
    _add(acc, "para_format", fc)
    fmt_cat = "para_format"
    if off:
        pf = K.para_format({**d[1], "align": e[1]["align"]} if d[1] and e[1] else d[1], e[1])
        cf = K.char_format(d[2], e[2])
        attrs = pf + cf
        if attrs:
            _add(acc, "para_format", pf)
            _add(acc, "char_format", cf)
            if cf and not pf and not fc:
                fmt_cat = "char_format"
        else:
            attrs = f["dialog_open"] + f["dialog_field"] + f["dialog_ok"]
            _add(acc, "para_format", attrs)
        fc += attrs
    caret = K.C()["text"]["place_caret"] if fc and not tc else 0
    _add(acc, fmt_cat, caret)
    return tc + fc + caret


def para_author_parts(e, base_ps, base_run, acc):
    from formbench.editing import cost as K
    t = K.C()["text"]
    cost = t["newline"] + (K.typed(e[0]) if e[0].strip() else 0)
    _add(acc, "para_fill", cost)
    if base_ps:
        cost += para_format_parts(base_ps, e[1], acc)
    runs = e[2]
    if runs and base_run is not None and K._dominant(runs) != base_run:
        cost += char_format_parts([("x", base_run)], runs, acc)
    cf = K.C()["char_format"]
    extra = (cf["select_range"] + cf["bold"]) * max(0, len({k for _, k in runs}) - 1)
    _add(acc, "char_format", extra)
    return cost + extra


def move_parts(d, e, tol, acc):
    from formbench.editing import cost as K
    v = K._move(d, e, tol)
    _add(acc, "objects", v)
    return v


def table_edit_parts(d, e, tol, acc):
    from formbench.editing import cost as K
    T = K.C()["table"]
    gd, ge = d.grid, e.grid
    cost = abs(gd["rows"] - ge["rows"]) * T["insert_row"] + abs(gd["cols"] - ge["cols"]) * T["insert_col"]
    sd = {k: (v[0], v[1]) for k, v in gd["cells"].items()}
    se = {k: (v[0], v[1]) for k, v in ge["cells"].items()}
    cost += sum(T["merge"] for k, v in se.items() if v != (1, 1) and sd.get(k) != v)
    cost += sum(T["split"] for k, v in sd.items() if v != (1, 1) and se.get(k) != v)
    _add(acc, "table_build", cost)
    for k, ev in ge["cells"].items():
        dv = gd["cells"].get(k)
        e_raw = "\n".join(p[0] for p in ev[3])
        if dv is None:
            v = (T["cell_nav"] + K.typed(e_raw)) if ev[2] else 0
            _add(acc, "table_fill", v)
            cost += v
            continue
        d_raw = "\n".join(p[0] for p in dv[3])
        tc = text_parts(d_raw, e_raw, [r for p in ev[3] for r in p[2]], "table_fill", acc)
        fc = K.C()["para_format"]["align"] if dv[3] and ev[3] and dv[3][0][1]["align"] != ev[3][0][1]["align"] else 0
        nav = T["cell_nav"] if fc and not tc else 0
        _add(acc, "para_format", fc + nav)
        cost += tc + fc + nav
    for idx, w in ge["col_w"].items():
        if idx in gd["col_w"] and abs(gd["col_w"][idx] - w) > tol:
            cost += T["col_width"]
            _add(acc, "table_build", T["col_width"])
    same = {r for r in range(ge["rows"]) if all(gd["cells"].get(k, (0, 0, None))[2] == v[2] for k, v in ge["cells"].items() if k[0] == r)}
    for idx, h in ge["row_h"].items():
        if idx in same and idx in gd["row_h"] and abs(gd["row_h"][idx] - h) > tol:
            cost += T["row_height"]
            _add(acc, "table_build", T["row_height"])
    if abs(d.x - e.x) > tol:
        pf = K.C()["para_format"]
        v = pf["dialog_open"] + pf["dialog_field"] + pf["dialog_ok"]
        cost += v
        _add(acc, "para_format", v)
    return cost


def table_author_parts(e, tol, acc):
    from formbench.editing import cost as K
    T = K.C()["table"]
    g = e.grid
    build = T["insert_dialog"] + sum(T["merge"] for v in g["cells"].values() if (v[0], v[1]) != (1, 1))
    if g["col_w"]:
        m = statistics.mean(g["col_w"].values())
        build += sum(T["col_width"] for w in g["col_w"].values() if abs(w - m) > tol)
    if g["row_h"]:
        m = statistics.median(g["row_h"].values())
        build += sum(T["row_height"] for h in g["row_h"].values() if abs(h - m) > tol)
    aligns = [v[3][0][1]["align"] for v in g["cells"].values() if v[3]]
    dom = max(set(aligns), key=aligns.count) if aligns else None
    fill = fmt = 0.0
    for v in g["cells"].values():
        paras = [p for p in v[3] if p[0].strip()]
        if paras:
            fill += T["cell_nav"] + sum(K.typed(p[0]) for p in paras) + (len(paras) - 1) * K.C()["text"]["newline"]
        if v[3] and dom and v[3][0][1]["align"] != dom:
            fmt += K.C()["para_format"]["align"] + (0 if paras else T["cell_nav"])
    build += 3 * max(0, g.get("borders", 1) - 1)
    _add(acc, "table_build", build)
    _add(acc, "table_fill", fill)
    _add(acc, "para_format", fmt)
    return build + fill + fmt


def sub_parts(d, e, tol, acc):
    from formbench.editing import cost as K
    if d.kind == "P":
        return para_edit_parts((d.raw, d.ps, d.runs), (e.raw, e.ps, e.runs), K.geometry_off(d, e, tol), acc)
    if d.kind == "T":
        return table_edit_parts(d, e, tol, acc)
    if d.kind == "B":
        dr, er = "\n".join(p[0] for p in d.paras), "\n".join(p[0] for p in e.paras)
        return text_parts(dr, er, [r for p in e.paras for r in p[2]], "para_fill", acc) + move_parts(d, e, tol, acc)
    return move_parts(d, e, tol, acc)


def author_parts(e, base_ps, base_run, tol, acc):
    from formbench.editing import cost as K
    if e.kind == "P":
        return para_author_parts((e.raw, e.ps, e.runs), base_ps, base_run, acc)
    if e.kind == "T":
        return table_author_parts(e, tol, acc)
    if e.kind == "B":
        t = K.C()["text"]
        create = K.C()["object"]["textbox_create"]
        text = sum(K.typed(p[0]) + t["newline"] for p in e.paras if p[0].strip())
        _add(acc, "objects", create)
        _add(acc, "para_fill", text)
        return create + text
    v = K.C()["object"]["image_insert"]
    _add(acc, "objects", v)
    return v


def zone_parts(E, D, tol, acc, by_op):
    """align() + batch saving of one zone, decomposed. Returns the zone cost as document_cost() counts it."""
    from formbench.editing import cost as K
    r = K.align(E, D, tol)
    eb, db = {b.nid: b for b in E}, {b.nid: b for b in D}
    if r.get("base_format") == "example (set once)":
        base_ps, base_run = K._base(E)
        d_ps, d_run = K._base(D)
        cf = K.C()["char_format"]
        pf = K.para_format(d_ps, base_ps)
        ch = K.char_format([("x", d_run)], [("x", base_run)]) if d_run and base_run else 0
        _add(acc, "para_format", cf["select_range"] + pf)
        _add(acc, "char_format", ch)
        setup = cf["select_range"] + pf + ch
        by_op["edit"] += setup
        if abs(setup - r["base_setup_cost"]) > EPS:
            raise AssertionError(f"base setup {setup} != {r['base_setup_cost']}")
    else:
        d_ps, d_run = K._base(D)
        e_ps, e_run = K._base(E)
        base_ps, base_run = (d_ps, d_run) if d_ps else (e_ps, e_run)
        setup = 0.0
    caret = K.C()["text"]["place_caret"]
    total, prev = setup, "S"
    for op, kind, e_nid, d_nid, c in r["ops"]:
        part = defaultdict(float)
        if op in ("keep", "edit"):
            v = sub_parts(db[d_nid], eb[e_nid], tol, part)
            prev = "S"
        elif op == "author":
            v = author_parts(eb[e_nid], base_ps, base_run, tol, part)
            if kind == "P" and prev != "A":
                v += caret
                part["para_fill"] += caret
            prev = "A"
        else:
            dc, flow = K.delete_cost(db[d_nid])
            v = 0.0 if (flow and prev == "X") else dc
            part["delete"] += v
            prev = "X"
        if op != "delete" and abs(v - c) > EPS and not (op == "author" and kind == "P" and abs(v - caret - c) < EPS):
            raise AssertionError(f"{op} {kind}: parts {v} != op cost {c}")
        if abs(sum(part.values()) - v) > EPS:
            raise AssertionError(f"{op} {kind}: categories {sum(part.values())} != {v}")
        for k2, x in part.items():
            acc[k2] += x
        by_op[{"keep": "edit", "edit": "edit", "author": "add", "delete": "delete"}[op]] += v
        total += v
    if abs(total - r["cost"]) > 1e-4:
        raise AssertionError(f"zone total {total} != align cost {r['cost']}")
    sv = K.batch_format_saving(r["ops"], E, D, tol)
    acc["para_format"] -= sv
    by_op["edit"] -= sv
    return total - sv


def doc_parts(d_sess, e_sess, lang):
    from formbench.editing import cost as K
    from formbench.editing.blocks import extract, page_setup
    if lang:
        K._LANG["lang"] = lang
    tol = float(K.C()["layout_tolerance_pt"])
    dz, ez = extract(d_sess, None), extract(e_sess, None)
    acc, by_op = defaultdict(float), defaultdict(float)
    total = sum(zone_parts(ez[z], dz[z], tol, acc, by_op) for z in ("body", "header", "footer"))
    pc = K.page_cost(page_setup(d_sess), page_setup(e_sess))
    acc["page"] += pc
    by_op["page"] += pc
    total += pc
    ref = K.document_cost(d_sess, e_sess, lang=lang)["cost"]
    if abs(total - ref) > 1e-4 or abs(sum(acc.values()) - ref) > 1e-4:
        raise AssertionError(f"document {total} / {sum(acc.values())} != document_cost {ref}")
    if abs(sum(by_op.values()) - ref) > 1e-4:
        raise AssertionError(f"by operation {sum(by_op.values())} != document_cost {ref}")
    return dict(acc), dict(by_op), ref


# ------------------------------------------------------------------------------------------ per task (inputs as evaluate_cost.py)
def task_job(args):
    cpath, out_dir, source_run = args
    from formbench.collection.discovery import blob_path
    from formbench.completion.sessions import open_session
    from formbench.util import DB
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from evaluate_cost import empty_odt
    c = json.load(open(cpath))
    tid = c["task_id"].replace("~", "__")
    dst = Path(out_dir) / f"{tid}.json"
    if dst.exists():
        return json.loads(dst.read_text())
    d = Path(path("data", obs_name(), tid))
    db = DB()
    lang = c.get("language")
    current = (d / "template.odt").exists()       # current (experiment): original and example preprocessed to ODT
    e_native = None if current else open_session(blob_path(db.get_doc(c["completed_id"])["sha256"]).read_bytes(), c["format"])
    e_odt = open_session((d / "example.odt").read_bytes(), "odt")
    stored = Path(path("data", "runs", source_run))
    rec = {"task_id": c["task_id"], "format": c["format"], "language": lang, "labels": {}}
    for lab in LABELS:
        if not (stored / lab).exists():           # label not in this run (current has no MinerU2.5)
            continue
        if lab == "original" and current:
            parts, by_op, cost = doc_parts(open_session((d / "template.odt").read_bytes(), "odt"), e_odt, lang)
        elif lab == "original":
            ds = open_session(blob_path(db.get_doc(c["template_id"])["sha256"]).read_bytes(), c["format"])
            parts, by_op, cost = doc_parts(ds, e_native, lang)
        else:
            f = d / "recon" / f"{lab}.odt"
            ds = open_session(f.read_bytes() if f.exists() else empty_odt(), "odt")
            parts, by_op, cost = doc_parts(ds, e_odt, lang)
        ref = json.loads((stored / lab / f"{tid}.json").read_text())["cost"]
        rec["labels"][lab] = {"cost": cost, "stored_cost": ref, "parts": parts, "by_op": by_op}
    write_json(dst, rec)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source-run", default="main", help="cost run whose stored totals are checked (current: main)")
    ap.add_argument("--out-run", default="edit-tasks")
    a = ap.parse_args()
    out_dir = Path(path("data", "runs", a.out_run))
    out_dir.mkdir(parents=True, exist_ok=True)
    cs = sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json"))))
    if a.limit:
        cs = cs[:a.limit]
    recs, errors = [], []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(task_job, (c, str(out_dir), a.source_run)): c for c in cs}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                recs.append(fut.result())
            except Exception as e:  # noqa: BLE001
                errors.append({"contract": Path(futs[fut]).name, "error": f"{type(e).__name__}: {e}"[:300]})
            print(f"# {n}/{len(cs)} errors={len(errors)}", flush=True)
    summary = {"run": a.out_run, "source_run": a.source_run, "unit": "seconds", "tasks": len(recs), "errors": errors,
               "categories": CATS, "labels": {}}
    rows = []
    for lab in [x for x in LABELS if recs and all(x in r["labels"] for r in recs)]:
        tot, top = defaultdict(float), defaultdict(float)
        cost = stored = 0.0
        for r in recs:
            x = r["labels"][lab]
            cost += x["cost"]
            stored += x["stored_cost"]
            for k, v in x["parts"].items():
                tot[k] += v
            for k, v in x["by_op"].items():
                top[k] += v
            rows.append({"task_id": r["task_id"], "label": lab, "cost": round(x["cost"], 3),
                         **{k: round(x["parts"].get(k, 0.0), 3) for k in CATS}})
        summary["labels"][lab] = {"cost_sum": round(cost, 2), "stored_cost_sum": round(stored, 2),
                                  "max_task_abs_diff_vs_stored": round(max(abs(r["labels"][lab]["cost"] - r["labels"][lab]["stored_cost"]) for r in recs), 4),
                                  "seconds": {k: round(tot[k], 2) for k in CATS},
                                  "share_pct": {k: round(100 * tot[k] / cost, 2) for k in CATS},
                                  "by_operation_share_pct": {k: round(100 * top[k] / cost, 2) for k in ("edit", "add", "delete", "page")}}
    rep = Path(path("reports", "experiments", a.source_run))
    write_json(rep / "edit_tasks.json", summary)
    with open(rep / "edit_tasks_rows.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["task_id", "label", "cost", *CATS])
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({k: v["share_pct"] for k, v in summary["labels"].items()}, ensure_ascii=False, indent=1))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
