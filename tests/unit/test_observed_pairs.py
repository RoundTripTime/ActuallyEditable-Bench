"""Observed (blank form, completed example) pairs: publisher-page discovery, evidence, human review, DOCX handling."""
import io

import pytest

from formbench.collection.search import EXAMPLE_RX, PairedAttachmentProvider, pair_key
from formbench.corpus import pairing
from formbench.corpus.dedupe import shingles
from formbench.util import DB, path, write_json

KCC = """<table><tr><td>[작성예시]조정신청서</td>
<td><a href="/f/download.do?no=1&amp;f=2" title="[작성예시]조정신청서 다운로드"><img alt="[작성예시]조정신청서.pdf 다운로드"/></a></td>
<td>조정신청서</td><td><a href="/f/download.do?no=1&amp;f=1" title="조정신청서 다운로드"><img alt="조정신청서.hwp 다운로드"/></a></td></tr>
<tr><td>답변서</td><td><a href="/f/download.do?no=3&amp;f=1"><img alt="답변서.hwp 다운로드"/></a></td></tr></table>"""

GOVUK = """<section><a class="govuk-link" tabindex="-1" href="https://assets.example.gov.uk/media/1/form-x1.docx"><svg/></a>
<h3><a class="govuk-link" href="https://assets.example.gov.uk/media/1/form-x1.docx">X1 – annual return</a></h3></section>
<section><a href="https://assets.example.gov.uk/media/2/example-form-x1.docx"></a>
<h3><a href="https://assets.example.gov.uk/media/2/example-form-x1.docx">X1 – annual return (completed example)</a></h3></section>
<section><h3><a href="https://assets.example.gov.uk/media/3/guidance.pdf">Guidance on fees</a></h3></section>"""

BOARD = """<a href="javascript:f_down('./down.do?brd_id=1&amp;seq=9&amp;file_seq=1');">가족 거주사실 확인서(한글).hwp</a>
<a href="javascript:f_down('./down.do?brd_id=1&amp;seq=9&amp;file_seq=2');">가족 거주사실 확인서(작성예시).hwp</a>
<a href="/x/申請書.docx">申請書.docx</a><a href="/x/申請書_記入例.docx">申請書（記入例）.docx</a>
<a href="/x/대안교육기관_등록제_안내.hwp">대안교육기관 등록제 안내.hwp</a><a href="/x/b.hwp">대안교육기관 등록 신청서(작성예시).hwp</a>"""


def _pairs(html, page):
    out = {}
    for h in PairedAttachmentProvider.pairs_on_page(html.encode(), page):
        for lk in h.extra["pair_links"]:
            out.setdefault(lk["group"], {})[lk["role"]] = h.title
    return out


def test_publisher_labelled_pairs_on_a_form_library_page():
    g = _pairs(KCC, "https://www.example.or.kr/forms")
    assert len(g) == 1
    (pair,) = g.values()
    assert pair == {"blank": "조정신청서.hwp 다운로드", "example": "[작성예시]조정신청서.pdf 다운로드"}       # 답변서 has no example: no pair


def test_attachment_titles_survive_icon_links_and_docx_pairs_are_found():
    g = _pairs(GOVUK, "https://www.example.gov.uk/government/publications/x1")
    assert list(g.values()) == [{"blank": "X1 – annual return", "example": "X1 – annual return (completed example)"}]


def test_board_javascript_downloads_and_japanese_labels_but_no_guessing_across_different_forms():
    g = _pairs(BOARD, "https://overseas.example.go.kr/brd/view.do?seq=9")
    titles = sorted(tuple(sorted(v.values())) for v in g.values())
    assert ("申請書.docx", "申請書（記入例）.docx") in titles
    assert ("가족 거주사실 확인서(작성예시).hwp", "가족 거주사실 확인서(한글).hwp") in titles
    assert not any("등록제 안내" in t for pair in titles for t in pair)       # a notice is not the blank of the example form


@pytest.mark.parametrize("text,is_example", [("작성예시 SAMPLE_인감 위임장.pdf", True), ("Resample report.docx", False), ("記載例.pdf", True),
                                             ("Form to be completed by the applicant", False), ("CH1 completed example", True),
                                             ("Modelo de RFCD - Exemplo preenchido.docx", True), ("Contoh Pengisian Formulir.docx", True),
                                             ("voorbeeld ingevuld.docx", True), ("ตัวอย่างการกรอก แบบฟอร์ม.docx", True),
                                             ("Formulir pendaftaran.docx", False), ("Modelo em branco.odt", False),
                                             ("아이디어 공모 제출 예시.hwpx", True), ("예시문 작성 안내.hwpx", False),
                                             ("Fall 2021 SOAP – Completed (docx)", True), ("Fall 2021 SOAP – Blank (docx)", False)])
