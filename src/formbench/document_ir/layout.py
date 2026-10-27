"""Deterministic layout engine: IR -> positioned fragments per page (the reference renderer's geometry).

Coordinates are points, origin top-left of the paper. Every text fragment keeps (paragraph nid, char offset)
so rendered regions can be traced back to IR nodes and XML. Approximations (justify, square wrap, footnote
placement, header-row repeat) are approximations (``partial``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import fonts
from .schema import Cell, DocIR, Para, Section, Shape, Table

EPS = 0.01
CHECK_GLYPHS = "□☐■☑☒▣✔✓√"


@dataclass
class Frag:
    page: int
    x: float
    y: float
    w: float
    h: float
    text: str
    para: str | None
    start: int
    xs: list[float]
    size: float
    font: str
    color: str = "#000000"
    underline: bool = False
    generated: bool = False          # numbering / page number / captions of form controls: not editable text
    clipped: bool = False
    role: str = "body"
    owner: str | None = None         # innermost container nid (cell / textbox) or None for body flow
    absolute: bool = False

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h


@dataclass
class Rule:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    width: float = 0.34
    color: str = "#000000"
    owner: str | None = None
    kind: str = "cell_border"
    style: str = "SOLID"
    absolute: bool = False


@dataclass
class Box:
    page: int
    x: float
    y: float
    w: float
    h: float
    nid: str | None
    kind: str                         # para | cell | table | textbox | pic | shape | form | page_area
    fill: str | None = None
    extra: dict = field(default_factory=dict)
    absolute: bool = False

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h


@dataclass
class Layout:
    pages: list[tuple[float, float]] = field(default_factory=list)
    frags: list[Frag] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    boxes: list[Box] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    font_faces: set = field(default_factory=set)

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    def box_of(self, nid: str) -> Box | None:
        for b in self.boxes:
            if b.nid == nid:
                return b
        return None

    def boxes_of(self, nid: str) -> list[Box]:
        return [b for b in self.boxes if b.nid == nid]

    def mark(self) -> tuple[int, int, int]:
        return len(self.frags), len(self.rules), len(self.boxes)

    def translate(self, mark: tuple[int, int, int], dx: float, dy: float, page: int | None = None) -> None:
        for f in self.frags[mark[0]:]:
            if not f.absolute:
                f.x += dx
                f.y += dy
            if page is not None:
                f.page = page
        for r in self.rules[mark[1]:]:
            if not r.absolute:
                r.x0 += dx
                r.x1 += dx
                r.y0 += dy
                r.y1 += dy
            if page is not None:
                r.page = page
        for b in self.boxes[mark[2]:]:
            if not b.absolute:
                b.x += dx
                b.y += dy
            if page is not None:
                b.page = page


@dataclass
class _Ctx:
    """Flow cursor. ``areas`` is None inside cells / text boxes (no pagination, overflow is measured)."""
    out: Layout
    page: int
    x: float
    y: float
    width: float
    bottom: float
    section: Section | None = None
    col: int = 0
    paginate: bool = False
    owner: str | None = None
    role: str = "body"
    bands: list[tuple[int, float, float]] = field(default_factory=list)     # (page, y0, y1) exclusion bands
    engine: object = None
    trailing: float = 0.0             # line-spacing gap after the last laid-out line (not part of content height)

    def skip_bands(self, h: float) -> None:
        moved = True
        while moved:
            moved = False
            for pg, y0, y1 in self.bands:
                if pg == self.page and self.y < y1 - EPS and self.y + h > y0 + EPS:
                    self.y = y1
                    moved = True

    def ensure(self, h: float) -> None:
        self.skip_bands(h)
        if not self.paginate:
            return
        guard = 0
        while self.y + h > self.bottom + 0.5 and guard < 4:
            top = self.engine.body_top(self)
            if self.y <= top + EPS:
                break                        # taller than a whole area: let it overflow rather than loop
            self.engine.next_area(self)
            self.skip_bands(h)
            guard += 1


class LayoutEngine:
    def __init__(self, ir: DocIR):
        self.ir = ir
        self.out = Layout()

    # ------------------------------------------------------------------ pages / areas
    def _areas(self, sec: Section) -> list[tuple[float, float]]:
        pg = sec.page
        x0, _, x1, _ = pg.body
        n = max(1, pg.col_count)
        w = (x1 - x0 - pg.col_gap * (n - 1)) / n
        return [(x0 + i * (w + pg.col_gap), w) for i in range(n)]

    def body_top(self, ctx: _Ctx) -> float:
        return ctx.section.page.body[1]

    def new_page(self, sec: Section) -> int:
        self.out.pages.append((sec.page.width, sec.page.height))
        self._page_section.append(sec)
        return len(self.out.pages) - 1

    def next_area(self, ctx: _Ctx) -> None:
        areas = self._areas(ctx.section)
        if ctx.col + 1 < len(areas):
            ctx.col += 1
        else:
            ctx.col = 0
            ctx.page = self.new_page(ctx.section)
        ctx.x, ctx.width = areas[ctx.col]
        ctx.y = ctx.section.page.body[1]

    def run(self) -> Layout:
        self._page_section: list[Section] = []
        for sec in self.ir.sections:
            page = self.new_page(sec)
            areas = self._areas(sec)
            ctx = _Ctx(self.out, page, areas[0][0], sec.page.body[1], areas[0][1], sec.page.body[3], section=sec, paginate=True, engine=self)
            first_page = page
            self.layout_paras(sec.paras, ctx)
            if sec.notes:
                ctx.y += 6
                self.out.rules.append(Rule(ctx.page, ctx.x, ctx.y, ctx.x + ctx.width * 0.3, ctx.y, 0.34, kind="note_line"))
                ctx.y += 3
                ctx.role = "note"
                for _, ps in sec.notes:
                    self.layout_paras(ps, ctx)
                ctx.role = "body"
            for pno in range(first_page, len(self.out.pages)):
                self._page_furniture(sec, pno, pno - first_page + 1)
        return self.out

    def _page_furniture(self, sec: Section, pno: int, number: int) -> None:
        pg = sec.page
        odd = number % 2 == 1
        for kind, groups, y in (("header", sec.headers, pg.top), ("footer", sec.footers, pg.height - pg.bottom - pg.footer)):
            for apply, ps in groups:
                if apply == "BOTH" or (apply == "ODD" and odd) or (apply == "EVEN" and not odd):
                    c = _Ctx(self.out, pno, pg.left, y, pg.width - pg.left - pg.right, y + (pg.header if kind == "header" else pg.footer),
                             section=sec, role=kind, engine=self)
                    c.page_number = number
                    self.layout_paras(ps, c)
        if sec.page_num:
            side = sec.page_num.get("side") or ""
            txt = f"{side} {number} {side}".strip() if side else str(number)
            pos = sec.page_num["pos"]
            w = fonts.text_width(txt, "sans", 10)
            x = pg.left if "LEFT" in pos else (pg.width - pg.right - w if "RIGHT" in pos else (pg.width - w) / 2)
            y = max(4.0, pg.top / 2 - 5) if "TOP" in pos else pg.height - max(pg.bottom, 24) / 2 - 5
            if pos != "NONE":
                self._emit_text(self.out, pno, x, y, txt, "sans", 10, role="page_number", generated=True)

    def _emit_text(self, out, page, x, y, text, face, size, role="body", generated=True, para=None, owner=None, color="#000000"):
        xs, cur = [0.0], 0.0
        key = "sans"
        for ch in text:
            key, adv = fonts.resolve_char(face, False, ch)
            cur += adv * size
            xs.append(cur)
        out.frags.append(Frag(page, x, y, cur, size, text, para, 0, xs, size, key, color=color, generated=generated, role=role, owner=owner))

    # ------------------------------------------------------------------ paragraphs
    def layout_paras(self, paras: list[Para], ctx: _Ctx) -> None:
        for p in paras:
            self.layout_para(p, ctx)

    def _tokens(self, p: Para, ctx: _Ctx) -> tuple[list, list]:
        toks, floats = [], []            # token: [kind, text, cs, start, w, h, fontkey, obj]
        for i in p.inlines:
            cs = i.cs or p.default_cs
            if i.kind in ("text", "tab", "br"):
                self.out.font_faces.add(cs.font)
                for k, ch in enumerate(i.text):
                    if ch == "\n":
                        toks.append(["br", ch, cs, i.meta["start"] + k, 0.0, cs.size, "sans", None])
                    elif ch == "\t":
                        toks.append(["tab", ch, cs, i.meta["start"] + k, 0.0, cs.size, "sans", None])
                    else:
                        face = cs.font if (ord(ch) > 0x2E80 or not cs.latin_font) else cs.latin_font
                        key, adv = fonts.resolve_char(face, cs.bold, ch)
                        toks.append(["ch", ch, cs, i.meta["start"] + k, adv * cs.size * cs.ratio / 100.0 + cs.size * cs.spacing / 100.0, cs.size, key, None])
            elif i.kind == "obj":
                o = i.obj
                if o.pos.treat_as_char:
                    toks.append(["obj", "", cs, i.meta["start"], o.width + o.pos.margin[0] + o.pos.margin[1], o.height + o.pos.margin[2] + o.pos.margin[3], "sans", o])
                else:
                    floats.append(o)
            elif i.kind == "ctrl" and (i.meta.get("display") or i.meta.get("name") == "autoNum"):
                disp = i.meta.get("display")
                if disp is None:
                    disp = str(getattr(ctx, "page_number", ctx.page + 1)) if i.meta.get("numType") in (None, "PAGE") else (i.meta.get("num") or "1")
                for ch in disp:
                    key, adv = fonts.resolve_char(cs.font, cs.bold, ch)
                    toks.append(["gen", ch, cs, i.meta.get("start", 0), adv * cs.size * cs.ratio / 100.0, cs.size, key, None])
        return toks, floats

    def layout_para(self, p: Para, ctx: _Ctx) -> None:
        out = ctx.out
        ps = p.ps
        if p.page_break and ctx.paginate and ctx.y > self.body_top(ctx) + EPS:
            ctx.col = len(self._areas(ctx.section))          # force a new page
            self.next_area(ctx)
        toks, floats = self._tokens(p, ctx)
        base = p.default_cs.size if p.default_cs else 10.0
        ctx.y += ps.prev
        y_start, page_start = ctx.y, ctx.page
        # floating objects anchored here
        for o in floats:
            self.place_float(o, p, ctx)
        first_x = ps.left + max(ps.indent, 0.0)
        rest_x = ps.left + max(-ps.indent, 0.0)
        avail_w = lambda first: max(ctx.width - (first_x if first else rest_x) - ps.right, 10.0)   # noqa: E731
        # --- line breaking
        lines: list[list] = []
        cur: list = []
        cur_w = 0.0
        last_break = -1
        for t in toks:
            if t[0] == "br":
                cur.append(t)
                lines.append(cur)
                cur, cur_w, last_break = [], 0.0, -1
                continue
            if t[0] == "tab":
                stop = 40.0
                t[4] = stop - ((cur_w + (first_x if not lines else rest_x)) % stop)
            lim = avail_w(not lines)
            if cur and cur_w + t[4] > lim + EPS and not (t[0] == "ch" and t[1] == " "):
                if t[0] in ("ch", "gen") and _latin(t[1]) and last_break >= 0 and last_break < len(cur) - 1 and _latin(cur[-1][1]):
                    nxt = cur[last_break + 1:]
                    cur = cur[:last_break + 1]
                    lines.append(cur)
                    cur = nxt
                else:
                    lines.append(cur)
                    cur = []
                cur_w = sum(x[4] for x in cur)
                last_break = -1
            cur.append(t)
            cur_w += t[4]
            if t[0] == "obj" or t[1] == " " or (t[0] in ("ch", "gen") and not _latin(t[1])):
                last_break = len(cur) - 1
        if cur or not lines:
            lines.append(cur)
        # --- placement
        for li, line in enumerate(lines):
            text_h = max([t[5] for t in line if t[0] != "obj"] or [base])
            obj_h = max([t[5] for t in line if t[0] == "obj"] or [0.0])
            core_h = max(text_h if any(t[0] != "obj" for t in line) or not line else 0.0, obj_h) or base
            if ps.ls_type == "PERCENT":
                adv = core_h + text_h * (ps.ls_value - 100.0) / 100.0 if obj_h > text_h else text_h * ps.ls_value / 100.0
            elif ps.ls_type == "FIXED":
                adv = max(ps.ls_value, obj_h)
            elif ps.ls_type == "BETWEEN_LINES":
                adv = core_h + ps.ls_value
            else:  # AT_LEAST
                adv = max(core_h, ps.ls_value)
            adv = max(adv, 1.0)
            # tables placed as characters may be split by rows across pages
            big_tbl = next((t[7] for t in line if t[0] == "obj" and isinstance(t[7], Table)), None)
            if big_tbl is not None and ctx.paginate and len(line) == 1:
                x0 = ctx.x + (first_x if li == 0 else rest_x)
                x0 += _align_dx(ps.align, avail_w(li == 0), line[0][4])
                self.layout_table(big_tbl, x0 + big_tbl.pos.margin[0], ctx, split=True, top_margin=big_tbl.pos.margin[2])
                ctx.y += big_tbl.pos.margin[3] + (adv - core_h)
                continue
            ctx.ensure(core_h)
            lw = sum(t[4] for t in line)
            trail = 0.0
            for t in reversed(line):
                if t[0] == "ch" and t[1] == " ":
                    trail += t[4]
                else:
                    break
            x = ctx.x + (first_x if li == 0 else rest_x) + _align_dx(ps.align, avail_w(li == 0), lw - trail)
            y = ctx.y
            run: list = []

            def flush(run=run, y=y):
                if not run:
                    return
                t0 = run[0]
                xs, c = [0.0], 0.0
                for t in run[1:]:
                    c += t[4]
                    xs.append(c)
                txt = "".join(t[1] for t in run[1:])
                cs = t0[2]
                clipped = (not ctx.paginate) and ctx.bottom < 1e8 and (y + cs.size > ctx.bottom + 0.75)
                out.frags.append(Frag(ctx.page, t0[0], y + (core_h - cs.size if obj_h > text_h else text_h - cs.size), c, cs.size, txt, p.nid, t0[3], xs, cs.size, t0[1],
                                      color=cs.color, underline=cs.underline, generated=t0[4], clipped=clipped, role=ctx.role, owner=ctx.owner))
                run.clear()
            for t in line:
                if t[0] in ("ch", "gen"):
                    gen = t[0] == "gen"
                    if run and (run[0][1] != t[6] or run[0][2] is not t[2] or run[0][4] != gen):
                        flush()
                    if not run:
                        run.append((x, t[6], t[2], t[3], gen))
                    run.append(t)
                else:
                    flush()
                    if t[0] == "obj":
                        o = t[7]
                        m = o.pos.margin
                        self.place_object(o, x + m[0], y + (core_h - t[5]) + m[2], ctx)
                x += t[4]
            flush()
            ctx.y += adv
            ctx.trailing = max(adv - core_h, 0.0)
        if not ctx.paginate or ctx.page == page_start:
            out.boxes.append(Box(page_start, ctx.x + ps.left, y_start, ctx.width - ps.left - ps.right, max(ctx.y - y_start, 0.0), p.nid, "para",
                                 extra={"owner": ctx.owner, "role": ctx.role}))
        else:
            out.boxes.append(Box(ctx.page, ctx.x + ps.left, self.body_top(ctx), ctx.width - ps.left - ps.right, max(ctx.y - self.body_top(ctx), 0.0), p.nid, "para",
                                 extra={"owner": ctx.owner, "role": ctx.role, "continued_from": page_start}))
        ctx.y += ps.next
        ctx.trailing += ps.next

    # ------------------------------------------------------------------ objects
    def place_float(self, o, p: Para, ctx: _Ctx) -> None:
        pos = o.pos
        sec = ctx.section
        pg = sec.page if sec else None
        w, h = o.width, o.height
        absolute = False
        if pos.horz_rel == "PAPER" and pg:
            bx, bw, absolute = 0.0, pg.width, True
        elif pos.horz_rel == "PAGE" and pg:
            bx, bw, absolute = pg.body[0], pg.body[2] - pg.body[0], True
        elif pos.horz_rel == "PARA":
            bx, bw = ctx.x + p.ps.left, ctx.width - p.ps.left - p.ps.right
        else:
            bx, bw = ctx.x, ctx.width
        if pos.horz_align in ("CENTER",):
            x = bx + (bw - w) / 2 + pos.horz_off
        elif pos.horz_align in ("RIGHT", "OUTSIDE"):
            x = bx + bw - w - pos.horz_off
        else:
            x = bx + pos.horz_off
        vabs = False
        if pos.vert_rel == "PAPER" and pg:
            by, bh, vabs = 0.0, pg.height, True
        elif pos.vert_rel == "PAGE" and pg:
            by, bh, vabs = pg.body[1], pg.body[3] - pg.body[1], True
        else:
            by, bh = ctx.y, 0.0
        if pos.vert_align == "CENTER" and vabs:
            y = by + (bh - h) / 2 + pos.vert_off
        elif pos.vert_align in ("BOTTOM", "OUTSIDE") and vabs:
            y = by + bh - h - pos.vert_off
        else:
            y = by + pos.vert_off
        takes_space = pos.wrap not in ("BEHIND_TEXT", "IN_FRONT_OF_TEXT")
        is_abs = absolute and vabs
        if isinstance(o, Table) and ctx.paginate and not vabs and takes_space:
            # para-anchored table in the body flow: flows and splits across pages like HWP does
            ctx.y = y
            self.layout_table(o, x, ctx, split=True, top_margin=pos.margin[2])
            ctx.y += pos.margin[3]
            return
        mark = ctx.out.mark()
        sub = _Ctx(ctx.out, ctx.page, ctx.x, ctx.y, ctx.width, ctx.bottom, section=ctx.section, owner=ctx.owner, role=ctx.role, engine=self)
        used_h = self.place_object(o, x, y, sub)
        if is_abs and not ctx.paginate:
            for coll in (ctx.out.frags[mark[0]:], ctx.out.rules[mark[1]:], ctx.out.boxes[mark[2]:]):
                for it in coll:
                    it.absolute = True
        if takes_space:
            band = (ctx.page, y - pos.margin[2], y + used_h + pos.margin[3])
            ctx.bands.append(band)
            if not vabs:
                ctx.y = max(ctx.y, band[2]) if (pos.wrap in ("TOP_AND_BOTTOM",) or w > ctx.width * 0.5) else ctx.y

    def place_object(self, o, x: float, y: float, ctx: _Ctx) -> float:
        """Draw object at (x, y). Returns the height actually used (text boxes may grow)."""
        out = ctx.out
        if isinstance(o, Table):
            sub = _Ctx(out, ctx.page, ctx.x, y, ctx.width, 1e9, section=ctx.section, owner=ctx.owner, role=ctx.role, engine=self)
            self.layout_table(o, x, sub, split=False)
            return sub.y - y
        s: Shape = o
        w, h = s.width, s.height
        if s.is_textbox:
            tm = s.text_margin
            mark = out.mark()
            inner = _Ctx(out, ctx.page, 0.0, 0.0, max(w - tm[0] - tm[1], 5.0), 1e9, section=ctx.section, owner=s.nid, role=ctx.role, engine=self)
            self.layout_paras(s.paras, inner)
            content_h = max(inner.y - inner.trailing, 0.0)
            need = content_h + tm[2] + tm[3]
            grew = False
            if need > h + 0.5:
                if s.size_protect:
                    for f in out.frags[mark[0]:]:
                        if f.y + f.h + tm[2] > h + 0.75:
                            f.clipped = True
                else:
                    grew, h = True, need
            dy = tm[2]
            if s.valign == "CENTER":
                dy += max(0.0, (h - tm[2] - tm[3] - content_h) / 2)
            elif s.valign == "BOTTOM":
                dy += max(0.0, h - tm[2] - tm[3] - content_h)
            out.translate(mark, x + tm[0], y + dy, ctx.page)
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "textbox", fill=s.fill,
                                 extra={"grew": grew, "declared_h": s.height, "content_h": content_h, "wrap": s.pos.wrap,
                                        "floating": not s.pos.treat_as_char, "border": s.line.visible, "owner": ctx.owner, "z": s.pos.z,
                                        "overflow": need > s.height + 0.5 and s.size_protect}))
            if s.line.visible:
                self._rect_rules(out, ctx.page, x, y, w, h, s.line, s.nid)
            return h
        kind = s.kind
        if kind == "pic":
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "pic", extra={"bin": s.bin_ref, "wrap": s.pos.wrap, "z": s.pos.z, "owner": ctx.owner}))
        elif kind in ("line", "connectLine") and len(s.points) == 2:
            (ax, ay), (bx, by) = s.points
            out.rules.append(Rule(ctx.page, x + ax, y + ay, x + bx, y + by, s.line.width, s.line.color, s.nid, "shape_line", s.line.type))
            out.boxes.append(Box(ctx.page, x + min(ax, bx), y + min(ay, by), abs(bx - ax), abs(by - ay), s.nid, "shape", extra={"shape": "line", "owner": ctx.owner, "wrap": s.pos.wrap, "z": s.pos.z}))
        elif kind == "rect":
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "shape", fill=s.fill, extra={"shape": "rect", "owner": ctx.owner, "border": s.line.visible, "wrap": s.pos.wrap, "z": s.pos.z}))
            if s.line.visible:
                self._rect_rules(out, ctx.page, x, y, w, h, s.line, s.nid)
        elif kind in ("polygon", "curve") and len(s.points) >= 2:
            pts = s.points
            for a, b in zip(pts, pts[1:]):
                out.rules.append(Rule(ctx.page, x + a[0], y + a[1], x + b[0], y + b[1], s.line.width, s.line.color, s.nid, "shape_line", s.line.type))
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "shape", extra={"shape": kind, "owner": ctx.owner, "wrap": s.pos.wrap, "z": s.pos.z}))
        elif kind == "container":
            for ch in s.children:
                gx, gy = ch.props.get("group_offset", (0.0, 0.0))
                self.place_object(ch, x + gx, y + gy, ctx)
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "shape", extra={"shape": "group", "owner": ctx.owner, "wrap": s.pos.wrap, "z": s.pos.z}))
        elif kind == "equation":
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "shape", extra={"shape": "equation", "owner": ctx.owner, "wrap": s.pos.wrap, "z": s.pos.z}))
            self._emit_text(out, ctx.page, x, y, s.props.get("script", "")[:200], "serif", min(max(h * 0.7, 6.0), 12.0), role="equation", owner=ctx.owner)
        elif kind in ("checkBtn", "radioBtn"):
            side = min(h, 10.0)
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "form", extra={"form": kind, "value": s.props.get("value"), "mark": (x + 1, y + (h - side) / 2, side),
                                                                          "owner": ctx.owner}))
            self._emit_text(out, ctx.page, x + side + 3, y + (h - 9) / 2, s.props.get("caption", ""), "sans", 9, role="form_caption", owner=ctx.owner)
        elif kind in ("btn", "comboBox", "edit", "listBox", "scrollBar"):
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "form", extra={"form": kind, "value": s.props.get("text") or s.props.get("caption"), "owner": ctx.owner}))
            if s.props.get("text") or s.props.get("caption"):
                self._emit_text(out, ctx.page, x + 2, y + (h - 9) / 2, s.props.get("text") or s.props.get("caption"), "sans", 9, role="form_caption", owner=ctx.owner)
        else:  # ellipse, arc, ole, chart, video, textart, unknown -> outline placeholder at the right place
            out.boxes.append(Box(ctx.page, x, y, w, h, s.nid, "shape", fill=s.fill, extra={"shape": kind, "owner": ctx.owner, "border": s.line.visible, "placeholder": kind not in ("ellipse", "arc"), "wrap": s.pos.wrap, "z": s.pos.z}))
        return h

    @staticmethod
    def _rect_rules(out, page, x, y, w, h, line, nid) -> None:
        for a in ((x, y, x + w, y), (x, y + h, x + w, y + h), (x, y, x, y + h), (x + w, y, x + w, y + h)):
            out.rules.append(Rule(page, *a, line.width, line.color, nid, "box_edge", line.type))

    # ------------------------------------------------------------------ tables
    @staticmethod
    def col_widths(t: Table) -> list[float]:
        n = t.n_cols
        w = [None] * n
        for c in t.cells:
            if c.colspan == 1 and c.col < n and c.width > 0:
                if w[c.col] is None:
                    w[c.col] = c.width
        for _ in range(3):
            for c in sorted(t.cells, key=lambda c: c.colspan):
                cols = list(range(c.col, min(c.col + c.colspan, n)))
                unknown = [j for j in cols if w[j] is None]
                if unknown:
                    known = sum(w[j] for j in cols if w[j] is not None)
                    if len(unknown) == 1 or _ == 2:
                        share = max(c.width - known, 1.0) / len(unknown)
                        for j in unknown:
                            w[j] = share
        fallback = (t.width / n) if n and t.width else 50.0
        return [x if x is not None else fallback for x in w]

    def layout_table(self, t: Table, x: float, ctx: _Ctx, split: bool, top_margin: float = 0.0) -> None:
        out = ctx.out
        cw = self.col_widths(t)
        cx = [0.0]
        for v in cw:
            cx.append(cx[-1] + v)
        # measure each cell (content laid out at origin, translated later)
        meas: dict[str, tuple] = {}
        for c in t.cells:
            w = cx[min(c.col + c.colspan, t.n_cols)] - cx[c.col]
            mark = out.mark()
            inner = _Ctx(out, ctx.page, 0.0, 0.0, max(w - c.margin[0] - c.margin[1], 4.0), 1e9, section=ctx.section, owner=c.nid, role=ctx.role, engine=self)
            self.layout_paras(c.paras, inner)
            meas[c.nid] = (mark, out.mark(), max(inner.y - inner.trailing, 0.0), w)
        rh = [0.0] * t.n_rows
        for c in t.cells:
            if c.rowspan == 1:
                rh[c.row] = max(rh[c.row], c.height, meas[c.nid][2] + c.margin[2] + c.margin[3])
        for r in range(t.n_rows):
            if rh[r] == 0.0:
                spans = [c.height / c.rowspan for c in t.cells if c.row <= r < c.row + c.rowspan]
                rh[r] = max(spans) if spans else 10.0
        for c in t.cells:
            if c.rowspan > 1:
                rows = range(c.row, min(c.row + c.rowspan, t.n_rows))
                need = max(c.height, meas[c.nid][2] + c.margin[2] + c.margin[3])
                have = sum(rh[r] for r in rows)
                if need > have + EPS:
                    rh[rows[-1]] += need - have
        # row placement with page splitting at row boundaries
        ctx.y += top_margin
        row_pos: list[tuple[int, float]] = []
        row_x: list[float] = []
        seg_start = (ctx.page, ctx.y)
        segs: list[tuple[int, float, float]] = []
        for r in range(t.n_rows):
            if split and ctx.paginate:
                block = rh[r]
                ctx.skip_bands(block)
                if ctx.y + block > ctx.bottom + 0.5 and ctx.y > self.body_top(ctx) + EPS:
                    segs.append((seg_start[0], seg_start[1], ctx.y))
                    off = x - ctx.x
                    self.next_area(ctx)
                    x = ctx.x + off                  # same horizontal offset in the new area
                    seg_start = (ctx.page, ctx.y)
            row_pos.append((ctx.page, ctx.y))
            row_x.append(x)
            ctx.y += rh[r]
        segs.append((seg_start[0], seg_start[1], ctx.y))
        # move measured content into place. Because cells were measured in table order, translate by recorded marks
        order = sorted(t.cells, key=lambda c: meas[c.nid][0])
        for c in order:
            mark, end, content_h, w = meas[c.nid]
            pg, y = row_pos[c.row]
            x = row_x[c.row]
            rows = range(c.row, min(c.row + c.rowspan, t.n_rows))
            h = sum(rh[r] for r in rows if row_pos[r][0] == pg)
            dy = c.margin[2]
            inner_h = h - c.margin[2] - c.margin[3]
            if c.valign == "CENTER":
                dy += max(0.0, (inner_h - content_h) / 2)
            elif c.valign == "BOTTOM":
                dy += max(0.0, inner_h - content_h)
            _translate_range(out, mark, end, x + cx[c.col] + c.margin[0], y + dy, pg)
            out.boxes.append(Box(pg, x + cx[c.col], y, w, h, c.nid, "cell", fill=c.bf.fill,
                                 extra={"table": t.nid, "row": c.row, "col": c.col, "rowspan": c.rowspan, "colspan": c.colspan, "content_h": content_h,
                                        "owner": ctx.owner, "split_rows": len({row_pos[r][0] for r in rows}) > 1}))
            bx0, bx1, by0, by1 = x + cx[c.col], x + cx[c.col] + w, y, y + h
            for side, seg in (("top", (bx0, by0, bx1, by0)), ("bottom", (bx0, by1, bx1, by1)), ("left", (bx0, by0, bx0, by1)), ("right", (bx1, by0, bx1, by1))):
                b = getattr(c.bf, side)
                if b.visible:
                    out.rules.append(Rule(pg, *seg, b.width, b.color, c.nid, "cell_border", b.type))
        for pg, y0, y1 in segs:
            x = next((row_x[r] for r in range(t.n_rows) if row_pos[r][0] == pg), x)
            out.boxes.append(Box(pg, x, y0, cx[-1], y1 - y0, t.nid, "table", extra={"rows": t.n_rows, "cols": t.n_cols, "owner": ctx.owner,
                                                                                    "floating": not t.pos.treat_as_char, "segments": len(segs)}))


def _translate_range(out: Layout, a, b, dx, dy, page) -> None:
    for coll, i, j in ((out.frags, a[0], b[0]), (out.rules, a[1], b[1]), (out.boxes, a[2], b[2])):
        for it in coll[i:j]:
            if it.absolute:
                it.page = page
                continue
            if isinstance(it, Rule):
                it.x0 += dx
                it.x1 += dx
                it.y0 += dy
                it.y1 += dy
            else:
                it.x += dx
                it.y += dy
            it.page = page


def _latin(ch: str) -> bool:
    return bool(ch) and ord(ch[0]) < 0x2E80 and not ch.isspace()


def _align_dx(align: str, avail: float, used: float) -> float:
    if align == "CENTER":
        return max(0.0, (avail - used) / 2)
    if align == "RIGHT":
        return max(0.0, avail - used)
    return 0.0


def layout_document(ir: DocIR) -> Layout:
    return LayoutEngine(ir).run()


__all__ = ["Layout", "Frag", "Rule", "Box", "layout_document", "LayoutEngine", "Cell", "CHECK_GLYPHS"]
