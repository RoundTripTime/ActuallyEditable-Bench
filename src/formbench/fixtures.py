"""Deterministic synthetic HWPX documents used by tests, operator test cases, capability probes and metric
sensitivity experiments. All are generated with HwpxBuilder; none contains real personal data."""
from __future__ import annotations

from lxml import etree

from .formats.hwpx import HP
from .formats.hwpx_build import HwpxBuilder, X, make_line, make_p, make_pic, make_table, make_textbox

LEFT, TOP = 56.7, 70.8          # body origin of the default builder page (margins 56.7 / 42.5 + header 28.3)
BODY_W = 595.28 - 2 * 56.7


def _styles(b: HwpxBuilder) -> dict:
    return {"body": b.head.char_pr(size=10), "title": b.head.char_pr(size=16, bold=True), "small": b.head.char_pr(size=9),
            "left": b.head.para_pr(align="LEFT", line_spacing=160), "center": b.head.para_pr(align="CENTER", line_spacing=160),
            "bf": b.head.border_fill(), "bf_fill": b.head.border_fill(fill="#E5E5E5"), "bf_none": b.head.border_fill(False, False, False, False)}


def _tbl(b, st, col_widths, row_heights, cells, **kw):
    for c in cells:
        if "text" in c:
            c["paras"] = [make_p(c.pop("text"), st["body"], st["center"] if c.pop("center", False) else st["left"])]
    return make_table(b.next_id(), col_widths, row_heights, cells, st["bf"], char_id=st["body"], para_id=st["left"], **kw)


def simple_form() -> bytes:
    """Title, 4-row label/value table, closing sentence."""
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("지 원 서", st["title"], st["center"])
    labels = ["성명", "생년월일", "주소", "연락처"]
    cells = [{"row": i, "col": 0, "text": lab, "center": True, "bf": st["bf_fill"]} for i, lab in enumerate(labels)]
    b.attach(_tbl(b, st, [110, BODY_W - 110], [26] * 4, cells), b.add_para("", st["body"], st["left"]))
    b.add_para("위와 같이 지원합니다.", st["body"], st["left"])
    b.add_para("", st["body"], st["left"])
    return b.to_bytes()


def merged_form() -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("신 청 서", st["title"], st["center"])
    cells = [{"row": 0, "col": 0, "rowspan": 2, "text": "신청인", "center": True, "bf": st["bf_fill"]},
             {"row": 0, "col": 1, "text": "성명", "center": True}, {"row": 1, "col": 1, "text": "주소", "center": True},
             {"row": 2, "col": 0, "colspan": 2, "text": "신청 사유", "center": True, "bf": st["bf_fill"]}]
    b.attach(_tbl(b, st, [70, 80, BODY_W - 150], [26, 26, 60], cells), b.add_para("", st["body"], st["left"]))
    b.add_para("첨부: 증빙서류 1부", st["body"], st["left"])
    return b.to_bytes()


def long_text_form(placeholder: str = "") -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("자기소개서", st["title"], st["center"])
    cells = [{"row": 0, "col": 0, "text": "성명", "center": True, "bf": st["bf_fill"]},
             {"row": 1, "col": 0, "text": "지원 동기", "center": True, "bf": st["bf_fill"]}]
    if placeholder:
        cells.append({"row": 1, "col": 1, "text": placeholder, "valign": "TOP"})
    else:
        cells.append({"row": 1, "col": 1, "valign": "TOP"})
    b.attach(_tbl(b, st, [110, BODY_W - 110], [26, 80], cells), b.add_para("", st["body"], st["left"]))
    b.add_para("※ 기재 내용이 사실과 다를 경우 불이익을 받을 수 있습니다.", st["small"], st["left"])
    return b.to_bytes()


def repeat_form(rows: int = 2, trailing: bool = True) -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("경력 사항", st["title"], st["center"])
    cells = [{"row": 0, "col": k, "text": t, "center": True, "bf": st["bf_fill"], "header": True} for k, t in enumerate(["기간", "근무처", "담당 업무"])]
    b.attach(_tbl(b, st, [120, 160, BODY_W - 280], [24] * (rows + 1), cells), b.add_para("", st["body"], st["left"]))
    if trailing:
        b.add_para("위 기재 사항은 사실과 다름없음을 확인합니다.", st["body"], st["left"])
        b.add_para("작성자:              (서명)", st["body"], st["left"])
    return b.to_bytes()


def textbox_form(n_boxes: int = 1) -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    p = b.add_para("신청 내용", st["body"], st["left"])
    y = 140.0
    b.attach(make_textbox(b.next_id(), LEFT, y - 30, 80, 22, [make_p("성명", st["body"], st["left"])], border=False), p)
    for i in range(n_boxes):
        b.attach(make_textbox(b.next_id(), LEFT + 90, y - 30 + i * 26, 220, 22, [make_p("", st["body"], st["left"])], border=True), p)
    b.add_para("", st["body"], st["left"])
    return b.to_bytes()


