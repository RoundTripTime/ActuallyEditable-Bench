"""Experiment edit metric ②: human actions to turn a document D into the example E (block edit cost).

D and E are reading-order block sequences per zone (editing/blocks.py). An alignment keeps blocks (and fixes their text,
format, structure, position), authors E blocks D lacks, and deletes D blocks E lacks. Costs are human actions from
configs/costs_actions.yaml. The alignment is solved exactly by dynamic programming (Gotoh: a run of consecutive deleted
flow blocks costs one selection + Delete, a run of authored flow paragraphs one caret placement), so the result is
the minimum UNDER THIS BLOCK MODEL. Authoring is always possible, so the cost is always finite ("무조건 만든다")."""
from __future__ import annotations

import difflib
import statistics
import unicodedata
from functools import lru_cache

from ..util import load_yaml, path
from .blocks import Block, norm

COST_VERSION = "edit-cost-1.3"   # 1.1 geometry-gated format; 1.2 base format once; 1.3 batch format runs + KLM unit option
INF = 10 ** 9


_LANG = {"lang": "ja"}


@lru_cache(maxsize=1)
def C() -> dict:
    """Cost table: human actions (configs/costs_actions.yaml) or KLM seconds (FORMBENCH_COST_UNITS=klm,
    configs/costs_klm.yaml). Sensitivity runs only: FORMBENCH_COST_TOL overrides the layout tolerance;
    FORMBENCH_COST_TYPING_SCALE multiplies the keystroke time K (ratios are scale-free, so only the weight of typing relative
    to the other operators matters); FORMBENCH_COST_KPC_FLAT=1 counts every character as one keystroke (no input-method
    assumption)."""
    import os
    name = "costs_klm.yaml" if os.environ.get("FORMBENCH_COST_UNITS") == "klm" else "costs_actions.yaml"
    c = load_yaml(path("configs", name))
    if os.environ.get("FORMBENCH_COST_TOL"):
        c["layout_tolerance_pt"] = float(os.environ["FORMBENCH_COST_TOL"])
    if "klm" in c and os.environ.get("FORMBENCH_COST_TYPING_SCALE"):
        c["klm"]["K"] = c["klm"]["K"] * float(os.environ["FORMBENCH_COST_TYPING_SCALE"])
    if "keys_per_char" in c and os.environ.get("FORMBENCH_COST_KPC_FLAT") == "1":
        c["keys_per_char"] = {k: 1.0 for k in c["keys_per_char"]}
    return c


def keys(text: str) -> float:
    """Keystrokes to type text with the document language's input method (KLM mode)."""
    kp = C().get("keys_per_char", {})
    n = 0.0
    for ch in text:
        if ch.isspace():
            n += 1.0 if ch == " " else 0.0
            continue
        o = ord(ch)
        if o < 0x250:
            n += kp.get("latin", 1.0)
        elif 0xAC00 <= o <= 0xD7A3 or 0x3130 <= o <= 0x318F:
            n += kp.get("hangul", 2.3)
        elif 0x3040 <= o <= 0x30FF:
            n += kp.get("kana", 2.0)
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            n += kp.get("han_zh" if _LANG["lang"] == "zh" else "han_ja", 2.5)
        elif 0xFF01 <= o <= 0xFF5E:
            n += kp.get("fullwidth", 1.5)
        else:
            n += kp.get("other", 1.5)
    return n


def typed(text: str) -> float:
    """Cost of typing one contiguous string: one action (actions mode) or M + H + K x keystrokes (KLM mode)."""
    base = C()["text"]["type"]
    if C().get("unit") == "seconds":
        return base + C()["klm"]["K"] * keys(text)
    return base


def _nf(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s or "").split())


# ----------------------------------------------------------------------------------------------- text
def text_edit(d_raw: str, e_raw: str, e_runs: list | None = None) -> int:
    """Actions to turn the text d into e inside one container: diff regions are grouped (a group = select or click +
    type one string, or select + Delete); retyping across differently formatted runs of e costs re-formatting."""
    a, b = _nf(d_raw), _nf(e_raw)
    if norm(a) == norm(b):
        return 0
    ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != "equal"]
    if not ops:
        return 0
    t = C()["text"]
    bounds = []                                   # run boundaries of e (normalised offsets)
    if e_runs:
        off = 0
        for txt, _ in e_runs:
            off += len(_nf(txt)) + 1
            bounds.append(off)

    def runs_in(j1, j2):
        return sum(1 for x in bounds if j1 < x < j2)

    def group_cost(g0, g1):                       # regions g0..g1-1 handled by one selection
        j1, j2 = ops[g0][3], ops[g1 - 1][4]
        if all(o[0] == "delete" for o in ops[g0:g1]):
            return t["select_range"] + t["delete"]
        cf = C()["char_format"]
        return t["select_range"] + typed(b[j1:j2]) + (cf["select_range"] + cf["bold"]) * runs_in(j1, j2)
    k = len(ops)
    best = [0] + [INF] * k
    for i in range(1, k + 1):
        for j in range(i):
            best[i] = min(best[i], best[j] + group_cost(j, i))
    return best[k]


