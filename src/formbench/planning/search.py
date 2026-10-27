"""Minimum Form Completion Cost planner.

``C*(D, tau) = min over executable plans of sum of operator costs``. The plan space is the *grounded* action set
defined in the v1 protocol (docs/protocol.md, archived 2026-10-18): per-paragraph exact hunk scripts (DP), container creation, row insertion and a finite
set of layout corrections. Search is A* over real document states (every transition is executed by the editing
backend); results are ``exact`` only when the search terminates with a proof, otherwise proven bounds are returned.
"""
from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field

from lxml import etree

from ..completion.editor_backends import EditorSession, OpError
from ..contracts.schema import CHECKED_GLYPHS
from ..contracts.verify import DocView, GoalReport, Verifier, norm
from ..document_ir import fonts
from ..formats.hwpx import HwpxPackage
from ..util import Status, cjson, jhash
from .costs import CostModel
from .dp import hunk_ops

NID_KEYS = ("target", "table", "field")
SOLVER_VERSION = "1.1"             # 1.1: open list stores plans, not document snapshots (memory only; same decisions as 1.0)
TIE_BREAK = "min f, then fewer typed characters, then fewer operators, then lexicographically smallest canonical-JSON plan"


@dataclass
class Step:
    ops: list[dict]
    label: str
    key: str | None = None            # normal-form key: a corrective with the same key is applied at most once per path


@dataclass
class ComponentResult:
    preds: list[int]
    status: str                       # exact | bounded | infeasible | timeout
    ops: list[dict] = field(default_factory=list)
    cost: int | None = None
    lower_bound: int = 0
    upper_bound: int | None = None
    expansions: int = 0
    reason: str = ""


@dataclass
class PlanResult:
    status: str                       # exact | bounded | infeasible | unsupported | timeout | failed
    plan: list[dict] = field(default_factory=list)
    cost: int | None = None
    lower_bound: int | None = None
    upper_bound: int | None = None
    exact_minimum: bool = False
    completion_feasible: bool = False
    components: list[dict] = field(default_factory=list)
    solver_status: str = ""
    expansions: int = 0
    elapsed_s: float = 0.0
    scheme: str = "uniform"
    reason: str = ""
    goal: dict | None = None

    @property
    def optimality_gap(self):
        if self.upper_bound is None or self.lower_bound is None:
            return None
        return self.upper_bound - self.lower_bound

    def to_json(self) -> dict:
        return {"status": self.status, "cost": self.cost, "lower_bound": self.lower_bound, "upper_bound": self.upper_bound,
                "optimality_gap": self.optimality_gap, "exact_minimum": self.exact_minimum, "completion_feasible": self.completion_feasible,
                "solver_status": self.solver_status, "expansions": self.expansions, "scheme": self.scheme, "reason": self.reason,
                "components": self.components, "n_ops": len(self.plan), "solver_version": SOLVER_VERSION, "tie_break": TIE_BREAK}


def state_key(sess: EditorSession) -> str:
    h = [etree.tostring(r) for r in sess.pkg.sections]
    return jhash([[x.decode("utf-8", "replace") for x in h], sess.caret, sess.selection, list(sess.obj_selection)])