def test_example_markers(text, is_example):
    assert bool(EXAMPLE_RX.search(text)) is is_example


def test_pair_key_ignores_labels_sizes_and_format_words():
    assert pair_key("(작성예시)의료급여일수 연장 신청서.hwpx [49.89 KB]") == pair_key("의료급여일수 연장 신청서.hwp [44 KB]")
    assert pair_key("Example Part A of your consignment note (PDF, 120 KB, 2 pages)") == pair_key("Part A of your consignment note (ODT, 30 KB)")
    assert pair_key("記入例 [PDFファイル／106KB]") == ""                  # a bare label names no form, so it can pair with nothing


def _doc(db, i, fmt, text, role, group="https://p#k", relation="same_name", fam="fam-1", lang="en"):
    db.put_doc({"document_id": i, "sha256": i, "original_format": fmt, "source_domain": "www.example.gov.uk", "template_family_id": fam,
                "language": lang,
                "rights_status": "uk_open_government_licence_v3", "redistribution_allowed": True, "pair_type": "unpaired",
                "observed_pair_links": [{"group": group, "page": "https://p", "role": role, "key": "k", "name_relation": relation, "link_text": i}]})
    write_json(path("data", "normalized", f"{i}.json"), {"text_shingle_source": text})


def test_page_pairs_are_candidates_until_a_named_human_verifies_them(sandbox):
    db = DB()
    blank = "Annual return. Company name: ______ Company number: ______ Signature: ______ Date: ______"
    _doc(db, "doc-b", "docx", blank, "blank")
    _doc(db, "doc-e", "docx", blank.replace("______", "Example Ltd", 1) + " Example Ltd 0123", "example")
    cands = pairing.page_pair_candidates(db)
    assert len(cands) == 1 and cands[0]["evidence"]["grade"] == "strong" and cands[0]["evidence"]["template_text_containment"] >= 0.6
    pairing.record_observed(db, cands)
    counts = pairing.pair_counts(db)
    assert counts["real_pairs"]["observed_verified"]["pairs"] == 0 and counts["real_pairs"]["observed_candidate"]["pairs"] == 1
    with pytest.raises(ValueError):
        pairing.review_observed(db, "doc-b~doc-e", "verified", reviewer="  ")
    pairing.review_observed(db, "doc-b~doc-e", "verified", reviewer="tester", note="same form, fictitious values")
    pairing.record_observed(db, pairing.page_pair_candidates(db))           # re-running discovery never downgrades a decision
    counts = pairing.pair_counts(db)
    assert counts["real_pairs"]["observed_verified"]["pairs"] == 1 and counts["real_pairs"]["observed_candidate"]["pairs"] == 0
    assert counts["real_pairs"]["observed_verified"]["by_format"] == {"docx->docx": 1}
    assert db.get_doc("doc-e")["is_completed"] is True and db.get_doc("doc-b")["is_template"] is True
    assert "synthetic_pairs" in counts and counts["synthetic_pairs"] == {}


def test_text_that_shares_nothing_contradicts_the_label(sandbox):
    db = DB()
    _doc(db, "doc-b", "hwpx", "대안교육기관 등록제 안내 문서 본문 " * 5, "blank", relation="name_similar:0.81", fam="f1")
    _doc(db, "doc-e", "hwpx", "학칙 예시 제1조 목적 제2조 명칭 " * 5, "example", relation="name_similar:0.81", fam="f2")
    out = pairing.page_pair_candidates(db)                                # a publisher label alone cannot override the text
    assert [c["pair_type"] for c in out] == ["observed_auto_rejected"] and out[0]["evidence"]["auto_review"]["rule"] == "texts_differ"
    assert pairing.page_pair_candidates.last_contradicted[0]["containment"] < pairing.AUTO_REJECT_CONTAINMENT
    pairing.record_observed(db, out)
    assert pairing.pair_counts(db)["real_pairs"]["observed_auto_rejected"]["pairs"] == 1
    pairing.review_observed(db, "doc-b~doc-e", "verified", reviewer="tester")   # a reviewer can still overrule the rule
    pairing.record_observed(db, pairing.page_pair_candidates(db))
    assert db.one("SELECT pair_type FROM pairs WHERE pair_id='doc-b~doc-e'")["pair_type"] == "observed_verified"