# ----------------------------------------------------------------------------------------------- format
def para_format(d_ps: dict, e_ps: dict, tol: float = 1.0) -> int:
    if not d_ps or not e_ps:
        return 0
    f = C()["para_format"]
    cost = f["align"] if d_ps["align"] != e_ps["align"] else 0
    n = sum(1 for k in ("left", "right", "indent", "prev", "next") if abs(d_ps[k] - e_ps[k]) > tol)
    if d_ps["ls"][0] != e_ps["ls"][0] or abs(d_ps["ls"][1] - e_ps["ls"][1]) > 5:
        n += 1
    if n:
        cost += f["dialog_open"] + n * f["dialog_field"] + f["dialog_ok"]
    return cost


def _dominant(runs: list) -> tuple | None:
    if not runs:
        return None
    w = {}
    for txt, key in runs:
        w[key] = w.get(key, 0) + len(txt)
    return max(w.items(), key=lambda kv: (kv[1], str(kv[0])))[0]


def char_format(d_runs: list, e_runs: list) -> int:
    a, b = _dominant(d_runs), _dominant(e_runs)
    if a is None or b is None or a == b:
        return 0
    f = C()["char_format"]
    n = 0
    n += f["size"] if abs(a[0] - b[0]) > 0.5 else 0
    n += f["bold"] if a[1] != b[1] else 0
    n += f["italic"] if a[2] != b[2] else 0
    n += f["underline"] if a[3] != b[3] else 0
    n += f["font"] if a[4] != b[4] else 0
    return f["select_range"] + n if n else 0


def geometry_off(d: Block, e: Block, tol: float) -> bool:
    """A kept flow block needs a layout fix when its own geometry differs by more than tol: horizontal position, height
    (line wrapping) or the gap to the previous flow block. Drift caused by earlier blocks is not charged again."""
    return abs(d.x - e.x) > tol or abs(d.h - e.h) > tol or abs(d.gap - e.gap) > tol


def para_edit(d: tuple, e: tuple, off: bool = True) -> int:
    """(raw, ps, runs) -> (raw, ps, runs). Format attributes are fixed only when the block's geometry is off (or the
    alignment differs, which moves the glyphs of every short line); a needed fix the attributes do not explain costs a
    paragraph-dialog change (open + one field + OK)."""
    tc = text_edit(d[0], e[0], e[2])
    f = C()["para_format"]
    fc = f["align"] if d[1] and e[1] and d[1]["align"] != e[1]["align"] else 0
    if off:
        attrs = para_format({**d[1], "align": e[1]["align"]} if d[1] and e[1] else d[1], e[1]) + char_format(d[2], e[2])
        fc += attrs or (f["dialog_open"] + f["dialog_field"] + f["dialog_ok"])
    return tc + fc + (C()["text"]["place_caret"] if fc and not tc else 0)


def para_author(e: tuple, base_ps: dict | None, base_run: tuple | None) -> int:
    t = C()["text"]
    cost = t["newline"] + (typed(e[0]) if e[0].strip() else 0)
    if base_ps:
        cost += para_format(base_ps, e[1])
    runs = e[2]
    if runs and base_run is not None and _dominant(runs) != base_run:
        cost += char_format([("x", base_run)], runs)
    cf = C()["char_format"]
    cost += (cf["select_range"] + cf["bold"]) * max(0, len({k for _, k in runs}) - 1)   # extra formatted runs: select + apply each
    return cost


# ----------------------------------------------------------------------------------------------- blocks
def _move(d: Block, e: Block, tol: float) -> int:
    o = C()["object"]
    if d.page != e.page:
        return 4                                             # select + cut + click on the page + paste
    cost = 0
    if abs(d.x - e.x) > tol or abs(d.y - e.y) > tol:
        cost += o["select"] + o["move"]
    if abs(d.w - e.w) > tol or abs(d.h - e.h) > tol:
        cost += o["resize"] + (0 if cost else o["select"])
    return cost


