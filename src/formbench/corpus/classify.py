"""FormClassifier: rule-based classification (blank / completed / other, structured / semi-structured, domain,
difficulty features). Rules are deterministic; uncertain cases are labelled ``uncertain`` for the review queue."""
from __future__ import annotations

import re

CLASSIFIER_VERSION = "1.2"             # 1.1: Japanese/Chinese language detection, ja/en domain cues; 1.2: ne/hi (Devanagari), zxx (number grids)
FORM_CUES = re.compile(r"申請書|届出書|様式|記入|氏名|住所|生年月日|年\s*月\s*日|署名|押印|신청서|신고서|지원서|동의서|확인서|계약서|보고서|계획서|점검표|평가표|설문|일지|회의록|명세서|견적서|발주서|제안서|기록지|접수증|체크리스트|이력서|"
                       r"정산서|검수서|서식|양식|위임장|청구서|증명서|통지서|대장|성\s*명|주\s*소|생년월일|년\s+월\s+일|\(서명 또는 인\)|\(인\)|□|☐|\[\s\]|"
                       r"application|form|signature|date of birth|name:|address", re.I)
DOMAINS = [
    ("의료·복지", r"의료|진료|진단|환자|복지|장애|요양|건강|보건|간병|치료|medical|health|介護|医療|福祉|健康|保健|患者|障害|\bpatient\b|\bcare\b"),
    ("취업·지원", r"이력서|입사|채용|지원서|자기소개|경력|구직|resume|job application|履歴書|職務経歴|採用|求職|ジョブ・カード|ジョブカード|\bemployment\b"),
    ("교육·시험", r"학교|학생|교육|시험|수강|학점|장학|입학|수업|교원|훈련|education|student|学校|学生|教育|試験|研修|実習|\btraining\b|\bschool\b"),
    ("연구·사업·과제", r"연구|과제|사업계획|사업 계획|제안서|R&D|보조금|지원사업|proposal|grant|研究|補助金|事業計画|助成|\bfunding\b"),
    ("법률·계약", r"계약|약정|위임|합의|소송|법원|공증|contract|agreement|court|契約|登記|委任|法務|裁判|遺産|抵当|相続|贈与|\bpower of attorney\b|\btribunal\b"),
    ("회계·세무", r"세무|세금|부가가치|소득|정산|회계|영수|원천징수|세액|국세|tax|invoice|receipt|税|申告|会計|領収|納付|\baccount\b"),
    ("인사·노무", r"근로|임금|휴가|인사|노무|퇴직|고용|근무|산재|employee|payroll|leave|労働|雇用|賃金|休暇|離職|社会保険|\bemployment\b|\bwages\b"),
    ("건설·제조·품질·안전", r"건설|공사|시공|제조|품질|안전|검사|시설|설비|위험|소방|safety|\binspection\b|quality|建設|工事|製造|品質|安全|点検|消防|\bhazard\b|\bwaste\b|\bconstruction\b"),
    ("행사·참가·접수", r"행사|참가|접수|등록|대회|공모|event|registration|参加|申込|受付|イベント|\bentry\b"),
    ("점검·감사·일지", r"점검|감사|일지|체크리스트|checklist|audit|log|点検|監査|日誌|記録簿|\binspection\b"),
    ("견적·발주", r"견적|발주|구매|납품|주문|quotation|purchase order|見積|発注|購入|納品|\border\b"),
    ("설문·조사", r"설문|조사표|조사서|survey|questionnaire|アンケート|調査票|\bcensus\b"),
    ("회의·기록", r"회의록|회의|기록지|minutes|meeting|議事録|会議|\bagenda\b"),
    ("인증·증명", r"인증|증명|확인서|자격|면허|허가|certificate|certification|license|証明|認定|資格|免許|許可|\bpermit\b"),
    ("행정·민원", r"민원|신청서|신고서|청구서|행정|허가|등록|통지|application|request|申請|届出|届|手続|登録|変更|\bclaim\b|\bnotice\b"),
]
_DOM = [(n, re.compile(rx, re.I)) for n, rx in DOMAINS]


_NEPALI_RX = re.compile(r"(हरू|नुहोस्|गर्ने|गर्नु|हुन्छ|छन्|पर्ने|लागि|मिति)")     # Nepali verb endings / postpositions
_HINDI_RX = re.compile(r"(है|हैं|करें|कीजिए|लिए|तारीख|और)")