def _lcs(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


class Planner:
    def __init__(self, contract: dict, costs: CostModel | None = None, max_expansions: int = 60, time_limit_s: float = 45.0,
                 assets: dict | None = None, greedy_only: bool = False):
        self.c = contract
        self.costs = costs or CostModel()
        self.max_expansions = max_expansions
        self.time_limit_s = time_limit_s
        self.assets = assets or {}
        self.greedy_only = greedy_only
        self.fields = {f["field_id"]: f for f in contract["fields"]}
        self.preds = contract["target_predicates"]
        self.min_op = self.costs.min_base()
        self.min_enter = self.costs.min_base(["focus", "focus_cell", "focus_field", "create_textbox", "create_table", "split_paragraph", "duplicate_paragraph"])
        self.min_font = float(contract.get("min_font_pt", 7.0))
        self.max_correctives = 3

    # ------------------------------------------------------------------ target values
    def target_value(self, p: dict) -> str:
        return "√" if p["type"] == "field_checked" else p["value"]

    def _satisfied(self, p: dict, got: str) -> bool:
        if p["type"] == "field_checked":
            return norm(got) in [norm(g) for g in CHECKED_GLYPHS]
        return norm(got) == norm(p["value"])

    # ------------------------------------------------------------------ step generation
    def steps(self, sess: EditorSession, view: DocView, ver: Verifier, rep: GoalReport, idxs: list[int], d0_nodes: set, used: frozenset) -> list[Step]:
        out: list[Step] = []
        unsat = [t for t in rep.targets if not t["ok"]]
        by_para: dict[str, list] = {}
        # canonical target order (partial-order reduction): steps of different targets commute, so only the first
        # unsatisfied target is expanded; inline fields sharing its paragraph are solved jointly with it.
        first_para = None
        for n_t, t in enumerate(unsat):
            p = self.preds[t["index"]]
            if n_t > 0:
                if first_para is not None and p["type"] in ("field_text_equals", "field_checked"):
                    f = self.fields[p["field_id"]]
                    ss = ver.sites(view, f)
                    if ss and ss[0].span is not None and ss[0].para == first_para:
                        by_para[first_para].append((p, f, ss[0]))
                continue
            if p["type"] == "table_row_count_at_least":
                g = ver.groups[p["table_id"]]
                row = t.get("missing_row", 0)
                if "bottom rule" in t.get("reason", ""):
                    out.extend(self._rule_steps(view, ver, g, row))
                else:
                    out.extend(self._new_row_steps(view, ver, g, row))
                continue
            f = self.fields[p["field_id"]]
            if p["type"] == "image_in_region":
                page, r = ver.resolve(view, f)
                out.append(Step([{"op": "insert_image", "page": page, "x": round(r[0], 2), "y": round(r[1], 2), "w": round(r[2] - r[0], 2),
                                  "h": round(r[3] - r[1], 2), "asset": p["asset"]}], f"image:{f['field_id']}"))
                continue
            if p["type"] == "field_checked":
                page, r = ver.resolve(view, f)
                ctl = next((b for b in view.layout.boxes if b.kind == "form" and b.page == page and b.extra.get("form") in ("checkBtn", "radioBtn")
                            and b.x < r[2] + 2.5 and b.x1 > r[0] - 2.5 and b.y < r[3] + 2.5 and b.y1 > r[1] - 2.5), None)
                if ctl is not None:
                    out.append(Step([{"op": "toggle_checkbox", "target": ctl.nid}], f"toggle:{f['field_id']}"))
                    continue
            sites = ver.sites(view, f)
            val = self.target_value(p)
            if not sites:
                rp = f.get("repeat") or {}
                if rp.get("new_row"):
                    out.extend(self._new_row_steps(view, ver, ver.groups[rp["group"]], rp["row"]))
                    continue
                page, r = ver.resolve(view, f)
                if f["kind"] == "check":
                    side = max(r[3] - r[1], 9.0) + 4.0
                    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
                    geo = (cx - side / 2, cy - side / 2, side + 4, side)
                else:
                    geo = (r[0] + 1.0, r[1] + 1.0, max(r[2] - r[0] - 2.0, 12.0), max(r[3] - r[1] - 2.0, 12.0))
                out.append(Step([{"op": "create_textbox", "page": page, "x": round(geo[0], 2), "y": round(geo[1], 2), "w": round(geo[2], 2), "h": round(geo[3], 2)},
                                 {"op": "type_text", "text": val}], f"box+type:{f['field_id']}"))
                continue
            if sites[0].span is not None:
                first_para = sites[0].para
                by_para.setdefault(first_para, []).append((p, f, sites[0]))
            elif len(sites) == 1:
                cur = view.text[sites[0].para]
                ops, _ = hunk_ops(sites[0].para, 0, cur, val, self.costs)
                if ops:
                    out.append(Step(ops, f"type:{f['field_id']}"))
            else:
                out.extend(self._multi_site_steps(sess, view, f, sites, val))
        for para, items in by_para.items():          # all inline fields of one paragraph: joint exact hunk script
            raw = view.text[para]
            tgt, last = [], 0
            for p, _f, s in sorted(items, key=lambda it: it[2].span[0]):
                tgt.append(raw[last:s.span[0]])
                tgt.append(self.target_value(p))
                last = s.span[1]
            tgt.append(raw[last:])
            fld = None
            if len(items) == 1:
                pp = view.ir.nodes.get(para)
                s = items[0][2]
                if pp is not None and s.span[0] == s.span[1]:
                    fld = next((i.meta.get("nid") for i in pp.inlines if i.kind == "field_begin" and i.meta.get("start") == s.span[0]), None)
            ops, _ = hunk_ops(para, 0, raw, "".join(tgt), self.costs, field=fld)
            if ops:
                out.append(Step(ops, "type-inline:" + ",".join(it[1]["field_id"] for it in items)))
        out.extend(self._corrective_steps(sess, view, ver, rep, d0_nodes, used))
        seen, uniq = set(), []
        n_corr = sum(1 for u in used if u)
        for s in out:
            k = cjson(s.ops)
            if s.key is not None and (n_corr >= self.max_correctives or s.key.split(":")[0] + ":" + s.key.split(":")[1] in {":".join(u.split(":")[:2]) for u in used}):
                continue                      # normal form: bounded number of layout corrections, one per kind and object
            if k not in seen and (s.key is None or s.key not in used):
                seen.add(k)
                uniq.append(s)
        return uniq

    def _capacity(self, view: DocView, para: str, text: str) -> int:
        p = view.ir.nodes.get(para)
        box = view.para_box.get(para)
        if p is None or box is None:
            return len(text)
        cs = p.default_cs
        w, n = 0.0, 0
        for ch in text:
            w += fonts.resolve_char(cs.font, cs.bold, ch)[1] * cs.size * cs.ratio / 100.0
            if w > box.w + 0.01:
                break
            n += 1
        return n

    def _multi_site_steps(self, sess, view: DocView, f: dict, sites, val: str) -> list[Step]:
        out = []
        plans = []
        plans.append(("all-in-first", [val] + [""] * (len(sites) - 1)))
        parts, rest = [], val
        for s in sites:
            n = self._capacity(view, s.para, rest) if rest else 0
            if n < len(rest) and n > 0:
                cut = rest.rfind(" ", 0, n + 1)
                n = cut + 1 if cut > n * 0.5 else n
            parts.append(rest[:n])
            rest = rest[n:]
        if not rest and sum(1 for x in parts if x) > 1:
            plans.append(("split-by-capacity", parts))
        for name, targets in plans:
            ops: list[dict] = []
            for s, tv in zip(sites, targets):
                cur = view.text[s.para]
                if cur != tv:
                    o, _ = hunk_ops(s.para, 0, cur, tv, self.costs)
                    ops.extend(o)
            if ops:
                out.append(Step(ops, f"type[{name}]:{f['field_id']}"))
        return out

    def _row_regions(self, view: DocView, ver: Verifier, g: dict, row: int):
        res = []
        for col in range(len(g["columns"])):
            f = ver.by_group_cell.get((g["group_id"], row, col))
            if f is None:
                return None
            res.append((f, ver.resolve(view, f)))
        return res

    def _new_row_steps(self, view: DocView, ver: Verifier, g: dict, row: int) -> list[Step]:
        out: list[Step] = []
        regs = self._row_regions(view, ver, g, row)
        if regs is None or row == 0:
            return out
        prev = ver.by_group_cell.get((g["group_id"], row - 1, 0))
        ps = ver.sites(view, prev) if prev is not None else []
        cb = view.container_box(ps[0].para) if ps else None
        if cb is not None and cb.kind == "cell":
            out.append(Step([{"op": "table_insert_row", "table": cb.extra["table"], "after_row": cb.extra["row"] + cb.extra.get("rowspan", 1) - 1}],
                            f"insert_row:{g['group_id']}:{row}"))
            return out
        page = regs[0][1][0]
        x0, y0 = regs[0][1][1][0], regs[0][1][1][1]
        widths = [round(r[1][2] - r[1][0], 2) for _, r in regs]
        h = round(regs[0][1][1][3] - regs[0][1][1][1], 2)
        out.append(Step([{"op": "create_table", "page": page, "x": round(x0, 2), "y": round(y0, 2), "col_widths": widths, "row_heights": [h]}],
                        f"create_row_table:{g['group_id']}:{row}"))
        boxes = [{"op": "create_textbox", "page": page, "x": round(r[0], 2), "y": round(r[1], 2), "w": round(r[2] - r[0], 2), "h": round(r[3] - r[1], 2)}
                 for _, (pg, r) in regs]
        out.append(Step(boxes, f"create_row_boxes:{g['group_id']}:{row}"))
        return out

    def _rule_steps(self, view: DocView, ver: Verifier, g: dict, row: int) -> list[Step]:
        regs = self._row_regions(view, ver, g, row)
        if regs is None:
            return []
        page = regs[0][1][0]
        y = max(r[1][3] for _, r in regs)
        return [Step([{"op": "create_line", "page": page, "x0": round(regs[0][1][1][0], 2), "y0": round(y, 2), "x1": round(regs[-1][1][1][2], 2), "y1": round(y, 2)}],
                     f"rule:{g['group_id']}:{row}", key=f"rule:{g['group_id']}:{row}")]

    def _shrink_steps(self, view: DocView, v: dict) -> list[Step]:
        para = v.get("para")
        p = view.ir.nodes.get(para)
        if p is None or not p.text:
            return []
        cur = min((i.cs.size for i in p.inlines if i.kind == "text" and i.cs), default=10.0)
        out = []
        for size in (9.0, 8.0, 7.0, 6.0):
            if size < cur - 0.01 and size >= self.min_font:
                out.append(Step([{"op": "focus", "target": para, "offset": 0}, {"op": "select_range", "start": 0, "end": len(p.text)},
                                 {"op": "set_char_format", "props": {"size": size}}], f"shrink:{para}:{size}", key=f"shrink:{para}:{size}"))
        return out

    def _corrective_steps(self, sess, view: DocView, ver: Verifier, rep: GoalReport, d0_nodes: set, used: frozenset) -> list[Step]:
        out: list[Step] = []
        for v in rep.violations:
            code, cont, other = v["code"], v.get("container"), v.get("other")
            cbox = view.boxes.get(cont) if cont else None
            if code == "clipped_text" and cbox is not None and cbox.kind == "textbox":
                out.append(Step([{"op": "set_size_lock", "target": cont, "locked": False}], f"unlock:{cont}", key=f"unlock:{cont}"))
                need = cbox.extra.get("content_h", cbox.h) + 6.0
                out.append(Step([{"op": "resize_object", "target": cont, "w": round(cbox.w, 2), "h": round(need, 2)}], f"resize:{cont}", key=f"resize:{cont}"))
            if code in ("overlap_text", "overlap_rule"):
                amt = float(v.get("amount", 0.0))
                ob = view.boxes.get(other) if other else None
                oshape = view.ir.nodes.get(other) if other else None
                floating = ob is not None and ob.kind in ("textbox", "shape", "pic", "table") and ob.extra.get("owner") is None and \
                    oshape is not None and not oshape.pos.treat_as_char
                if floating and amt > 0:
                    out.append(Step([{"op": "move_object", "target": other, "dx": 0, "dy": round(amt, 2)}], f"move:{other}", key=f"move:{other}"))
                    if ob.kind == "textbox" and not norm("".join(p.text for p in (oshape.paras or []))):
                        out.append(Step([{"op": "delete_object", "target": other}], f"delete-empty:{other}", key=f"delete:{other}"))
                if cbox is not None and amt > 0:
                    pw, ph = view.layout.pages[cbox.page]
                    y_from = cbox.y + cbox.extra.get("declared_h", cbox.h) - 0.5 if cbox.kind == "textbox" else cbox.y1 - amt
                    out.append(Step([{"op": "select_objects_in_rect", "page": cbox.page, "x0": 0, "y0": round(y_from, 2), "x1": round(pw, 2), "y1": round(ph, 2)},
                                     {"op": "move_selection", "dx": 0, "dy": round(amt, 2)}], f"push-below:{cont}", key=f"push:{cont}"))
            if code in ("clipped_text", "overlap_text", "overlap_rule", "overlap_ink", "out_of_page"):
                out.extend(self._shrink_steps(view, v))
        for p in rep.protected:
            if p["type"] == "no_extraneous_marks" and not p["ok"]:
                for s in view.ir.shapes():
                    if s.is_textbox and s.line.visible and s.nid not in d0_nodes:
                        out.append(Step([{"op": "set_object_style", "target": s.nid, "border": "NONE"}], f"hide-border:{s.nid}", key=f"border:{s.nid}"))
        return out

    # ------------------------------------------------------------------ heuristic
    def h(self, sess: EditorSession, view: DocView, ver: Verifier, rep: GoalReport) -> int:
        total = 0
        units: dict = {}
        for t in rep.targets:
            if t["ok"]:
                continue
            p = self.preds[t["index"]]
            if p["type"] == "table_row_count_at_least":
                continue
            f = self.fields[p["field_id"]]
            if p["type"] == "image_in_region":
                total += self.costs.base("insert_image")
                continue
            sites = ver.sites(view, f)
            key = sites[0].para if sites else ("new", f["field_id"])
            units.setdefault(key, []).append((p, f, sites))
        caret_para = sess.caret[0] if sess.caret else None
        ct, cd, cs_, per = self.costs.base("type_text"), self.costs.base("delete_selection"), self.costs.base("select_range"), self.costs.per_char("type_text")
        tog = self.costs.base("toggle_checkbox")
        for key, items in units.items():
            need_chars = 0
            for p, f, sites in items:
                got = ver.field_text(view, f, sites) if sites else ""
                val = self.target_value(p)
                need_chars += max(0, len(norm(val)) - _lcs(norm(got), norm(val)))
            lb = (ct + per * need_chars) if need_chars > 0 else min(cs_ + cd, ct)
            if not (isinstance(key, str) and key == caret_para):
                lb += self.min_enter
            if any(p["type"] == "field_checked" for p, _, _ in items):
                lb = min(lb, tog)                     # a form control can be toggled with one operator
            total += lb
        if total == 0 and not rep.ok:
            total = self.min_op
        return total

    # ------------------------------------------------------------------ component search
    def _solve_component(self, sess: EditorSession, ver: Verifier, d0: DocView, root, idxs: list[int], deadline: float,
                         greedy_only: bool = False) -> ComponentResult:
        d0_nodes = set(d0.ir.nodes)
        counter = 0
        # Open-list entries carry the plan only. The document state of a node is rebuilt by replaying its plan on the
        # root snapshot when (and only if) the node is popped: keeping a snapshot + layout view per *generated* node
        # cost ~6 MB each and let one search grow to gigabytes (host OOM of 2026-10-14, reports/failures).
        open_: list = [(0, 0, 0, "", counter, 0, [], frozenset())]
        best_g: dict[str, int] = {}
        expansions = 0
        res = ComponentResult(idxs, "infeasible")
        min_open_f = 0
        while open_:
            f_, chars, nops, _, _, g, plan, used = heapq.heappop(open_)
            min_open_f = f_
            if res.upper_bound is not None and f_ >= res.upper_bound:
                res.status, res.lower_bound = "exact", res.upper_bound
                break
            if expansions >= self.max_expansions or time.monotonic() > deadline:
                res.status = "timeout" if time.monotonic() > deadline else "bounded"
                res.reason = "search budget exhausted"
                res.lower_bound = f_
                break
            sess.restore(root)
            for op in plan:                             # deterministic replay: same state the plan produced when generated
                sess.apply(op)
            key = state_key(sess)
            if best_g.get(key, 1 << 60) <= g:
                continue
            best_g[key] = g
            snap = sess.snapshot() if plan else root    # the only live snapshot besides the root
            view = d0 if not plan else DocView.of(sess)
            rep = ver.check(view, idxs, protected=True)
            expansions += 1
            if rep.ok:
                if res.upper_bound is None or g < res.upper_bound:
                    res.upper_bound, res.cost, res.ops = g, g, plan
                res.status, res.lower_bound = "exact", g           # A*: first goal popped with admissible h is optimal
                break
            timed_out = False
            for st in self.steps(sess, view, ver, rep, idxs, d0_nodes, used):
                if time.monotonic() > deadline:         # an expansion with many successors must not outlive the safety net
                    timed_out = True
                    break
                sess.restore(snap)
                try:
                    for op in st.ops:
                        sess.apply(op)
                except OpError:
                    continue
                g2 = g + self.costs.plan_cost(st.ops)
                child_plan = plan + st.ops
                cview = DocView.of(sess)
                crep = ver.check(cview, idxs, protected=True)
                hh = 0 if (crep.ok or greedy_only) else self.h(sess, cview, ver, crep)
                counter += 1
                heapq.heappush(open_, (g2 + hh, chars + CostModel.content_chars(st.ops), nops + len(st.ops), cjson(child_plan) if len(child_plan) < 40 else "",
                                       counter, g2, child_plan, used | ({st.key} if st.key else frozenset())))
                if greedy_only:
                    break
            if timed_out:
                res.status, res.reason, res.lower_bound = "timeout", "search budget exhausted", f_
                break
        else:
            res.status = "infeasible" if res.upper_bound is None else "exact"
            res.reason = "grounded action space exhausted without reaching the goal" if res.upper_bound is None else ""
            res.lower_bound = res.upper_bound if res.upper_bound is not None else min_open_f
        res.expansions = expansions
        if greedy_only and res.status == "exact":
            res.status = "bounded"                      # a greedy dive proves feasibility only
            res.lower_bound = 0
        return res

    # ------------------------------------------------------------------ whole task
    def components(self, ver: Verifier, view: DocView) -> list[list[int]]:
        parent = list(range(len(self.preds)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)
        owner_of: dict = {}
        for i, p in enumerate(self.preds):
            keys = []
            if p["type"] == "table_row_count_at_least":
                keys.append(("group", p["table_id"]))
            else:
                f = self.fields[p["field_id"]]
                if f.get("repeat"):
                    keys.append(("group", f["repeat"]["group"]))
                for s in ver.sites(view, f):
                    keys.append(("para", s.para))
                    b = view.boxes.get(s.container) if s.container else None
                    if b is not None and b.kind == "textbox" and b.extra.get("floating"):
                        keys.append(("box", s.container))
            for k in keys:
                if k in owner_of:
                    union(i, owner_of[k])
                owner_of[k] = i
        groups: dict[int, list[int]] = {}
        for i in range(len(self.preds)):
            groups.setdefault(find(i), []).append(i)
        return sorted(groups.values(), key=lambda g: g[0])

    @staticmethod
    def _remap(ops: list[dict], l0: int, delta: int) -> list[dict]:
        if delta == 0:
            return [dict(o) for o in ops]

        def m(v):
            if isinstance(v, str) and v.startswith("s") and "n" in v:
                sec, k = v[1:].split("n")
                if k.isdigit() and int(k) >= l0:
                    return f"s{sec}n{int(k) + delta}"
            return v
        out = []
        for o in ops:
            o = dict(o)
            for k in NID_KEYS:
                if k in o:
                    o[k] = m(o[k])
            if "targets" in o:
                o["targets"] = [m(x) for x in o["targets"]]
            out.append(o)
        return out

    def solve(self, blank: bytes) -> PlanResult:
        t0 = time.monotonic()
        deadline = t0 + self.time_limit_s
        res = PlanResult("failed", scheme=self.costs.scheme)
        try:
            sess = EditorSession.load(blank, self.assets)
        except Exception as e:      # noqa: BLE001 - unreadable input is a reportable outcome, not a crash
            res.status, res.reason = "unsupported", f"cannot open document: {e}"
            return res
        for p in self.preds:
            if p["type"] == "image_in_region" and p["asset"] not in self.assets:
                res.status, res.reason = "unsupported", f"asset {p['asset']} not supplied"
                return res
        sess.keep_undo = False
        view0 = DocView.of(sess)
        ver0 = Verifier(self.c, view0)
        root = sess.snapshot()
        l0 = int(sess.pkg.sections[0].get("{urn:formbench:session}next") or 0)
        comps = self.components(ver0, view0)
        merged_once = False
        while True:
            results = [self._solve_component(sess, ver0, view0, root, c, deadline, self.greedy_only) for c in comps]
            res.expansions += sum(r.expansions for r in results)
            res.components = [{"preds": r.preds, "status": r.status, "cost": r.cost, "lower_bound": r.lower_bound, "upper_bound": r.upper_bound,
                               "expansions": r.expansions, "reason": r.reason} for r in results]
            res.lower_bound = sum(r.lower_bound for r in results)
            if any(r.upper_bound is None for r in results):
                bad = [r for r in results if r.upper_bound is None]
                res.status = "infeasible" if all(r.status == "infeasible" for r in bad) else ("timeout" if any(r.status == "timeout" for r in bad) else "bounded")
                res.solver_status = {"infeasible": "infeasible_under_grounded_operators", "timeout": "timeout", "bounded": Status.SOLVER_UNPROVEN}[res.status]
                res.reason = "; ".join(sorted({r.reason for r in bad if r.reason}))
                res.upper_bound = None
                break
            # assemble: replay component plans in canonical order on one fresh document with id remapping
            s2 = EditorSession.load(blank, self.assets)
            s2.keep_undo = False
            plan: list[dict] = []
            ok_replay = True
            for r in results:
                delta = int(s2.pkg.sections[0].get("{urn:formbench:session}next") or 0) - l0
                ops = self._remap(r.ops, l0, delta)
                try:
                    for op in ops:
                        s2.apply(op)
                except OpError as e:
                    ok_replay = False
                    res.reason = f"component plans interact: {e.code}"
                    break
                plan.extend(ops)
            rep = ver0.check(DocView.of(s2)) if ok_replay else None
            if ok_replay and rep.ok:
                res.plan, res.cost, res.upper_bound = plan, self.costs.plan_cost(plan), self.costs.plan_cost(plan)
                res.completion_feasible = True
                res.goal = rep.to_json()
                all_exact = all(r.status == "exact" for r in results)
                res.exact_minimum = all_exact and res.lower_bound == res.upper_bound
                res.status = "exact" if res.exact_minimum else ("timeout" if any(r.status == "timeout" for r in results) else "bounded")
                res.solver_status = "optimal_proven" if res.exact_minimum else Status.SOLVER_UNPROVEN
                break
            if len(comps) > 1 and not merged_once and time.monotonic() < deadline:
                comps = [sorted(i for c in comps for i in c)]         # interaction detected: solve jointly
                merged_once = True
                continue
            res.status = "failed"
            res.solver_status = "interaction_unresolved"
            res.goal = rep.to_json() if rep is not None else None
            res.reason = res.reason or "assembled plan does not satisfy the contract"
            res.upper_bound = None
            res.lower_bound = max((r.lower_bound for r in results), default=0)
            break
        res.elapsed_s = round(time.monotonic() - t0, 3)
        return res


def solve(contract: dict, blank: bytes, scheme: str | None = None, **kw) -> PlanResult:
    return Planner(contract, CostModel(scheme), **kw).solve(blank)


def view_of_bytes(data: bytes) -> DocView:
    return DocView(HwpxPackage.load(data))