def table_edit(d: Block, e: Block, tol: float) -> int:
    T = C()["table"]
    gd, ge = d.grid, e.grid
    cost = abs(gd["rows"] - ge["rows"]) * T["insert_row"] + abs(gd["cols"] - ge["cols"]) * T["insert_col"]
    sd = {k: (v[0], v[1]) for k, v in gd["cells"].items()}
    se = {k: (v[0], v[1]) for k, v in ge["cells"].items()}
    cost += sum(T["merge"] for k, v in se.items() if v != (1, 1) and sd.get(k) != v)
    cost += sum(T["split"] for k, v in sd.items() if v != (1, 1) and se.get(k) != v)
    for k, ev in ge["cells"].items():
        dv = gd["cells"].get(k)
        e_raw = "\n".join(p[0] for p in ev[3])
        if dv is None:
            cost += (T["cell_nav"] + typed(e_raw)) if ev[2] else 0
            continue
        d_raw = "\n".join(p[0] for p in dv[3])
        tc = text_edit(d_raw, e_raw, [r for p in ev[3] for r in p[2]])
        fc = C()["para_format"]["align"] if dv[3] and ev[3] and dv[3][0][1]["align"] != ev[3][0][1]["align"] else 0
        cost += tc + fc + (T["cell_nav"] if fc and not tc else 0)
    for idx, w in ge["col_w"].items():
        if idx in gd["col_w"] and abs(gd["col_w"][idx] - w) > tol:
            cost += T["col_width"]
    same_text_rows = {r for r in range(ge["rows"]) if all(gd["cells"].get(k, (0, 0, None))[2] == v[2] for k, v in ge["cells"].items() if k[0] == r)}
    for idx, h in ge["row_h"].items():                    # rows whose text already matches but whose height differs
        if idx in same_text_rows and idx in gd["row_h"] and abs(gd["row_h"][idx] - h) > tol:
            cost += T["row_height"]
    if abs(d.x - e.x) > tol:                              # table indent / alignment
        cost += C()["para_format"]["dialog_open"] + C()["para_format"]["dialog_field"] + C()["para_format"]["dialog_ok"]
    return cost


def table_author(e: Block, tol: float) -> int:
    T = C()["table"]
    g = e.grid
    cost = T["insert_dialog"] + sum(T["merge"] for v in g["cells"].values() if (v[0], v[1]) != (1, 1))
    if g["col_w"]:
        m = statistics.mean(g["col_w"].values())
        cost += sum(T["col_width"] for w in g["col_w"].values() if abs(w - m) > tol)
    if g["row_h"]:
        m = statistics.median(g["row_h"].values())
        cost += sum(T["row_height"] for h in g["row_h"].values() if abs(h - m) > tol)
    aligns = [v[3][0][1]["align"] for v in g["cells"].values() if v[3]]
    dom = max(set(aligns), key=aligns.count) if aligns else None
    for v in g["cells"].values():
        paras = [p for p in v[3] if p[0].strip()]
        if paras:
            cost += T["cell_nav"] + sum(typed(p[0]) for p in paras) + (len(paras) - 1) * C()["text"]["newline"]
        if v[3] and dom and v[3][0][1]["align"] != dom:
            cost += C()["para_format"]["align"] + (0 if paras else T["cell_nav"])
    cost += 3 * max(0, g.get("borders", 1) - 1)              # border / shading dialog per extra cell style
    return cost


def sub_cost(d: Block, e: Block, tol: float) -> int:
    if d.kind != e.kind:
        return INF
    if d.kind == "P":
        if d.text and e.text and difflib.SequenceMatcher(None, d.text, e.text, autojunk=False).quick_ratio() < 0.2:
            return INF
        return para_edit((d.raw, d.ps, d.runs), (e.raw, e.ps, e.runs), geometry_off(d, e, tol))
    if d.kind == "T":
        return table_edit(d, e, tol)
    if d.kind == "B":
        dr, er = "\n".join(p[0] for p in d.paras), "\n".join(p[0] for p in e.paras)
        if d.text and e.text and difflib.SequenceMatcher(None, d.text, e.text, autojunk=False).quick_ratio() < 0.2:
            return INF
        tc = text_edit(dr, er, [r for p in e.paras for r in p[2]])
        return tc + _move(d, e, tol)
    if d.kind == "I":
        return _move(d, e, tol) if d.obj_kind == e.obj_kind else INF
    return INF


