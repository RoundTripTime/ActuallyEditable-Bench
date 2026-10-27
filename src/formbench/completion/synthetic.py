"""SyntheticCompletion values: fixed, fully synthetic personas and business data (no real personal data).

Collision control: names are dictionary words that read as placeholders, phone numbers use the unassigned
``010-0000-xxxx`` block, e-mail uses the reserved ``example.com`` domain (RFC 2606), addresses use non-existent
administrative names, and no resident-registration-like identifier is ever produced.
"""
from __future__ import annotations

import hashlib
import re

GENERATOR_VERSION = "1.0"

PERSONAS = [
    {"persona_id": "P01", "name": "가상인", "name_en": "Gasang In", "birth": "1990. 1. 1.", "address": "가상특별시 예시구 모의로 101, 1동 101호",
     "phone": "010-0000-0101", "email": "persona01@example.com", "org": "예시상사 주식회사", "dept": "모의기획팀", "title": "선임", "school": "가상대학교"},
    {"persona_id": "P02", "name": "모의현", "name_en": "Moui Hyeon", "birth": "1985. 7. 15.", "address": "예시도 견본시 시험길 22",
     "phone": "010-0000-0202", "email": "persona02@example.com", "org": "견본기술연구소", "dept": "시험평가부", "title": "책임", "school": "예시고등학교"},
    {"persona_id": "P03", "name": "시험용", "name_en": "Siheom Yong", "birth": "1978. 11. 30.", "address": "견본광역시 가상구 샘플대로 303",
     "phone": "010-0000-0303", "email": "persona03@example.com", "org": "모의복지재단", "dept": "운영지원과", "title": "과장", "school": "견본전문학교"},
    {"persona_id": "P04", "name": "예시랑", "name_en": "Yesi Rang", "birth": "2001. 3. 9.", "address": "가상도 모의군 예시읍 표본리 44",
     "phone": "010-0000-0404", "email": "persona04@example.com", "org": "표본건설 합자회사", "dept": "품질안전팀", "title": "사원", "school": "모의대학교"},
]

SENTENCES = [
    "본 내용은 평가를 위한 합성 예시 문장이며 실제 사실과 무관합니다.",
    "신청 배경과 목적을 구체적으로 설명하고 기대 효과를 단계별로 기술합니다.",
    "관련 업무를 수행하며 일정 관리와 품질 점검을 꾸준히 담당하였습니다.",
    "주요 추진 내용은 현황 조사, 개선안 도출, 시범 적용, 결과 공유의 순서로 진행합니다.",
    "필요한 자료는 별도 첨부로 제출하며 추가 요청이 있으면 즉시 보완하겠습니다.",
    "향후 계획은 분기별 목표를 정하고 점검 회의를 통해 진행 상황을 확인하는 것입니다.",
]

_RULES = [
    ("name", r"성\s*명|이\s*름|대표자|담당자|작성자|신청인|청구인|보호자|확인자|성함|name|서명자|제출자|신고인|위임인|대리인"),
    ("birth", r"생년월일|출생|birth"),
    ("phone", r"전화|연락처|휴대|핸드폰|팩스|phone|tel|mobile"),
    ("email", r"이메일|e-?mail|전자우편"),
    ("address", r"주\s*소|소재지|거주지|address|근무지|사업장"),
    ("date", r"일\s*자|날\s*짜|일\s*시|기\s*간|연월일|date|기한|일정|작성일|접수일|신청일|발생일"),
    ("money", r"금\s*액|단\s*가|합\s*계|비\s*용|가\s*격|수수료|예산|보상금|치료비|총액|amount|price"),
    ("count", r"수\s*량|인\s*원|횟\s*수|건\s*수|나\s*이|연\s*령|점\s*수|번\s*호|호\s*수|면\s*적|quantity|no\."),
    ("org", r"기\s*관|회\s*사|소\s*속|상\s*호|법\s*인|단\s*체|업체|학교|근무처|직장|부대|organization|company"),
    ("dept", r"부\s*서|학\s*과|전\s*공|department"),
    ("title", r"직\s*위|직\s*급|직\s*책|직\s*업|관\s*계|position|title"),
    ("long", r"사\s*유|내\s*용|동\s*기|소\s*개|비\s*고|의\s*견|계\s*획|목\s*적|요\s*약|개\s*요|특이|설\s*명|경\s*위|조치|결과|업무|description|remarks|comments"),
]
_COMPILED = [(k, re.compile(rx, re.I)) for k, rx in _RULES]


def data_type_of(label: str) -> str:
    for k, rx in _COMPILED:
        if rx.search(label or ""):
            return k
    return "generic"


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:8], 16)


def long_text(n_chars: int, seed) -> str:
    out, i = [], _h("long", seed)
    while sum(len(s) + 1 for s in out) < n_chars:
        out.append(SENTENCES[i % len(SENTENCES)])
        i += 1
    return " ".join(out)[:n_chars].rstrip()


def value_for(field: dict, persona: dict, seed: int, long_chars: int = 220) -> str:
    """Deterministic value for a field (depends only on persona, seed, field id/label/kind)."""
    fid, label, kind = field["field_id"], field.get("label", ""), field["kind"]
    dt = field.get("data_type") or data_type_of(label)
    k = _h(seed, persona["persona_id"], fid, label)
    if kind == "check":
        return "√"
    if kind == "long_text":
        return long_text(long_chars, (seed, fid))
    if dt == "name":
        return persona["name"]
    if dt == "birth":
        return persona["birth"]
    if dt == "phone":
        return persona["phone"]
    if dt == "email":
        return persona["email"]
    if dt == "address":
        return persona["address"]
    if dt == "date":
        return f"2026. {1 + k % 12}. {1 + (k // 12) % 28}."
    if dt == "money":
        return f"{(1 + k % 97) * 10000:,}원"
    if dt == "count":
        return str(1 + k % 30)
    if dt == "org":
        return persona["org"]
    if dt == "dept":
        return persona["dept"]
    if dt == "title":
        return persona["title"]
    if dt == "long":
        return ["해당 사항 확인함", "모의 입력 내용", "정기 점검 수행", "시험 운영 지원"][k % 4]
    return ["해당 없음", "예시 입력", "모의 값", "시험 기재"][k % 4]


def persona_for(template_id: str, seed: int) -> dict:
    return PERSONAS[_h(seed, template_id) % len(PERSONAS)]