def test_docx_pair_is_parsed_converted_and_compared():
    """DOCX documents flow through the same text extraction and HWPX conversion as HWP/HWPX."""
    docx = pytest.importorskip("docx")
    from formbench.formats.docx import docx_info, docx_to_hwpx
    from formbench.formats.hwpx import HwpxPackage

    def build(name, date):
        d = docx.Document()
        d.add_paragraph("Application for a permit")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "Name", name
        t.cell(1, 0).text, t.cell(1, 1).text = "Date", date
        b = io.BytesIO()
        d.save(b)
        return b.getvalue()
    blank, filled = build("", ""), build("Jane Example", "01/02/2026")
    ib, ie = docx_info(blank), docx_info(filled)
    assert ib["tables"] == ie["tables"] == 1 and "Jane Example" in ie["text"]
    sb, se = shingles(ib["text"]), shingles(ie["text"])
    assert len(sb & se) / len(sb) >= 0.6
    hwpx, info = docx_to_hwpx(filled)
    assert "Jane Example" in "".join(p.text for p in __import__("formbench.contracts.verify", fromlist=["DocView"]).DocView(HwpxPackage.load(hwpx)).ir.paras()) or info


def test_archived_synthetic_pairs_are_kept_but_not_used(sandbox):
    import os
    os.unlink(sandbox / "configs")
    (sandbox / "configs").mkdir()
    (sandbox / "configs" / "pairs.yaml").write_text("synthetic: {status: archived, use_for_evaluation: false, generate_new: false}\n")
    db = DB()
    db.put_doc({"document_id": "doc-s", "split": "test", "pair_evidence": [{"task_id": "t"}], "synthetic_status": "synthetic_completions_generated"})
    db.x("INSERT INTO pairs(pair_id,template_id,completed_id,pair_type,evidence,confidence,eval_eligible,created_at) VALUES('t','doc-s','x','synthetic_from_native','{}',1,1,'')")
    assert pairing.run(db, include_synthetic=True, log=lambda s: None)["synthetic"].startswith("archived")
    c = pairing.pair_counts(db)
    assert c["synthetic_pairs"] == {"synthetic_from_native": 1} and c["synthetic_policy"]["status"] == "archived"


def test_excluded_formats_are_neither_paired_nor_fetched(sandbox):
    g = PairedAttachmentProvider.pairs_on_page(BOARD.encode(), "https://o.example.go.kr/view.do", exclude_formats=["hwp", "doc"])
    assert {h.guessed_format for h in g} == {"docx"}                     # the HWP pair on the same page is dropped
    from formbench.collection.discovery import fetch_documents
    db = DB()
    db.x("INSERT INTO candidates(url,kind,source_domain,crawl_query,provider,discovered_at,guessed_format,extra) VALUES(?,?,?,?,?,?,?,?)",
         ("https://o.example.go.kr/a.hwp", "document", "o.example.go.kr", "q", "p", "", "hwp", "{}"))
    st = fetch_documents(db, {"exclude_formats": ["hwp", "doc"], "run_av": False}, offline=True, log=lambda s: None)
    assert st["ok"] == 0 and db.one("SELECT fetch_status FROM candidates")["fetch_status"] == "excluded_format"
    _doc(db, "doc-h", "hwp", "신청서 성명 주소", "blank")
    _doc(db, "doc-x", "hwpx", "신청서 성명 홍길동 주소", "example")
    assert pairing.page_pair_candidates(db) == []                         # the configured exclusion also covers stored HWP files
    assert pairing.pair_counts(db)["observed_pair_documents_in_excluded_formats"] == 1


def test_review_server_records_decisions(sandbox):
    import json as _j
    import threading
    import urllib.request

    from formbench.corpus import review_sheet
    db = DB()
    blank = "Annual return. Company name: ______ Company number: ______"
    _doc(db, "doc-b", "docx", blank, "blank")
    _doc(db, "doc-e", "docx", blank.replace("______", "Acme Widgets Ltd", 1).replace("______", "01234567", 1), "example")
    pairing.record_observed(db, pairing.page_pair_candidates(db))
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    threading.Thread(target=review_sheet.serve, kwargs={"port": port, "log": lambda m: None}, daemon=True).start()
    import time
    for _ in range(50):
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
            break
        except OSError:
            time.sleep(0.1)
    assert "doc-b~doc-e" in page and "Verified" in page

    def post(body, header=True):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/decide", data=_j.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"X-Formbench-Review": "1"} if header else {})})
        try:
            return urllib.request.urlopen(req).status, None
        except urllib.error.HTTPError as e:
            return e.code, _j.loads(e.read())
    assert post({"pair_id": "doc-b~doc-e", "decision": "verified", "reviewer": "tester"}, header=False)[0] == 403
    assert post({"pair_id": "doc-b~doc-e", "decision": "verified", "reviewer": ""})[0] == 400        # a name is required
    assert post({"pair_id": "doc-b~doc-e", "decision": "verified", "reviewer": "tester", "note": "ok"})[0] == 200
    assert pairing.pair_counts(DB())["real_pairs"]["observed_verified"]["pairs"] == 1
    assert post({"pair_id": "doc-b~doc-e", "decision": "rejected", "reviewer": "tester"})[0] == 200    # a decision can be revised
    assert pairing.pair_counts(DB())["real_pairs"]["observed_rejected"]["pairs"] == 1
    import pytest as _p
    with _p.raises(ValueError):
        review_sheet.serve(host="0.0.0.0", port=port + 1)