def author_cost(e: Block, base_ps, base_run, tol: float) -> int:
    if e.kind == "P":
        return para_author((e.raw, e.ps, e.runs), base_ps, base_run)
    if e.kind == "T":
        return table_author(e, tol)
    if e.kind == "B":
        t = C()["text"]
        return C()["object"]["textbox_create"] + sum(typed(p[0]) + t["newline"] for p in e.paras if p[0].strip())
    return C()["object"]["image_insert"]


def delete_cost(d: Block) -> tuple[int, bool]:
    """(cost, is_flow): flow blocks (P, T) are deleted in runs."""
    if d.kind in ("P", "T"):
        return C()["block"]["delete_run"], True
    return C()["object"]["select"] + C()["object"]["delete"], False


# ----------------------------------------------------------------------------------------------- alignment
def _base(blocks: list[Block]) -> tuple:
    ps_list = [b.ps for b in blocks if b.kind == "P" and b.ps]
    base_ps = max(ps_list, key=lambda p: (sum(1 for q in ps_list if q == p), str(p))) if ps_list else None
    return base_ps, _dominant([r for b in blocks if b.kind == "P" for r in b.runs])


def align(E: list[Block], D: list[Block], tol: float) -> dict:
    """Minimum over the base format authored text starts from: D's own dominant format (text typed into D takes the
    format at the caret, free) or E's dominant format, set once for the whole document beforehand (select all + the
    paragraph / character changes). Each case is a Gotoh alignment (_align)."""
    d_ps, d_run = _base(D)
    e_ps, e_run = _base(E)
    best = _align(E, D, tol, d_ps if d_ps else e_ps, d_run if d_ps else e_run)
    best["base_format"] = "document"
    if d_ps and e_ps and (d_ps != e_ps or d_run != e_run):
        setup = C()["char_format"]["select_range"] + para_format(d_ps, e_ps) + char_format([("x", d_run)], [("x", e_run)]) if d_run and e_run \
            else C()["char_format"]["select_range"] + para_format(d_ps, e_ps)
        alt = _align(E, D, tol, e_ps, e_run)
        if alt["cost"] + setup < best["cost"]:
            alt["cost"] += setup
            alt["base_format"] = "example (set once)"
            alt["base_setup_cost"] = setup
            best = alt
    return best


def _align(E: list[Block], D: list[Block], tol: float, base_ps, base_run) -> dict:
    """Gotoh alignment. States: S (last op keep/substitute), A (last op authored a flow paragraph), X (last op deleted a
    flow block). Returns cost, counts and the operation list."""
    n, m = len(E), len(D)
    caret = C()["text"]["place_caret"]
    au = [author_cost(e, base_ps, base_run, tol) for e in E]
    de = [delete_cost(d) for d in D]
    S = [[INF] * (m + 1) for _ in range(n + 1)]
    A = [[INF] * (m + 1) for _ in range(n + 1)]
    X = [[INF] * (m + 1) for _ in range(n + 1)]
    bt = {}
    S[0][0] = 0
    sub_cache = {}

    def sub(i, j):
        k = (i, j)
        if k not in sub_cache:
            sub_cache[k] = sub_cost(D[j], E[i], tol)
        return sub_cache[k]
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best = (INF, None)
            if i and j:
                c = sub(i - 1, j - 1)
                if c < INF:
                    prev = min((S[i - 1][j - 1], "S"), (A[i - 1][j - 1], "A"), (X[i - 1][j - 1], "X"))
                    best = (prev[0] + c, ("sub", prev[1]))
            S[i][j] = best[0]
            bt[("S", i, j)] = best[1]
            if i:                                         # author E[i-1]
                e = E[i - 1]
                flow = e.kind == "P"
                cands = [(S[i - 1][j] + au[i - 1] + (caret if flow else 0), "S"), (X[i - 1][j] + au[i - 1] + (caret if flow else 0), "X"),
                         (A[i - 1][j] + au[i - 1] + (0 if flow else 0), "A")]
                v = min(cands)
                A[i][j] = v[0]
                bt[("A", i, j)] = ("ins", v[1])
            if j:                                         # delete D[j-1]
                c, flow = de[j - 1]
                cands = [(S[i][j - 1] + c, "S"), (A[i][j - 1] + c, "A"), (X[i][j - 1] + (0 if flow else c), "X")]
                v = min(cands)
                X[i][j] = v[0]
                bt[("X", i, j)] = ("del", v[1])
    end = min((S[n][m], "S"), (A[n][m], "A"), (X[n][m], "X"))
    ops, st, i, j = [], end[1], n, m
    while i or j:
        op, prev = bt[(st, i, j)]
        if op == "sub":
            c = sub_cache[(i - 1, j - 1)]
            ops.append(("keep" if c == 0 else "edit", E[i - 1].kind, E[i - 1].nid, D[j - 1].nid, c))
            i, j = i - 1, j - 1
        elif op == "ins":
            ops.append(("author", E[i - 1].kind, E[i - 1].nid, None, au[i - 1]))
            i -= 1
        else:
            ops.append(("delete", D[j - 1].kind, None, D[j - 1].nid, de[j - 1][0]))
            j -= 1
        st = prev
    ops.reverse()
    cnt = {}
    for o in ops:
        cnt[o[0]] = cnt.get(o[0], 0) + 1
    return {"cost": end[0], "counts": cnt, "ops": ops}


