"""Human-action executor over the native editing backend (contract: configs/edit_operators_v2.yaml).

A person never addresses a node id. Every action here is a gesture on the rendered page (a click at a point, a drag)
or a key press, resolved by hit testing against the reference layout and checked by the guards that make the action
behave identically in Hangul, Word and Writer (the common subset). Admitted actions are translated into backend
operations of the editing session, which changes the real document; costs are recorded per action.

Text input follows the user's definition (2026-10-16): one ``input_value`` inserts ONE contract value (or one
contiguous segment of it, segments in order) into ONE normal editable input area and costs one action.
"""
from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass, field

from ..completion.editor_backends import EditorSession, OpError
from ..document_ir.schema import Cell, Shape, Table
from ..knowledge.contract import load_contract

KLM = {"K": 280, "P": 1100, "H": 400, "M": 1350, "B": 100, "BB": 200}
BORDER_BAND_PT = 2.0          # a point within this distance of a text box edge hits the box border (object), not its text
MOUSE_ACTIONS = {"click_text", "double_click_word", "drag_select_text", "drag_object"}


class ActionError(Exception):
    """An action that cannot be performed in the common-subset UI in the current state."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code


@dataclass
class Hit:
    kind: str                      # text | object | none
    para: str | None = None
    offset: int | None = None
    container: str = "body"        # body | cell | textbox
    container_nid: str | None = None
    obj: str | None = None
    obj_kind: str | None = None
    extra: dict = field(default_factory=dict)


def keystrokes(text: str) -> int:
    """Keystrokes of typing ``text`` on a standard keyboard: Hangul syllables as 2-beolsik jamo keys (compound
    vowels / finals split, tense consonants and ㅒㅖ with Shift), Latin capitals with Shift, everything else one key."""
    n = 0
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            s = o - 0xAC00
            ini, med, fin = s // 588, (s % 588) // 28, s % 28
            n += 1 + (1 if ini in (1, 4, 8, 10, 13) else 0)                       # ㄲㄸㅃㅆㅉ: Shift
            n += 2 if med in (9, 10, 11, 14, 15, 16, 19) else 1                     # ㅘㅙㅚㅝㅞㅟㅢ: two keys
            n += 1 if med in (3, 7) else 0                                          # ㅒㅖ: Shift
            if fin:
                n += 2 if fin in (3, 5, 6, 9, 10, 11, 12, 13, 14, 15, 18) else 1    # ㄳㄵㄶㄺㄻㄼㄽㄾㄿㅀㅄ: two keys
                n += 1 if fin in (2, 20) else 0                                     # ㄲ ㅆ final: Shift
        elif ch.isascii() and ch.isupper():
            n += 2
        elif unicodedata.category(ch).startswith("C") and ch not in "\n\t":
            continue
        else:
            n += 1
    return n


class HumanSession:
    def __init__(self, ed: EditorSession, values: dict[str, str] | None = None, contract: dict | None = None):
        self.ed = ed
        self.values = dict(values or {})
        self.contract = contract or load_contract()
        self.ops = {o["name"]: o for o in self.contract["operators"]}
        self.next_seg: dict[str, int] = {}
        self.caret_known = False
        self.hand = "keyboard"
        self.log: list[dict] = []
        self.assumptions: set[str] = set()

    # ------------------------------------------------------------------ lookup helpers
    @property
    def ir(self):
        return self.ed.ir

    @property
    def layout(self):
        return self.ed.layout

    def _container_of(self, para_nid: str):
        p = self.ir.nodes.get(para_nid)
        par = getattr(p, "parent", None)
        while par is not None:
            if isinstance(par, Cell):
                return "cell", par
            if isinstance(par, Shape) and par.is_textbox:
                return "textbox", par
            par = getattr(par, "parent", None)
        return "body", None

    def _editable(self, nid: str) -> bool:
        try:
            self.ed.text_of(nid)
            return True
        except Exception:  # noqa: BLE001 - OpError (view-only / protected) or missing node
            return False

    def _para(self, nid: str):
        return self.ir.nodes.get(nid)

    def _text_len(self, nid: str) -> int:
        return len(self.ed.text_of(nid))

    # ------------------------------------------------------------------ hit testing
    def hit_test(self, page: int, x: float, y: float) -> Hit:
        lay = self.layout
        inside = [b for b in lay.boxes if b.page == page and b.x - 0.01 <= x <= b.x1 + 0.01 and b.y - 0.01 <= y <= b.y1 + 0.01]
        # objects and text boxes under the point: the innermost (smallest) one is the one drawn on top and hit;
        # objects laid out behind text are transparent to a plain click (hangul.behind_text_needs_select_tool);
        # on a text box's edge the box itself is hit (word.textbox_select_border), inside it its text
        cands, tbs = [], []
        glyph_owners = None
        for b in inside:
            if b.kind in ("pic", "shape", "form"):
                if (b.extra or {}).get("wrap") == "BEHIND_TEXT":
                    continue
                if b.kind == "shape" and (b.extra or {}).get("shape") == "group":
                    continue                                   # a group is hit through its members
                cands.append((b.w * b.h, 2, b, "object"))
            elif b.kind == "textbox":
                on_border = min(x - b.x, b.x1 - x, y - b.y, b.y1 - y) <= BORDER_BAND_PT
                if glyph_owners is None:                       # text boxes whose own glyphs lie under the point
                    glyph_owners = {f.owner for f in lay.frags if f.page == page and not f.generated
                                    and f.x - 0.01 <= x <= f.x1 + 0.01 and f.y - 0.01 <= y <= f.y1 + 0.01}
                tbs.append((0 if b.nid in glyph_owners else 1, b.w * b.h, 1 if on_border else 0, b, "border" if on_border else "text"))
        if tbs:
            # among overlapping text boxes (they overlap only when the reference fonts are wider than the producing
            # application's) the one whose text is under the point takes the click, else the innermost; against
            # objects the innermost wins as before
            g, area, bd, b, how = min(tbs, key=lambda c: (c[0], round(c[1], 1), c[2]))
            cands.append((area, bd, b, how))
        if cands:
            area, _, b, how = min(cands, key=lambda c: (round(c[0], 1), c[1]))
            if how != "text":
                return Hit("object", obj=b.nid, obj_kind=b.kind, extra={"wrap": (b.extra or {}).get("wrap"), "how": how})
            inside = [b]                                       # the text box's own paragraphs are the candidates
        # text: the innermost container under the point (text box > table cell > body), then its paragraph nearest
        # to the point (a click in a cell's padding or below its last line lands in that cell, as in every editor)
        conts = [b for b in inside if b.kind in ("textbox", "cell") and b.nid]
        if conts:
            cb = min(conts, key=lambda bb: (0 if bb.kind == "textbox" else 1, bb.w * bb.h))
            holder = self.ir.nodes.get(cb.nid)
            own = {p.nid for p in (getattr(holder, "paras", None) or [])}
            cands = [b for b in lay.boxes if b.kind == "para" and b.nid in own and b.page == page]
        else:
            cands = [b for b in inside if b.kind == "para" and b.nid and self._container_of(b.nid)[0] == "body"]
        cands = [b for b in cands if self._editable(b.nid)]      # view-only nodes (no native element) are not text
        if not cands:
            return Hit("none")
        pb = min(cands, key=lambda bb: (0 if bb.y - 0.01 <= y <= bb.y1 + 0.01 else min(abs(y - bb.y), abs(y - bb.y1)), bb.w * bb.h))
        frags = [f for f in lay.frags if f.page == page and f.para == pb.nid and not f.generated and f.y - 1 <= y <= f.y1 + 1]
        if frags:
            f = min(frags, key=lambda ff: 0 if ff.x <= x <= ff.x1 else min(abs(x - ff.x), abs(x - ff.x1)))
            off = f.start + min(range(len(f.xs)), key=lambda i: abs(f.x + f.xs[i] - x))
        else:                                               # empty line / empty paragraph: caret at its end
            off = self._text_len(pb.nid)
        kind, holder = self._container_of(pb.nid)
        return Hit("text", para=pb.nid, offset=off, container=kind, container_nid=getattr(holder, "nid", None))

    def point_in_para(self, para_nid: str, where: str = "end") -> tuple[int, float, float] | None:
        """A clickable point for a paragraph (the planner's way to aim a click): after its last character, or inside
        its empty box. None if the paragraph is not visible."""
        lay = self.layout
        fr = [f for f in lay.frags if f.para == para_nid and not f.generated]
        if fr:
            f = fr[-1] if where == "end" else fr[0]
            return (f.page, f.x1 - 0.2 if where == "end" else f.x + 0.2, f.y + f.h / 2)
        b = next((b for b in lay.boxes if b.kind == "para" and b.nid == para_nid), None)
        if b is None or b.h <= 0:
            return None
        return (b.page, b.x + min(3.0, b.w / 2), b.y + min(b.h / 2, 6.0))

    # ------------------------------------------------------------------ table helpers
    def _cell_of_caret(self) -> tuple[Table, Cell] | None:
        if not self.caret_known or self.ed.caret is None:
            return None
        kind, holder = self._container_of(self.ed.caret[0])
        if kind != "cell":
            return None
        return holder.table, holder

    @staticmethod
    def _reading_order(t: Table) -> list[Cell]:
        return sorted(t.cells, key=lambda c: (c.row, c.col))

    # ------------------------------------------------------------------ execution
    def apply(self, a: dict) -> dict:
        name = a["action"]
        spec = self.ops.get(name)
        if spec is None:
            raise ActionError("unknown_action", name)
        if spec["admission"] == "unsupported":
            raise ActionError("unsupported_operation", f"{name}: {spec.get('reason', '')}")
        fn = getattr(self, "_a_" + name)
        snap = self.ed.snapshot()
        state = (self.caret_known, dict(self.next_seg), self.hand)
        n_hist = len(self.ed.history)
        try:
            cost = fn(a)
        except (ActionError, OpError) as e:
            self.ed.restore(snap)
            self.caret_known, self.next_seg, self.hand = state[0], state[1], state[2]
            if isinstance(e, OpError):
                raise ActionError(getattr(e, "code", "backend_error"), str(e)) from e
            raise
        klm = cost.pop("klm")
        if name in MOUSE_ACTIONS:
            klm += KLM["H"] if self.hand == "keyboard" else 0
            self.hand = "mouse"
        else:
            klm += KLM["H"] if self.hand == "mouse" else 0
            self.hand = "keyboard"
        rec = {"action": dict(a), "actions": cost.get("actions", 1), "klm_ms": int(klm),
               "backend_ops": [dict(o) for o in self.ed.history[n_hist:]]}
        self.log.append(rec)
        return rec

    def run(self, actions: list[dict]) -> dict:
        for a in actions:
            self.apply(a)
        return self.totals()

    def totals(self) -> dict:
        return {"actions": sum(r["actions"] for r in self.log), "klm_ms": sum(r["klm_ms"] for r in self.log),
                "n": len(self.log), "assumptions": sorted(self.assumptions)}

    # ------------------------------------------------------------------ actions
    def _a_click_text(self, a):
        h = self.hit_test(a["page"], a["x"], a["y"])
        if h.kind != "text":
            raise ActionError("hit_not_text", f"point hits {h.kind} {h.obj_kind or ''}".strip())
        if h.container == "textbox" and h.container_nid in set(self.ed.obj_selection):
            raise ActionError("textbox_object_selected", "click elsewhere first (Writer frame rule)")
        self.ed.apply({"op": "focus", "target": h.para, "offset": h.offset})
        self.caret_known = True
        return {"klm": KLM["P"] + KLM["BB"]}

    def _tab_move(self, step: int):
        tc = self._cell_of_caret()
        if tc is None:
            raise ActionError("caret_not_in_cell")
        t, c = tc
        order = self._reading_order(t)
        i = order.index(c) + step
        if not 0 <= i < len(order):
            raise ActionError("no_adjacent_cell", "last/first cell")
        dst = order[i]
        if any(p.text for p in dst.paras):
            raise ActionError("destination_cell_not_empty", "Word selects the content; not common")
        self.ed.apply({"op": "focus", "target": dst.paras[0].nid, "offset": 0})
        self.caret_known = True

    def _a_key_tab_next_cell(self, a):
        self._tab_move(+1)
        return {"klm": KLM["K"]}

    def _a_key_shift_tab_prev_cell(self, a):
        self._tab_move(-1)
        return {"klm": 2 * KLM["K"]}

    def _a_double_click_word(self, a):
        h = self.hit_test(a["page"], a["x"], a["y"])
        if h.kind != "text" or h.container == "textbox":
            raise ActionError("hit_not_body_or_cell_text")
        txt = self.ed.text_of(h.para)
        i = min(h.offset, max(len(txt) - 1, 0))
        if not txt or txt[i].isspace():
            raise ActionError("no_word_at_point")
        s = i
        while s > 0 and not txt[s - 1].isspace():
            s -= 1
        e = i
        while e < len(txt) and not txt[e].isspace():
            e += 1
        self.ed.apply({"op": "focus", "target": h.para, "offset": s})
        self.ed.apply({"op": "select_range", "start": s, "end": e})
        self.caret_known = True
        return {"klm": KLM["P"] + 2 * KLM["BB"]}

    def _a_drag_select_text(self, a):
        h0, h1 = self.hit_test(*a["from"]), self.hit_test(*a["to"])
        if h0.kind != "text" or h1.kind != "text" or h0.para != h1.para:
            raise ActionError("drag_not_within_one_paragraph")
        s, e = sorted((h0.offset, h1.offset))
        if s == e:
            raise ActionError("empty_selection")
        self.ed.apply({"op": "focus", "target": h0.para, "offset": s})
        self.ed.apply({"op": "select_range", "start": s, "end": e})
        self.caret_known = True
        return {"klm": 2 * KLM["P"] + 2 * KLM["B"]}

    def _a_key_shift_arrow(self, a):
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        n, d = int(a.get("count", 1)), a.get("direction", "left")
        p, off = self.ed.caret
        sel = self.ed.selection
        s, e = (sel[1], sel[2]) if sel else (off, off)
        anchor_left = sel is None or off == e
        if d == "left":
            s, e = (s - n, e) if anchor_left else (s, e - n)
        else:
            s, e = (s, e + n) if anchor_left or sel is None else (s + n, e)
        if s < 0 or e > self._text_len(p) or s >= e:
            raise ActionError("selection_out_of_paragraph")
        self.ed.apply({"op": "select_range", "start": s, "end": e})
        return {"actions": n, "klm": n * KLM["K"]}

    def _a_input_value(self, a):
        vid = a["value_id"]
        if vid not in self.values:
            raise ActionError("value_not_in_contract", vid)
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        full = self.values[vid]
        s, e = a.get("segment") or (0, len(full))
        if s != self.next_seg.get(vid, 0) or not s < e <= len(full):
            raise ActionError("segment_out_of_order", f"{vid} expects start {self.next_seg.get(vid, 0)}")
        text = full[s:e]
        kind, holder = self._container_of(self.ed.caret[0])
        before = self._fit_state(kind, holder)
        if self.ed.selection is not None:              # typing replaces the selection (common, kb typing_replaces_selection)
            self.ed.apply({"op": "delete_selection"})
        self.ed.apply({"op": "type_text", "text": text})
        self._check_no_clipping(kind, holder, before)
        self.next_seg[vid] = e
        return {"klm": KLM["M"] + KLM["K"] * keystrokes(text)}

    def _fit_state(self, kind, holder):
        """Height of the text box and the set of clipped fragments of the container, before an input."""
        lay = self.layout
        nid = getattr(holder, "nid", None)
        h = None
        if kind == "textbox":
            b = next((b for b in lay.boxes if b.kind == "textbox" and b.nid == nid), None)
            h = b.h if b is not None else None
        clipped = sum(1 for f in lay.frags if f.clipped and (f.owner == nid or nid is None))
        return h, clipped

    def _check_no_clipping(self, kind, holder, before=(None, 0)):
        """The input must not make a text box grow (growth differs between editors) nor clip text that was visible;
        compared with the state before the input (a box that already overflowed while empty is not the input's doing)."""
        h0, c0 = before
        h1, c1 = self._fit_state(kind, holder)
        if kind == "textbox" and h0 is not None and h1 is not None and h1 > h0 + 0.5:
            raise ActionError("textbox_would_grow_or_clip", "text box growth differs between editors")
        if c1 > c0:
            raise ActionError("text_clipped")

    def _a_key_ctrl_backspace(self, a):
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        if self._container_of(self.ed.caret[0])[0] == "cell":
            raise ActionError("caret_in_table", "Ctrl+BackSpace deletes the row in Hangul tables")
        n = int(a.get("count", 1))
        for _ in range(n):
            p, off = self.ed.caret
            txt = self.ed.text_of(p)
            s = off
            while s > 0 and txt[s - 1].isspace():
                s -= 1
            while s > 0 and not txt[s - 1].isspace():
                s -= 1
            if s == off:
                raise ActionError("nothing_to_delete")
            self.ed.apply({"op": "select_range", "start": s, "end": off})
            self.ed.apply({"op": "delete_selection"})
        return {"actions": n, "klm": n * 2 * KLM["K"]}

    def _a_key_delete(self, a):
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        key, n = a.get("key", "Delete"), int(a.get("count", 1))
        for i in range(n):
            if self.ed.selection is not None:          # one press removes the selection
                self.ed.apply({"op": "delete_selection"})
                continue
            p, off = self.ed.caret
            ln = self._text_len(p)
            s, e = (off, off + 1) if key == "Delete" else (off - 1, off)
            if s < 0 or e > ln:
                raise ActionError("at_paragraph_boundary", "joining paragraphs by Delete/BackSpace is not admitted")
            self.ed.apply({"op": "select_range", "start": s, "end": e})
            self.ed.apply({"op": "delete_selection"})
        return {"actions": n, "klm": n * KLM["K"]}

    def _a_key_enter(self, a):
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        if self.ed.selection is not None:
            raise ActionError("text_selection_active", "Enter over a selection is not in the knowledge base")
        p, off = self.ed.caret
        self.ed.apply({"op": "split_paragraph"})
        return {"klm": KLM["K"]}

    def _a_key_arrow(self, a):
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        n = int(a.get("count", 1)) * (-1 if a.get("direction", "left") == "left" else 1)
        p, off = self.ed.caret
        if not 0 <= off + n <= self._text_len(p):
            raise ActionError("leaves_paragraph", "arrow movement across paragraphs / cells is not admitted")
        self.ed.apply({"op": "move_caret", "offset": off + n})
        return {"actions": abs(n), "klm": abs(n) * KLM["K"]}

    def _a_key_home_end(self, a):
        if not self.caret_known or self.ed.caret is None:
            raise ActionError("caret_unknown")
        p, _ = self.ed.caret
        lines = {round(f.y, 1) for f in self.layout.frags if f.para == p and not f.generated}
        if len(lines) > 1:
            raise ActionError("multi_line_paragraph", "line ends depend on the renderer")
        self.ed.apply({"op": "move_caret", "offset": 0 if a.get("key", "Home") == "Home" else self._text_len(p)})
        return {"klm": KLM["K"]}

    def _a_key_tab_last_cell(self, a):
        tc = self._cell_of_caret()
        if tc is None:
            raise ActionError("caret_not_in_cell")
        t, c = tc
        if self._reading_order(t)[-1] is not c:
            raise ActionError("not_last_cell")
        self.ed.apply({"op": "table_insert_row", "table": t.nid, "after_row": c.row + c.rowspan - 1})
        self.caret_known = False
        return {"klm": KLM["K"]}

    def _a_cmd_insert_row_below(self, a):
        tc = self._cell_of_caret()
        if tc is None:
            raise ActionError("caret_not_in_cell")
        t, c = tc
        self.ed.apply({"op": "table_insert_row", "table": t.nid, "after_row": c.row + c.rowspan - 1})
        self.caret_known = False
        return {"klm": KLM["M"] + 2 * (KLM["P"] + KLM["BB"])}

    def _a_cmd_delete_row(self, a):
        tc = self._cell_of_caret()
        if tc is None:
            raise ActionError("caret_not_in_cell")
        t, c = tc
        self.ed.apply({"op": "table_delete_row", "table": t.nid, "row": c.row})
        self.caret_known = False
        return {"klm": KLM["M"] + 2 * (KLM["P"] + KLM["BB"])}

    def _a_cmd_split_cell(self, a):
        tc = self._cell_of_caret()
        if tc is None:
            raise ActionError("caret_not_in_cell")
        t, c = tc
        if c.rowspan == 1 and c.colspan == 1:          # the backend splits (unmerges) merged cells only
            raise ActionError("unsupported_operation", "splitting an unmerged cell is not implemented by the backend")
        self.ed.apply({"op": "table_split_cell", "table": t.nid, "row": c.row, "col": c.col})
        self.caret_known = False
        return {"klm": KLM["M"] + 2 * (KLM["P"] + KLM["BB"]) + 2 * KLM["K"] + KLM["BB"]}

    def _a_drag_object(self, a):
        h = self.hit_test(*a["object"])
        if h.kind != "object":
            raise ActionError("hit_not_object")
        pg, x, y = a["object"]
        tp, tx, ty = a["to"]
        if tp != pg:
            raise ActionError("cross_page_drag_not_supported")
        self.ed.apply({"op": "select_objects", "targets": [h.obj]})
        self.ed.apply({"op": "move_selection", "dx": tx - x, "dy": ty - y})
        self.caret_known = False
        return {"klm": 2 * KLM["P"] + 2 * KLM["B"]}

    def _a_key_ctrl_b(self, a):
        if self.ed.selection is None:
            raise ActionError("no_text_selection")
        self.ed.apply({"op": "set_char_format", "props": {"bold": True}})
        return {"klm": 2 * KLM["K"]}

    def _a_key_ctrl_z(self, a):
        self.ed.apply({"op": "undo"})
        self.caret_known = False
        return {"klm": 2 * KLM["K"]}

    def _a_key_ctrl_tab(self, a):
        if self._cell_of_caret() is None:
            raise ActionError("caret_not_in_cell")
        self.ed.apply({"op": "type_text", "text": "\t"})
        return {"klm": 2 * KLM["K"]}

    def clone(self) -> HumanSession:
        """Independent copy (planner search)."""
        h = copy.copy(self)
        h.ed = copy.deepcopy(self.ed)
        h.next_seg = dict(self.next_seg)
        h.log = list(self.log)
        h.assumptions = set(self.assumptions)
        return h