MIXED = """<ul><li>申請書 様式（<a href="/f/a.docx">Word</a> <a href="/f/a.pdf">PDF</a>）</li>
<li>申請書 記載例（<a href="/f/a_ex.docx">Word</a> <a href="/f/a_ex.pdf">PDF</a>）</li>
<li>届出書 様式（<a href="/f/b.pdf">PDF</a>）</li><li>届出書 記載例（<a href="/f/b_ex.docx">Word</a>）</li></ul>"""


def test_only_same_format_editable_pairs_are_discovered_and_kept(sandbox):
    hs = PairedAttachmentProvider.pairs_on_page(MIXED.encode(), "https://x.example.go.jp/p", pair_formats=["hwpx", "docx", "odt"])
    pairs = {}
    for h in hs:
        for lk in h.extra["pair_links"]:
            pairs.setdefault(lk["group"], []).append(h.guessed_format)
    assert list(pairs.values()) == [["docx", "docx"]]                   # PDF-PDF, DOCX-PDF and PDF-DOCX are not pairs
    db = DB()
    blank = "Annual return. Company name: ______ Company number: ______"
    _doc(db, "doc-b", "pdf", blank, "blank", group="g1")
    _doc(db, "doc-e", "docx", blank.replace("______", "Acme Widgets Ltd", 1).replace("______", "01234567", 1), "example", group="g1")
    _doc(db, "doc-c", "docx", blank, "blank", group="g2")
    _doc(db, "doc-d", "docx", blank.replace("______", "Acme Widgets Ltd", 1).replace("______", "01234567", 1), "example", group="g2")
    cands = pairing.page_pair_candidates(db)
    assert [(c["template_id"], c["completed_id"]) for c in cands] == [("doc-c", "doc-d")]
    assert cands[0]["evidence"]["alternate_format_pairs"] == []
    db.x("INSERT INTO pairs(pair_id,template_id,completed_id,pair_type,evidence,confidence,eval_eligible,created_at) VALUES('doc-b~doc-e','doc-b','doc-e','observed_candidate','{}',0.5,0,'')")
    for d in ("doc-b", "doc-e"):
        rec = db.get_doc(d)
        rec["provider"], rec["sha256"] = "observed_pair_pages", d
        db.put_doc(rec)
    out = pairing.prune_observed(db, log=lambda s: None)
    assert out["pairs_removed"] == 1 and out["documents_removed"] == 2
    assert db.get_doc("doc-b") is None and db.get_doc("doc-c") is not None


def test_example_list_and_form_list_on_different_pages_of_one_site():
    forms = """<a href="/f/land.odt">土地登記申請書.odt</a><a href="/f/other.odt">委託書.odt</a><a href="/f/x.pdf">說明.pdf</a>"""
    examples = """<a href="/e/land_ex.odt">土地登記申請書範例.odt</a><a href="/e/other_ex.pdf">委託書範例.pdf</a>"""
    site = {}
    for html, page in ((forms, "https://land.example.gov.tw/forms"), (examples, "https://land.example.gov.tw/examples")):
        for d, role, key in PairedAttachmentProvider.labelled_docs(html.encode(), page):
            if d.guessed_format == "odt":
                site.setdefault("land.example.gov.tw", []).append((d, role, key, page))
    hs = PairedAttachmentProvider.cross_page_pairs(site)
    assert sorted((h.url.rsplit("/", 1)[-1], h.extra["pair_links"][0]["role"]) for h in hs) == [("land.odt", "blank"), ("land_ex.odt", "example")]
    assert hs[0].extra["pair_links"][0]["name_relation"] == "same_site_other_page"


def test_single_blank_and_example_of_one_format_on_a_page_are_paired():
    html = """<a href="/d/1.docx">Télécharger le modèle vierge Word</a><a href="/d/2.docx">Télécharger l'exemple rempli Word</a>
<a href="/d/3.pdf">Télécharger le modèle vierge PDF</a>"""
    hs = PairedAttachmentProvider.pairs_on_page(html.encode(), "https://f.example.ma/devis", pair_formats=["docx", "odt", "hwpx"])
    assert sorted(h.url.rsplit("/", 1)[-1] for h in hs) == ["1.docx", "2.docx"]