def detect_language(text: str) -> str:
    """ISO 639-1 code from character counts (ko, en, ja, zh, ne, hi; 'ko+en' for mixed forms); 'zxx' (ISO 639-2:
    no linguistic content) for numbers with (almost) no letters, e.g. a multiplication grid; 'unknown' otherwise."""
    deva = sum(1 for c in text if "\u0900" <= c <= "\u097f")
    if deva >= 20 and deva >= 0.3 * sum(1 for c in text if c.isalpha()):
        return "ne" if len(_NEPALI_RX.findall(text)) >= len(_HINDI_RX.findall(text)) else "hi"
    if sum(1 for c in text if c.isalpha()) < 5 and sum(1 for c in text if c.isdigit()) >= 10:
        return "zxx"                                    # a number grid ('x' as the multiplication sign is not a word)
    ko = sum(1 for c in text if "가" <= c <= "힣")
    en = sum(1 for c in text if c.isascii() and c.isalpha())
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if kana + han >= 20 and kana + han > ko:
        return "ja" if kana >= 0.05 * (kana + han) else "zh"
    if ko + en < 20:
        return "unknown"
    if ko >= 0.3 * (ko + en):
        return "ko" if en < ko else "ko+en"
    return "en"


def domain_of(text: str, title: str = "") -> str:
    head = (title + " " + text[:400])
    scores = [(len(rx.findall(head)) * 3 + len(rx.findall(text[:6000])), n) for n, rx in _DOM]
    best = max(scores, key=lambda t: t[0])
    return best[1] if best[0] > 0 else "기타"


def classify(text: str, title: str, features: dict) -> dict:
    """features: n_fields, n_blank_cells, n_filled_cells, n_tables, n_merged, n_checks, n_long, n_groups, n_images, pages,
    form_fields (pdf widgets), text_source"""
    cues = len(FORM_CUES.findall(text[:8000] + " " + title))
    n_fields = features.get("n_fields", 0) or 0
    widgets = features.get("form_fields", 0) or 0
    is_form = cues >= 2 or n_fields >= 3 or widgets >= 3
    blank_like = n_fields + widgets
    filled = features.get("n_value_like", 0) or 0
    if not is_form:
        kind, is_template, is_completed = "other", False, False
    elif blank_like >= 3 and filled <= max(2, 0.25 * blank_like):
        kind, is_template, is_completed = "blank_form", True, False
    elif filled >= 5 and filled > blank_like:
        kind, is_template, is_completed = "completed_form", False, True
    elif blank_like >= 1:
        kind, is_template, is_completed = "blank_form", True, False
    else:
        kind, is_template, is_completed = "uncertain", None, None
    n_tables = features.get("n_tables", 0) or 0
    structure = "structured" if (n_tables >= 1 and n_fields >= 5) or widgets >= 5 else ("semi_structured" if is_form else "unstructured")
    score = (min(n_fields, 60) / 20.0 + (features.get("n_merged", 0) or 0) / 15.0 + (features.get("n_long", 0) or 0) * 0.5 + (features.get("n_groups", 0) or 0) * 0.7
             + max(0, (features.get("pages", 1) or 1) - 1) * 0.5 + (features.get("n_images", 0) or 0) * 0.3)
    difficulty = "easy" if score < 1.5 else ("medium" if score < 3.5 else "hard")
    return {"classifier_version": CLASSIFIER_VERSION, "kind": kind, "is_form": is_form, "is_template": is_template, "is_completed": is_completed,
            "structure": structure, "domain": domain_of(text, title), "difficulty": difficulty, "difficulty_score": round(score, 2), "form_cues": cues,
            "features": features}


def refresh_language_domain(db) -> dict:
    """Recompute language and domain from the normalized side-car text (no re-parsing) after a classifier change."""
    from ..util import path, read_json
    n = 0
    for d in db.docs():
        p = path("data", "normalized", f"{d['document_id']}.json")
        if not p.exists() or d.get("parser_status") != "ok":
            continue
        side = read_json(p)
        text = side.get("text_shingle_source", "")
        title = d.get("link_text") or d.get("filename") or side.get("title") or ""
        lang = detect_language(text)
        if lang in ("ko", "ko+en") and d.get("language") in ("ko", "ko+en"):
            continue                                   # 1.1 changed nothing for Korean; keep the full-text classification
        dom = domain_of(text, title)
        cl = d.setdefault("classification", {})
        if d.get("language") != lang or cl.get("domain") != dom:
            d["language"], cl["domain"] = lang, dom
            d.setdefault("version_info", {})["classifier"] = CLASSIFIER_VERSION
            db.put_doc(d)
            n += 1
    return {"updated": n, "classifier_version": CLASSIFIER_VERSION}
