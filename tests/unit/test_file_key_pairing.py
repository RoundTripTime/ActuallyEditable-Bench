"""Template sites label every link 'Download DOCX': the file names pair blank and example (2026-10-15)."""
from formbench.collection.search import PairedAttachmentProvider, file_key


def test_file_key_strips_roles_and_hashes():
    assert file_key("https://x.org/d/freelance-invoice-blank-1239dda63a0d.docx") == "freelanceinvoice"
    assert file_key("https://x.org/d/freelance-invoice-example-3c17863b483a.docx") == "freelanceinvoice"
    assert file_key("https://x.org/files/20261015123.hwpx") == ""          # numeric names carry no form name


def test_generic_download_links_pair_by_file_name():
    html = b"""<html><body><ul>
    <li><a href="/d/purchase-order-blank-aa11bb22.docx" aria-label="Blank template">Download DOCX</a></li>
    <li><a href="/d/purchase-order-example-cc33dd44.docx" aria-label="Worked example">Download DOCX</a></li>
    <li><a href="/d/purchase-order-hourly-blank-ee55ff66.docx">Download DOCX</a></li>
    </ul></body></html>"""
    pairs = PairedAttachmentProvider.pairs_on_page(html, "https://x.org/t/", set(), {"docx"})
    got = sorted((h.url.rsplit("/", 1)[-1], h.extra["pair_links"][0]["role"]) for h in pairs)
    assert got == [("purchase-order-blank-aa11bb22.docx", "blank"), ("purchase-order-example-cc33dd44.docx", "example")]


def test_canonical_post_url_drops_list_state_only_on_posts():
    from formbench.collection.search import canonical_post_url as c
    assert c("https://x.go.kr/ex/bbs/View.do?pageIndex=1&pageUnit=10&cbIdx=57&bcIdx=404453&searchCondition=subCont") == \
        "https://x.go.kr/ex/bbs/View.do?cbIdx=57&bcIdx=404453"
    assert c("https://x.go.kr/bbs/List.do?bbsId=1&pageIndex=3") == "https://x.go.kr/bbs/List.do?bbsId=1&pageIndex=3"
    assert c("https://a.kr/p?page=2") == "https://a.kr/p?page=2"


def test_board_attachment_format_from_icon():
    html = """<table><tr><td><a href="/boardFileDown.do?id=54&no=1" title="x 파일 다운로드"><img src="/i/file_hwpx.gif" alt="hwpx 파일">
    연기신청서(작성예시)</a></td></tr><tr><td><a href="/boardFileDown.do?id=54&no=2"><img alt="hwpx 파일"> 연기신청서</a></td></tr></table>""".encode()
    pairs = PairedAttachmentProvider.pairs_on_page(html, "https://x.go.kr/board/boardView.do?no=9", set(), {"hwpx"})
    assert sorted(h.extra["pair_links"][0]["role"] for h in pairs) == ["blank", "example"]


def test_topic_named_examples_offer_all_blanks_for_content_choice():
    html = b"""<ul><li><a href="/f/qip-template.docx">QIP template for the trainee to complete (blank copy)</a></li>
    <li><a href="/f/levels.docx">QIP feedback levels and word pictures</a></li>
    <li><a href="/f/home-visit.docx">Home visit example with ES responses</a></li></ul>"""
    pairs = PairedAttachmentProvider.pairs_on_page(html, "https://x.org/qip", set(), {"docx"})
    links = [lk for h in pairs for lk in h.extra["pair_links"]]
    assert {lk["name_relation"] for lk in links} == {"content_choice"}
    assert len({lk["group"] for lk in links}) == 1 and sorted(lk["role"] for lk in links) == ["blank", "blank", "example"]
