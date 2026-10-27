"""A publisher 'example' must actually be filled in (review finding 2026-10-15)."""
from formbench.corpus.fillcheck import check

BLANK = "営農計画書 氏名 住所 電話番号 経営面積 作付品目 令和 年 月 日 申請者 印"


def test_stamp_only_example_is_not_filled():
    assert check(BLANK, "記入例 " + BLANK)["verdict"] == "no_fill"
    assert check(BLANK, BLANK)["verdict"] == "no_fill"


def test_guidance_sentences_are_not_values():
    ex = BLANK.replace("氏名", "氏名 申請者の氏名を記入してください").replace("住所", "住所 住民票の住所を記入してください（※番地まで）")
    assert check(BLANK, ex)["verdict"] == "guidance_only"


def test_typed_values_are_filled_even_when_short():
    ex = BLANK.replace("氏名", "氏名 山田花子").replace("電話番号", "電話番号 0123-45-6789").replace("令和 年 月 日", "令和7年4月1日")
    c = check(BLANK, ex)
    assert c["verdict"] == "filled" and c["value_chars"] > 10
    assert check("Name: ____ Date: ____", "Name: Jane Citizen Date: 01/04/2026 Ref: AB12345")["verdict"] == "filled"