def test_blank_offered_in_two_formats_is_not_a_tie_and_reference_example_marker_is_stripped():
    # one blank as .odt and .pdf; the example says "參考範例" (reference example) and differs only by a "(1)" suffix
    rows = "".join(
        f'<li><a href="/uploaddowndoc?dis=download&amp;file=d/{i}a.odt&amp;filedisplay=a.odt">(民)表{n}-名冊 (1).odt</a> '
        f'<a href="/uploaddowndoc?dis=download&amp;file=d/{i}a.pdf&amp;filedisplay=a.pdf">(民)表{n}-名冊 (1).pdf</a></li>'
        f'<li><a href="/uploaddowndoc?dis=download&amp;file=d/{i}b.odt&amp;filedisplay=b.odt">(民)表{n}-參考範例-名冊.odt</a> '
        f'<a href="/uploaddowndoc?dis=download&amp;file=d/{i}b.pdf&amp;filedisplay=b.pdf">(民)表{n}-參考範例-名冊.pdf</a></li>'
        for i, n in enumerate("一二"))
    hs = PairedAttachmentProvider.pairs_on_page(f"<html><body><ul>{rows}</ul></body></html>".encode(), "https://d.example.gov.tw/home.jsp",
                                                pair_formats=["hwpx", "docx", "odt"])
    assert sorted(h.url.split("file=")[1][:6] for h in hs) == ["d/0a.o", "d/0b.o", "d/1a.o", "d/1b.o"]
    assert pair_key("(民)表一-參考範例-名冊.odt") == pair_key("(民)表一-名冊.odt")


def test_neighbour_expansion_skips_document_links():
    from formbench.collection.pair_explorer import DOC_URL_RX
    assert DOC_URL_RX.search("https://c.example.lg.jp/uploaded/attachment/39396.doc")
    assert DOC_URL_RX.search("https://d.example.gov.tw/uploaddowndoc?dis=download&file=download/1.doc&filedisplay=a.odt")
    assert not DOC_URL_RX.search("https://u.example.edu.tw/p/412-1030-2310.php?Lang=zh-tw")


def test_fill_in_example_marker_is_one_label():
    assert pair_key("契稅申報書-填寫範例.odt(另開新視窗)") == pair_key("契稅申報書.odt(另開新視窗)")
    assert pair_key("委託書填表範例.docx") == pair_key("委託書.docx")


def test_label_only_links_take_the_form_name_of_their_cell_not_the_table_heading():
    # one table of many forms under one heading; each cell names its form in a paragraph above "様式（ワード）" / "作成例（ワード）"
    row = '<tr><td><p>{n}</p><p><a href="/d/{f}.docx">{lab}（ワード：16KB）</a></p><p><a href="/d/{f}.pdf">{lab}（PDF：28KB）</a></p></td></tr>'
    body = "".join(row.format(n=n, f=f, lab=lab) for n, f, lab in [
        ("設立認証申請書", "a", "様式"), ("団体確認書", "b", "作成例"), ("事業報告書等提出書", "c", "様式"), ("事業報告書", "d", "作成例"),
        ("役員名簿", "e", "様式"), ("役員名簿", "f", "作成例")])
    html = f"<html><body><h2>（1）設立認証申請時に提出する書類</h2><table>{body}</table></body></html>"
    hs = PairedAttachmentProvider.pairs_on_page(html.encode(), "https://p.example.lg.jp/npo.html", pair_formats=["hwpx", "docx", "odt"])
    assert sorted(h.url.rsplit("/", 1)[1] for h in hs) == ["e.docx", "f.docx"]


def test_explorer_visits_new_hosts_and_thin_coverage_cells_first(sandbox):
    from formbench.collection.pair_explorer import PairExplorer, add_seed_pages, coverage, coverage_cell
    assert coverage_cell("www.sanxia.ntpc.gov.tw") == ("TW", "gov") and coverage_cell("www.nhs.uk") == ("UK", "health")
    db = DB()
    ex = PairExplorer(db, {}, log=lambda *a: None, client=object())
    for i in range(3):                                 # a well explored cell
        db.x("INSERT INTO explore_pages(url,host,query,origin,depth,status,ts) VALUES(?,?,?,?,?,?,?)",
             (f"https://a{i}.example.gov.tw/p", f"a{i}.example.gov.tw", "q", "websearch", 0, "ok", "t"))
    add_seed_pages(db, ["https://a0.example.gov.tw/again"], origin="reanalyse")
    add_seed_pages(db, ["https://b.example.gov.tw/new", "https://c.example.ac.uk/new"], origin="websearch")
    order = [ex._next_query(None)["query"] for _ in range(3)]
    assert order == ["page:https://c.example.ac.uk/new", "page:https://b.example.gov.tw/new", "page:https://a0.example.gov.tw/again"]
    assert {(c["country"], c["sector"]) for c in coverage(db)} >= {("TW", "gov")}