def field_form() -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("참가 신청", st["title"], st["center"])
    p = b.add_para("성명: ", st["body"], st["left"])
    run = [r for r in p if r.tag == HP + "run"][-1]
    run.append(X('<hp:ctrl><hp:fieldBegin id="900001" type="CLICK_HERE" name="" editable="1" dirty="0" zorder="-1" fieldid="627272811">'
                 '<hp:parameters cnt="2" name=""><hp:integerParam name="Prop">9</hp:integerParam>'
                 '<hp:stringParam name="Command" xml:space="preserve">Clickhere:set:26:Direction:wstring:2:이름 HelpState:wstring:0: </hp:stringParam>'
                 '</hp:parameters></hp:fieldBegin></hp:ctrl>'))
    run.append(X('<hp:ctrl><hp:fieldEnd beginIDRef="900001" fieldid="627272811"/></hp:ctrl>'))
    etree.SubElement(run, HP + "t").text = " (인)"
    b.add_para("소속: __________", st["body"], st["left"])
    return b.to_bytes()


def checkbox_form() -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("개인정보 수집 동의", st["title"], st["center"])
    b.add_para("□ 동의함   □ 동의하지 않음", st["body"], st["left"])
    p = b.add_para("", st["body"], st["left"])
    run = [r for r in p if r.tag == HP + "run"][-1]
    t = run.find(HP + "t")
    t.addprevious(X('<hp:checkBtn caption="뉴스레터 수신" value="UNCHECKED" radioGroupName="" triState="0" backStyle="TRANSPARENT" name="CheckBox1" '
                    'foreColor="#000000" backColor="#FFFFFF" groupName="" tabStop="1" editable="1" tabOrder="1" enabled="1" borderTypeIDRef="0" '
                    'drawFrame="0" printable="1" command=""><hp:formCharPr charPrIDRef="0" followContext="0" autoSz="0" wordWrap="0"/>'
                    '<hp:sz width="11338" widthRelTo="ABSOLUTE" height="1500" heightRelTo="ABSOLUTE" protect="0"/>'
                    '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="1" holdAnchorAndSO="0" vertRelTo="PARA" '
                    'horzRelTo="COLUMN" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
                    '<hp:outMargin left="0" right="0" top="0" bottom="0"/></hp:checkBtn>'))
    return b.to_bytes()


def paragraph_field_form() -> bytes:
    """Label and blank in ordinary paragraphs (the 'normal paragraph' variant of test #3)."""
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("확 인 서", st["title"], st["center"])
    b.add_para("성명:", st["body"], st["left"])
    b.add_para("", st["body"], st["left"])
    b.add_para("위 사실을 확인합니다.", st["body"], st["left"])
    return b.to_bytes()


def textbox_field_form() -> bytes:
    """Same visible form as paragraph_field_form but the blank is an (unbordered) text box (test #3)."""
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("확 인 서", st["title"], st["center"])
    p = b.add_para("성명:", st["body"], st["left"])
    b.attach(make_textbox(b.next_id(), LEFT, TOP + 16 * 1.6 + 10 * 1.6, BODY_W, 16, [make_p("", st["body"], st["left"])], border=False, margin=0.0), p)
    b.add_para("", st["body"], st["left"])
    b.add_para("위 사실을 확인합니다.", st["body"], st["left"])
    return b.to_bytes()


def fragmented_longtext_form(n_lines: int = 4, locked: bool = True, as_single_box: bool = False) -> bytes:
    """A long-text area rebuilt as ``n_lines`` separate one-line text boxes at absolute positions, followed by a
    protected sentence directly below (typical layout-preserving reconstruction; test #4)."""
    b = HwpxBuilder()
    st = _styles(b)
    p = b.add_para("", st["body"], st["left"])
    b.attach(make_textbox(b.next_id(), LEFT, 80, 200, 18, [make_p("지원 동기", st["body"], st["left"])], border=False, margin=1.0), p)
    line_h, w = 16.0, BODY_W
    if as_single_box:
        b.attach(make_textbox(b.next_id(), LEFT, 102, w, line_h * n_lines, [make_p("", st["body"], st["left"])], border=False, margin=1.0), p)
    else:
        for i in range(n_lines):
            b.attach(make_textbox(b.next_id(), LEFT, 102 + i * line_h, w, line_h, [make_p("", st["body"], st["left"])], border=False, margin=1.0,
                                  protect=locked), p)
    b.attach(make_textbox(b.next_id(), LEFT, 102 + n_lines * line_h + 2, w, 18,
                          [make_p("※ 기재 내용이 사실과 다를 경우 불이익을 받을 수 있습니다.", st["small"], st["left"])], border=False, margin=1.0), p)
    return b.to_bytes()


