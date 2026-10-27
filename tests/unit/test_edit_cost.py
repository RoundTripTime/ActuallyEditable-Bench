"""edit block edit cost (metric ②) and judge: basic properties."""
from formbench.editing import cost as C
from formbench.editing.blocks import Block
from formbench.editing.judge import match_glyphs


def P(text, align="LEFT", y=0.0, h=12.0, nid=None, size=10.0):
    ps = {"align": align, "left": 0.0, "right": 0.0, "indent": 0.0, "prev": 0.0, "next": 0.0, "ls": ("PERCENT", 120.0)}
    return Block("P", nid or text, text=C.norm(text) if hasattr(C, "norm") else text, raw=text, runs=[(text, (size, False, False, False, "f"))],
                 ps=ps, y=y, h=h)


def test_identical_text_costs_nothing():
    assert C.text_edit("令和８年度　申請書", "令和８年度 申請書") == 0          # white space / NFKC only


def test_one_region_is_select_plus_type():
    assert C.text_edit("氏名：", "氏名：山田太郎") == 2
    assert C.text_edit("abc def", "abc") == 2                                 # select + Delete


def test_regions_are_grouped_when_cheaper():
    # two separate gaps in one line: one selection over both and one typing (2) beats two edits (4)
    assert C.text_edit("年 月 日", "2026年 9月 1日") == 2


def test_alignment_identity_zero_and_runs():
    E = [P("a", y=0), P("b", y=20), P("c", y=40)]
    assert C.align(E, E, 12)["cost"] == 0
    # deleting three consecutive extra paragraphs is one run: select + Delete
    D = E + [P("x", y=60), P("y", y=80), P("z", y=100)]
    assert C.align(E, D, 12)["cost"] == C.C()["block"]["delete_run"]


def test_authoring_always_possible():
    E = [P("title", y=0), P("body", y=20)]
    r = C.align(E, [], 12)
    assert r["cost"] < C.INF and r["counts"].get("author") == 2


def test_alignment_change_costs_one_button():
    E, D = [P("title", align="CENTER")], [P("title", align="LEFT")]
    assert C.align(E, D, 12)["cost"] == C.C()["para_format"]["align"] + C.C()["text"]["place_caret"]


def test_glyph_matching_tolerance():
    e = [(0, "A", 100.0, 100.0), (0, "B", 110.0, 100.0)]
    d = [(0, "A", 105.0, 104.0), (0, "B", 140.0, 100.0)]
    pairs, me, md = match_glyphs(e, d, 12.0)
    assert len(pairs) == 1 and me == [(0, "B", 110.0, 100.0)] and md == [(0, "B", 140.0, 100.0)]


def test_klm_typing_grows_with_length(monkeypatch):
    # KLM seconds: typing cost grows with the keystrokes of the string, so a short fix beats retyping a paragraph
    monkeypatch.setenv("FORMBENCH_COST_UNITS", "klm")
    C.C.cache_clear()
    try:
        assert C.C()["unit"] == "seconds"
        assert C.typed("abcdefghij") > C.typed("ab")
        C._LANG["lang"] = "ko"
        assert C.keys("가나") == 2 * C.C()["keys_per_char"]["hangul"]
        long = "x" * 200
        assert C.text_edit(long, long + "y") < C.typed(long + "y")
    finally:
        monkeypatch.delenv("FORMBENCH_COST_UNITS")
        C.C.cache_clear()
        C._LANG["lang"] = "ja"


def test_batch_format_saving_for_consecutive_paragraphs():
    def Q(t, y, left):
        b = P(t, y=y)
        b.ps = dict(b.ps, left=left)
        b.x = left
        return b
    E = [Q("a", 0, 40.0), Q("b", 20, 40.0), Q("c", 40, 40.0)]
    D = [Q("a", 0, 0.0), Q("b", 20, 0.0), Q("c", 40, 0.0)]
    r = C.align(E, D, 12)
    sv = C.batch_format_saving(r["ops"], E, D, 12)
    f = C.C()["para_format"]
    assert sv == 2 * (f["dialog_open"] + f["dialog_field"] + f["dialog_ok"]) - C.C()["text"]["select_range"]
    assert C.batch_format_saving(r["ops"][:1], E, D, 12) == 0