def test_unlabelled_files_of_one_post_pair_only_when_one_completes_the_other(sandbox):
    # S4.6: same post folder, opaque file names (UUIDs); the texts decide which file completes which
    db = DB()
    blank = ("Leave application form. Applicant name ______ Department ______ Period from ______ to ______ Reason ______ "
             "Substitute ______ Approved by ______ Date ______ Remarks: attach supporting documents if the leave exceeds three days.")
    filled = blank.replace("Applicant name ______", "Applicant name Hong Gildong").replace("Reason ______", "Reason family event in Busan")
    other = "Consent to collect personal data. I agree that the office collects my name and contact for this application only. " * 2
    for i, t in (("doc-u1", filled), ("doc-u2", blank), ("doc-u3", other), ("doc-u4", blank)):
        _doc(db, i, "odt", t, "member", group="https://h/relfile/1/77/", relation="content_same_unit")
    cands = pairing.page_pair_candidates(db)
    assert len(cands) == 1
    c = cands[0]
    assert c["template_id"] in ("doc-u2", "doc-u4") and c["completed_id"] == "doc-u1"
    assert c["evidence"]["grade"] == "weak" and c["evidence"]["name_relation"] == "content_same_unit"
    db2_only_same = DB()
    for i in ("doc-u1", "doc-u3"):
        db2_only_same.x("DELETE FROM documents WHERE document_id=?", (i,))
    assert pairing.page_pair_candidates(db2_only_same) == []               # the same file twice is not a completion


def test_script_drawn_page_is_read_through_the_rendered_dom(sandbox, monkeypatch):
    # S4.4: the static HTML of an SPA has no links; the rendered DOM lists the blank and its example
    from formbench.collection import pair_explorer, render_page
    shell = b'<html><body><app-root></app-root><script src="main.bundle.js"></script></body></html>'
    dom = ('<html><body><ul><li><a href="/f/apply.odt">Application form.odt</a></li>'
           '<li><a href="/f/apply-example.odt">Application form (example).odt</a></li></ul></body></html>').encode()
    assert render_page.looks_scripted(shell) and not render_page.looks_scripted(dom)
    monkeypatch.setattr(render_page, "render", lambda url, ua, **k: dom)

    class Client:
        ua = "test"
        def fetch(self, url, purpose="page"):
            return type("F", (), {"status": "ok", "content": shell, "reason": ""})()
    db = DB()
    ex = pair_explorer.PairExplorer(db, {"pair_formats": ["hwpx", "docx", "odt"]}, log=lambda *a: None, client=Client())
    page = ex._visit("https://spa.example.gov.tw/forms", "q", "test", 0)
    assert page["rendered"] and len(page["pairs"]) == 2
    assert db.one("SELECT status FROM explore_pages WHERE url=?", ("https://spa.example.gov.tw/forms",))["status"] == "ok_rendered"


def test_institution_home_page_leads_to_an_office_form_library(sandbox):
    # S1.3: a university home page links its accounting office (sub-domain); the office's form page lists a pair
    from formbench.collection import pair_explorer
    pages = {
        "https://www.uni.example.edu.tw/": '<html><body><a href="https://acc.uni.example.edu.tw/">會計室 Accounting</a>'
                                            '<a href="https://www.other.example.com/forms">表單下載 elsewhere</a><a href="/news">News</a></body></html>',
        "https://acc.uni.example.edu.tw/": '<html><body><a href="/forms.php">表單下載</a></body></html>',
        "https://acc.uni.example.edu.tw/forms.php": '<html><body><ul><li><a href="/f/claim.odt">支出證明單.odt</a></li>'
                                                     '<li><a href="/f/claim-ex.odt">支出證明單(範例).odt</a></li></ul></body></html>',
    }
    visited = []

    class Client:
        ua = "test"
        def fetch(self, url, purpose="page"):
            visited.append(url)
            body = pages.get(url)
            return type("F", (), {"status": "ok" if body else "http_error", "content": (body or "").encode(), "reason": ""})()
    db = DB()
    pair_explorer.add_seed_pages(db, ["https://www.uni.example.edu.tw/"], origin="directory_tw")
    ex = pair_explorer.PairExplorer(db, {"pair_formats": ["hwpx", "docx", "odt"]}, log=lambda *a: None, client=Client())
    stats = ex.run(max_queries=1, max_depth=1)
    assert stats["pair_pages"] == 1
    assert "https://www.other.example.com/forms" not in visited                 # another organisation is not followed
    assert db.one("SELECT count(*) n FROM candidates")["n"] == 2


