"""Semantic Document IR. Every node keeps ``nid`` (stable session id) and ``el`` (the source lxml element),
so IR nodes can be traced back to the XML node path and forward to rendered regions (layout fragments)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HWPUNIT_PER_PT = 100.0          # 1 pt = 100 HWPUNIT (1 HWPUNIT = 1/7200 inch)


def hu(v: Any, default: float = 0.0) -> float:
    """HWPUNIT attribute -> points. Handles unsigned wrap-around of negative 32-bit values."""
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return default
    if x > 0x7FFFFFFF:
        x -= 0x100000000
    return x / HWPUNIT_PER_PT


@dataclass
class CharStyle:
    font: str = "함초롬바탕"
    latin_font: str = "함초롬바탕"
    size: float = 10.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: str = "#000000"
    ratio: float = 100.0
    spacing: float = 0.0
    ref: str | None = None

    def semantic_key(self) -> tuple:
        return (self.font, self.latin_font, round(self.size, 2), self.bold, self.italic, self.underline, self.strike,
                self.color.upper(), self.ratio, self.spacing)


@dataclass
class ParaStyle:
    align: str = "JUSTIFY"
    left: float = 0.0
    right: float = 0.0
    indent: float = 0.0
    prev: float = 0.0
    next: float = 0.0
    ls_type: str = "PERCENT"
    ls_value: float = 160.0
    heading: str = "NONE"
    heading_level: int = 0
    heading_id: str = "0"
    ref: str | None = None

    def semantic_key(self) -> tuple:
        return (self.align, self.left, self.right, self.indent, self.prev, self.next, self.ls_type, self.ls_value,
                self.heading, self.heading_level)


@dataclass
class Border:
    type: str = "NONE"
    width: float = 0.34           # pt
    color: str = "#000000"

    @property
    def visible(self) -> bool:
        return self.type not in ("NONE", "")


@dataclass
class BorderFill:
    left: Border = field(default_factory=Border)
    right: Border = field(default_factory=Border)
    top: Border = field(default_factory=Border)
    bottom: Border = field(default_factory=Border)
    fill: str | None = None
    diagonal: bool = False

    def semantic_key(self) -> tuple:
        return tuple((b.type, round(b.width, 2), b.color.upper()) for b in (self.left, self.right, self.top, self.bottom)) + (self.fill,)


@dataclass
class ObjPos:
    treat_as_char: bool = True
    vert_rel: str = "PARA"
    horz_rel: str = "COLUMN"
    vert_align: str = "TOP"
    horz_align: str = "LEFT"
    vert_off: float = 0.0
    horz_off: float = 0.0
    wrap: str = "TOP_AND_BOTTOM"
    flow_with_text: bool = True
    allow_overlap: bool = False
    z: int = 0
    margin: tuple = (0.0, 0.0, 0.0, 0.0)    # outMargin l, r, t, b


@dataclass
class Inline:
    kind: str                 # text | tab | br | obj | field_begin | field_end | ctrl | opaque
    text: str = ""
    cs: CharStyle | None = None
    obj: Any = None
    el: Any = None
    meta: dict = field(default_factory=dict)


@dataclass
class Para:
    nid: str
    el: Any
    ps: ParaStyle
    inlines: list[Inline] = field(default_factory=list)
    page_break: bool = False
    column_break: bool = False
    default_cs: CharStyle | None = None
    parent: Any = None

    @property
    def text(self) -> str:
        return "".join(i.text for i in self.inlines if i.kind in ("text", "tab", "br"))

    def objects(self) -> list:
        return [i.obj for i in self.inlines if i.kind == "obj" and i.obj is not None]


@dataclass
class Cell:
    nid: str
    el: Any
    row: int
    col: int
    rowspan: int
    colspan: int
    width: float
    height: float
    margin: tuple = (5.1, 5.1, 1.41, 1.41)          # l, r, t, b in pt
    valign: str = "CENTER"
    bf: BorderFill = field(default_factory=BorderFill)
    paras: list[Para] = field(default_factory=list)
    header: bool = False
    protect: bool = False
    table: Any = None

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paras)


@dataclass
class Table:
    nid: str
    el: Any
    n_rows: int
    n_cols: int
    cells: list[Cell] = field(default_factory=list)
    pos: ObjPos = field(default_factory=ObjPos)
    width: float = 0.0
    height: float = 0.0
    cell_spacing: float = 0.0
    in_margin: tuple = (5.1, 5.1, 1.41, 1.41)
    repeat_header: bool = False
    page_break: str = "CELL"
    kind: str = "tbl"
    parent: Any = None

    def cell_at(self, row: int, col: int) -> Cell | None:
        for c in self.cells:
            if c.row <= row < c.row + c.rowspan and c.col <= col < c.col + c.colspan:
                return c
        return None

    def merged_cells(self) -> list[Cell]:
        return [c for c in self.cells if c.rowspan > 1 or c.colspan > 1]


@dataclass
class Shape:
    nid: str
    el: Any
    kind: str                    # rect | line | ellipse | arc | polygon | curve | connectLine | pic | container | ole | equation | textart | checkBtn | radioBtn | btn | comboBox | edit | listBox | scrollBar | video | chart | unknown
    pos: ObjPos = field(default_factory=ObjPos)
    width: float = 0.0
    height: float = 0.0
    size_protect: bool = False
    paras: list[Para] | None = None          # text box content (drawText)
    text_margin: tuple = (2.83, 2.83, 2.83, 2.83)
    valign: str = "TOP"
    line: Border = field(default_factory=Border)
    fill: str | None = None
    bin_ref: str | None = None
    points: list[tuple] = field(default_factory=list)
    children: list = field(default_factory=list)
    props: dict = field(default_factory=dict)
    parent: Any = None

    @property
    def is_textbox(self) -> bool:
        return self.paras is not None


@dataclass
class PageSetup:
    width: float = 595.28
    height: float = 841.88
    left: float = 85.04
    right: float = 85.04
    top: float = 56.68
    bottom: float = 42.52
    header: float = 42.52
    footer: float = 42.52
    gutter: float = 0.0
    landscape: bool = False
    col_count: int = 1
    col_gap: float = 0.0

    @property
    def body(self) -> tuple[float, float, float, float]:
        return (self.left + self.gutter, self.top + self.header, self.width - self.right, self.height - self.bottom - self.footer)


@dataclass
class Section:
    index: int
    el: Any
    page: PageSetup
    paras: list[Para] = field(default_factory=list)
    headers: list[tuple[str, list[Para]]] = field(default_factory=list)
    footers: list[tuple[str, list[Para]]] = field(default_factory=list)
    page_num: dict | None = None
    notes: list[tuple[str, list[Para]]] = field(default_factory=list)


@dataclass
class DocIR:
    sections: list[Section] = field(default_factory=list)
    nodes: dict[str, Any] = field(default_factory=dict)
    opaque: list[dict] = field(default_factory=list)
    fonts: dict[str, list[str]] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def paras(self, deep: bool = True):
        """All paragraphs in document order (optionally descending into tables / text boxes / headers)."""
        def walk(ps):
            for p in ps:
                yield p
                if deep:
                    for o in p.objects():
                        yield from walk_obj(o)

        def walk_obj(o):
            if isinstance(o, Table):
                for c in o.cells:
                    yield from walk(c.paras)
            elif isinstance(o, Shape):
                if o.paras:
                    yield from walk(o.paras)
                for ch in o.children:
                    yield from walk_obj(ch)
        for s in self.sections:
            yield from walk(s.paras)
            if deep:
                for _, ps in s.headers + s.footers + s.notes:
                    yield from walk(ps)

    def objects(self):
        for p in self.paras():
            yield from p.objects()

    def tables(self) -> list[Table]:
        return [o for o in self.objects() if isinstance(o, Table)]

    def shapes(self) -> list[Shape]:
        out = []
        for o in self.objects():
            if isinstance(o, Shape):
                out.append(o)
                stack = list(o.children)
                while stack:
                    c = stack.pop()
                    if isinstance(c, Shape):
                        out.append(c)
                        stack.extend(c.children)
        return out
