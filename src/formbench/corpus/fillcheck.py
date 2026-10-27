"""Is the 'completed example' actually filled in? (user review 2026-10-15: many publisher examples are the blank form
with a 記入例 / 範例 stamp or with guidance sentences in the fields; rendered, they look like the blank.)

Deterministic text comparison of the blank and the example (whitespace removed): the text the example adds is split
into inserted fragments; publisher labels are discarded; what remains is classified as value-like (names, numbers,
dates, placeholders such as ○○ / XX, ticks) or instruction-like (please write / fill / attach ...).

verdict:
  filled          the example adds value-like text
  no_fill         fewer than MIN_ADDED characters added once labels are removed (same text as the blank)
  guidance_only   what is added is instructions about the fields, not values
"""
from __future__ import annotations

import difflib
import re
import unicodedata

FILLCHECK_VERSION = "1.0"
MIN_ADDED = 10                # characters (after removing labels) below which nothing was typed
GUIDANCE_SHARE = 0.7          # instruction-like share of the added characters ...
MAX_VALUE_CHARS = 15          # ... while value-like characters stay below this

LABEL_RX = re.compile(r"(記入例|記載例|作成例|記入見本|見本|範例|范例|填寫範例|填表範例|參考範例|填写示例|示例|样例|작성\s*예시|기재\s*례|작성\s*례|"
                      r"예시|견본|sample|example|completed|specimen|[（(【\[＜<＊*]\s*[）)】\]＞>＊*])", re.I)
INSTRUCTION_RX = re.compile(r"(してください|して下さい|下さい|ください|すること|こと。|を記入|を記載|記入して|記載して|記入する|記載する|添付|"
                            r"の場合|については|注意|※|請填寫|請勾選|請填|填寫|請於|請依|請附|說明|应填写|请填写|请|"
                            r"기재|기입|작성하|입력하|적어|써\s*주|하십시오|바랍니다|해\s*주|※|"
                            r"please|enter |write |insert |fill in|attach|tick|state |describe|if applicable)", re.I)
VALUE_RX = re.compile(r"(\d|[○〇●◯◎×✓✔☑■▓]|[ＸX]{2,}|[A-Z]{2,}\d|[぀-ゟ]{2,}[一-鿿]{1,3}|"
                      r"[一-龥]{1,2}(太郎|花子|一郎|次郎|三郎|大同|小明|美|子)\b|홍길동|김철수|이영희)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def added_fragments(blank: str, example: str) -> list[str]:
    a, b = _norm(blank), _norm(example)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [b[j1:j2] for op, _i1, _i2, j1, j2 in sm.get_opcodes() if op in ("insert", "replace")]


def check(blank: str, example: str) -> dict:
    frags = [LABEL_RX.sub("", f) for f in added_fragments(blank, example)]
    frags = [f for f in frags if f]
    added = sum(len(f) for f in frags)
    instr = sum(len(f) for f in frags if INSTRUCTION_RX.search(f) and not VALUE_RX.search(f))
    value = sum(len(m.group(0)) for f in frags for m in VALUE_RX.finditer(f))
    if added < MIN_ADDED:
        verdict = "no_fill"
    elif added and instr / added >= GUIDANCE_SHARE and value < MAX_VALUE_CHARS:
        verdict = "guidance_only"
    else:
        verdict = "filled"
    return {"version": FILLCHECK_VERSION, "verdict": verdict, "added_chars": added, "instruction_chars": instr,
            "value_chars": value, "fragments": len(frags)}