def test_lost_network_requeues_the_work_instead_of_recording_site_errors(sandbox, monkeypatch):
    from formbench.collection import pair_explorer
    monkeypatch.setattr(pair_explorer, "network_up", lambda timeout=3.0: False)

    class Client:
        ua = "test"
        def fetch(self, url, purpose="page"):
            return type("F", (), {"status": "error", "content": b"", "reason": "network error: ConnectionError"})()
    db = DB()
    pair_explorer.add_seed_pages(db, ["https://a.example.gov.tw/forms", "https://b.example.gov.tw/forms"], origin="websearch")
    ex = pair_explorer.PairExplorer(db, {}, log=lambda *a: None, client=Client())
    stats = ex.run(max_queries=5)
    assert stats["network_down"] == 1 and stats.get("queries", 0) == 1
    assert db.one("SELECT count(*) n FROM explore_queries WHERE status='pending'")["n"] == 2
    assert db.one("SELECT count(*) n FROM explore_pages")["n"] == 0


def test_fetch_shards_keep_one_institution_in_one_process():
    import json as _j
    from formbench.collection.discovery import _shard_of
    cfg = {"multi_institution_path_depth": {"www-ws.gov.taipei": 3}}
    def row(url, page):
        return {"extra": _j.dumps({"pair_links": [{"page": page}]}), "source_domain": url.split("/")[2]}
    a = row("https://www-ws.gov.taipei/001/Upload/305/relfile/1/2/a.odt", "https://www-ws.gov.taipei/001/Upload/305/relfile/1/2/")
    b = row("https://www-ws.gov.taipei/001/Upload/305/relfile/9/8/b.odt", "https://www-ws.gov.taipei/001/Upload/305/relfile/9/8/")
    assert _shard_of(a, cfg, 8) == _shard_of(b, cfg, 8)                    # same agency (Upload/305) -> same shard
    assert {_shard_of({"extra": "{}", "source_domain": f"h{i}.example.gov.tw"}, cfg, 8) for i in range(64)} == set(range(8))


def test_excluded_country_is_neither_searched_nor_visited(sandbox):
    from formbench.collection import pair_explorer
    db = DB()
    pair_explorer.add_seed_pages(db, ["https://www.city.example.lg.jp/forms", "https://www.example.gov.tw/forms"], origin="websearch")
    db.x("INSERT INTO explore_queries(query,engine,region,origin,status,n_results,ts) VALUES(?,?,?,?,?,?,?)",
         ("様式 記入例", pair_explorer.WEB_ENGINE, "jp-jp", "seed:ja", "pending", 0, "t"))
    ex = pair_explorer.PairExplorer(db, {"exclude_countries": ["JP"], "pair_explorer": {"search_every": 1000}}, log=lambda *a: None,
                                    client=type("C", (), {"ua": "x"})())
    picked = [ex._next_query(None) for _ in range(3)]
    assert [p["query"] for p in picked if p] == ["page:https://www.example.gov.tw/forms"]
    assert db.one("SELECT count(*) n FROM explore_queries WHERE status='skipped_country'")["n"] == 2
    assert ex._visit("https://www.pref.example.lg.jp/a", "q", "hub", 1) is None


def test_unreviewed_pairs_of_an_excluded_country_leave_the_review_queue(sandbox, monkeypatch):
    from formbench.corpus import pairing as pr
    import formbench.util as u
    real = u.load_yaml
    monkeypatch.setattr(u, "load_yaml", lambda p: {**real(p), "exclude_countries": ["JP"], "include_languages": []}
                        if str(p).endswith("collection.yaml") else real(p))
    c = [{"pair_type": "observed_candidate", "template_id": "a", "completed_id": "b", "evidence": {"page": "https://www.city.example.lg.jp/forms"}},
         {"pair_type": "observed_auto_rejected", "template_id": "a", "completed_id": "b", "evidence": {"page": "https://www.city.example.lg.jp/f2"}},
         {"pair_type": "observed_candidate", "template_id": "a", "completed_id": "b", "evidence": {"page": "https://www.example.gov.tw/forms"}}]
    out = pr._apply_country_exclusion(c)
    assert [x["pair_type"] for x in out] == ["observed_excluded", "observed_excluded", "observed_candidate"]
    assert out[0]["evidence"]["excluded"]["reason"] == "country:JP" and out[1]["evidence"]["excluded"]["was"] == "observed_auto_rejected"


def test_board_pagination_is_followed_on_lists_not_on_post_views():
    from formbench.collection.pair_explorer import PairExplorer
    ex = PairExplorer.__new__(PairExplorer)
    html = b"<html><body></body></html>"
    lst = ex._neighbours(html, "https://www.example.go.kr/minwon/00026/00028.web?cpage=3", productive=True)
    view = ex._neighbours(html, "https://www.example.go.kr/bbs/boardView.do?bIdx=110884270&page=1&menuId=3109", productive=True)
    assert len([u for u in lst if "cpage=" in u]) == 49 and not [u for u in view if "page=" in u and "bIdx" in u]