def drawn_table_form(rows: int = 2, trailing: bool = True) -> bytes:
    """Looks like repeat_form but the grid is line shapes + one text box per cell (no native table; test #5)."""
    b = HwpxBuilder()
    st = _styles(b)
    p = b.add_para("", st["body"], st["left"])
    b.attach(make_textbox(b.next_id(), LEFT, 72, BODY_W, 26, [make_p("경력 사항", st["title"], st["center"])], border=False, margin=1.0), p)
    cols = [120, 160, BODY_W - 280]
    xs = [LEFT, LEFT + 120, LEFT + 280, LEFT + BODY_W]
    y0, rh = 112.0, 24.0
    for r in range(rows + 2):
        b.attach(make_line(b.next_id(), xs[0], y0 + r * rh, xs[-1], y0 + r * rh), p)
    for x in xs:
        b.attach(make_line(b.next_id(), x, y0, x, y0 + (rows + 1) * rh), p)
    for k, t in enumerate(["기간", "근무처", "담당 업무"]):
        b.attach(make_textbox(b.next_id(), xs[k] + 1, y0 + 1, cols[k] - 2, rh - 2, [make_p(t, st["body"], st["center"])], border=False, margin=1.0,
                              valign="CENTER"), p)
    for r in range(1, rows + 1):
        for k in range(3):
            b.attach(make_textbox(b.next_id(), xs[k] + 1, y0 + r * rh + 1, cols[k] - 2, rh - 2, [make_p("", st["body"], st["left"])], border=False,
                                  margin=1.0, valign="CENTER"), p)
    if trailing:
        yb = y0 + (rows + 1) * rh + 8
        b.attach(make_textbox(b.next_id(), LEFT, yb, BODY_W, 18, [make_p("위 기재 사항은 사실과 다름없음을 확인합니다.", st["body"], st["left"])],
                              border=False, margin=1.0), p)
        b.attach(make_textbox(b.next_id(), LEFT, yb + 20, BODY_W, 18, [make_p("작성자:              (서명)", st["body"], st["left"])],
                              border=False, margin=1.0), p)
    return b.to_bytes()


def picture_form(png: bytes, px: tuple[int, int] = (10, 10)) -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("사진 부착", st["title"], st["center"])
    p = b.add_para("", st["body"], st["left"])
    b.attach(make_pic(b.next_id(), b.add_bin(png, "png"), 0, 0, 90, 110, px=px, inline=True), p)
    b.add_para("서명:", st["body"], st["left"])
    return b.to_bytes()


def multipage_form(n_rows: int = 60) -> bytes:
    b = HwpxBuilder()
    st = _styles(b)
    b.add_para("점 검 표", st["title"], st["center"])
    cells = [{"row": 0, "col": 0, "text": "번호", "center": True, "bf": st["bf_fill"]}, {"row": 0, "col": 1, "text": "점검 항목", "center": True, "bf": st["bf_fill"]},
             {"row": 0, "col": 2, "text": "결과", "center": True, "bf": st["bf_fill"]}]
    cells += [{"row": i, "col": 0, "text": str(i), "center": True} for i in range(1, n_rows + 1)]
    cells += [{"row": i, "col": 1, "text": f"점검 항목 {i}"} for i in range(1, n_rows + 1)]
    b.attach(_tbl(b, st, [50, BODY_W - 150, 100], [22] * (n_rows + 1), cells), b.add_para("", st["body"], st["left"]))
    b.add_para("점검자:", st["body"], st["left"])
    return b.to_bytes()


def tiny_png(w: int = 10, h: int = 10, rgb=(40, 40, 40)) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, format="PNG")
    return buf.getvalue()


def signature_png() -> bytes:
    """Fully synthetic 'signature' stroke image (deterministic)."""
    import io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (120, 60), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.line([(8, 40), (30, 12), (46, 44), (64, 16), (84, 46), (110, 20)], fill=(20, 20, 90), width=3)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


FIXTURES = {
    "simple_form": simple_form, "merged_form": merged_form, "long_text_form": long_text_form, "repeat_form": repeat_form,
    "textbox_form": textbox_form, "field_form": field_form, "checkbox_form": checkbox_form,
    "paragraph_field_form": paragraph_field_form, "textbox_field_form": textbox_field_form,
    "fragmented_longtext_form": fragmented_longtext_form, "drawn_table_form": drawn_table_form, "multipage_form": multipage_form,
}


def build(name: str, **kw) -> bytes:
    return FIXTURES[name](**kw)