def page_cost(d_page: dict, e_page: dict) -> int:
    if not d_page or not e_page:
        return 0
    p = C()["page"]
    n = sum(1 for k in ("width", "height", "left", "right", "top", "bottom") if abs(d_page[k] - e_page[k]) > 2)
    n += int(d_page["landscape"] != e_page["landscape"]) + int(d_page["cols"] != e_page["cols"])
    return p["setup_open"] + n * p["setup_field"] + p["setup_ok"] if n else 0


def batch_format_saving(ops: list, E: list[Block], D: list[Block], tol: float) -> float:
    """Consecutive edited paragraphs that need the SAME paragraph-format change get it in one go: one drag over them +
    one dialog, instead of one dialog each. Applied to the optimal alignment (the plan stays valid; an upper bound)."""
    eb, db = {b.nid: b for b in E}, {b.nid: b for b in D}
    f = C()["para_format"]
    saving, run, sig_prev = 0.0, 0, None

    def sig(d, e):
        if not geometry_off(d, e, tol):
            return None
        ch = tuple(sorted((k, d.ps[k], e.ps[k]) for k in ("left", "right", "indent", "prev", "next") if abs(d.ps[k] - e.ps[k]) > 1.0))
        if d.ps["ls"] != e.ps["ls"]:
            ch += (("ls", d.ps["ls"], e.ps["ls"]),)
        return ch or None

    def close(n, s_):
        if n >= 2 and s_:
            per = f["dialog_open"] + len(s_) * f["dialog_field"] + f["dialog_ok"]
            return max(0.0, (n - 1) * per - C()["text"]["select_range"])
        return 0.0
    for o in ops:
        s_ = None
        if o[0] == "edit" and o[1] == "P" and o[2] in eb and o[3] in db and eb[o[2]].ps and db[o[3]].ps:
            s_ = sig(db[o[3]], eb[o[2]])
        if s_ is not None and s_ == sig_prev:
            run += 1
        else:
            saving += close(run, sig_prev)
            run, sig_prev = (1, s_) if s_ is not None else (0, None)
    saving += close(run, sig_prev)
    return saving


def document_cost(d_sess, e_sess, d_layout=None, e_layout=None, lang: str | None = None) -> dict:
    from .blocks import extract, page_setup
    if lang:
        _LANG["lang"] = lang
    tol = float(C()["layout_tolerance_pt"])
    dz, ez = extract(d_sess, d_layout), extract(e_sess, e_layout)
    out = {"version": COST_VERSION, "unit": C().get("unit", "actions"), "zones": {}, "cost": 0}
    for z in ("body", "header", "footer"):
        r = align(ez[z], dz[z], tol)
        sv = batch_format_saving(r["ops"], ez[z], dz[z], tol)
        out["zones"][z] = {"cost": r["cost"] - sv, "cost_before_batch": r["cost"], "batch_format_saving": sv, "counts": r["counts"],
                           "blocks_example": len(ez[z]), "blocks_document": len(dz[z]), "base_format": r.get("base_format"),
                           "ops": [list(o) for o in r["ops"]]}
        out["cost"] += r["cost"] - sv
    pc = page_cost(page_setup(d_sess), page_setup(e_sess))
    out["page_setup"] = pc
    out["cost"] += pc
    return out