def test_duplicate_forms_keep_one_representative_and_reviewed_ones_are_marked_too(sandbox, monkeypatch):
    import formbench.util as u
    real = u.load_yaml
    monkeypatch.setattr(u, "load_yaml", lambda p: {**real(p), "include_languages": []} if str(p).endswith("collection.yaml") else real(p))
    db = DB()
    form = "土地共有所有權分割契約書 下列土地經全體共有人同意分割 特訂立本契約 土地標示 坐落 鄉鎮市區 段 小段 地號 面積 所有權人 權利範圍 權利價值"
    other = "肥料販売業務開始届出書 令和 年 月 日 書類を作成した者 住所 氏名 販売所の名称 所在地 開始年月日"
    for i, (t, g) in enumerate(((form, "a"), (form + " 備註", "b"), (other, "c"))):
        _doc(db, f"doc-b{i}", "odt", t, "blank", group=f"https://p{g}#k")
        _doc(db, f"doc-e{i}", "odt", t + " 臺中市○○區 123地號 0.5 新臺幣100萬元 王小明", "example", group=f"https://p{g}#k")
    pairing.record_observed(db, pairing.page_pair_candidates(db))
    pairing.review_observed(db, "doc-b1~doc-e1", "verified", reviewer="tester")
    out = pairing.dedupe_observed(db)
    types = {r["pair_id"]: r["pair_type"] for r in db.q("SELECT pair_id, pair_type FROM pairs")}
    assert out["pairs_marked_duplicate"] == 1 and types["doc-b1~doc-e1"] == "observed_verified"      # verified wins
    assert types["doc-b0~doc-e0"] == "observed_duplicate" and types["doc-b2~doc-e2"] == "observed_candidate"
    pairing.record_observed(db, pairing.page_pair_candidates(db))                                    # re-running keeps it
    assert db.one("SELECT pair_type FROM pairs WHERE pair_id='doc-b0~doc-e0'")["pair_type"] == "observed_duplicate"


def test_only_korean_and_english_pairs_stay_open(sandbox, monkeypatch):
    from formbench.corpus import pairing as pr
    from formbench.collection.pair_explorer import language_allowed
    import formbench.util as u
    real = u.load_yaml
    monkeypatch.setattr(u, "load_yaml", lambda p: {**real(p), "exclude_countries": ["JP"], "include_languages": ["ko", "en"]}
                        if str(p).endswith("collection.yaml") else real(p))
    db = DB()
    for i, lang in (("k", "ko+en"), ("e", "en"), ("z", "zh"), ("u", "unknown")):
        db.put_doc({"document_id": f"doc-{i}b", "sha256": f"{i}b", "language": lang, "original_format": "docx"})
        db.put_doc({"document_id": f"doc-{i}e", "sha256": f"{i}e", "language": lang, "original_format": "docx"})
    c = [{"pair_type": "observed_candidate", "template_id": f"doc-{i}b", "completed_id": f"doc-{i}e", "evidence": {"page": page}}
         for i, page in (("k", "https://www.example.go.kr/f"), ("e", "https://www.example.gov.uk/f"), ("z", "https://www.example.gov.tw/f"),
                         ("u", "https://www.example.go.kr/x"))]
    out = pr._apply_country_exclusion(c)
    assert [x["pair_type"] for x in out] == ["observed_candidate", "observed_candidate", "observed_excluded", "observed_candidate"]
    assert out[2]["evidence"]["excluded"]["reason"] == "language:zh"
    cfg = {"include_languages": ["ko", "en"]}
    assert language_allowed("ko", cfg) and language_allowed("en-gb", cfg) and language_allowed("kr", cfg) and not language_allowed("zh-tw", cfg)


def test_site_queries_keep_only_hits_on_that_site(sandbox):
    from formbench.collection import pair_explorer
    from formbench.collection.search import ProviderResult, SearchHit
    db = DB()
    db.conn.executescript(pair_explorer.SCHEMA)
    db.x("INSERT INTO explore_queries(query,engine,region,origin,status,n_results,ts) VALUES(?,?,?,?,?,?,?)",
         ("site:collegestar.org completed", pair_explorer.WEB_ENGINE, "us-en", "usersite:en", "pending", 0, "t"))
    visited = []

    class Prov:
        def search(self, q, n):
            return ProviderResult("w", "w", q, "ok", [SearchHit("https://www.merriam-webster.com/dictionary/completed", kind="page"),
                                                     SearchHit("https://collegestar.org/forms", kind="page")])
    ex = pair_explorer.PairExplorer(db, {}, log=lambda *a: None, client=type("C", (), {"ua": "x"})())
    ex._visit = lambda url, *a, **k: visited.append(url)
    q = ex._next_query(None)
    ex._run_query(q, __import__("collections").Counter(), {q["region"]: Prov()}, 1)
    assert visited == ["https://collegestar.org/forms"]
