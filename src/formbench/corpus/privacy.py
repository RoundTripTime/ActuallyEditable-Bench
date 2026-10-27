"""Privacy review: detect personal-data patterns; anything that looks filled-in or carries PII is held for manual
review and never enters the evaluation set or a public release automatically."""
from __future__ import annotations

import re

PII_PATTERNS = {
    "kr_resident_registration_number": re.compile(r"(?<!\d)\d{6}\s*[-–]\s*[1-4]\d{6}(?!\d)"),
    "phone_number": re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "card_number": re.compile(r"(?<!\d)\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}(?!\d)"),
    "passport_number": re.compile(r"(?<![A-Z0-9])[MSRODG]\d{8}(?!\d)"),
    "us_ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
}
SYNTHETIC_MARKERS = ("example.com", "010-0000-", "000000-0000000", "홍길동", "123-45-6789", "1234-5678")


def scan_pii(text: str) -> dict:
    hits = {}
    for name, rx in PII_PATTERNS.items():
        found = [m.group(0) for m in rx.finditer(text or "")]
        real = [f for f in found if not any(mk in f for mk in SYNTHETIC_MARKERS)]
        if real:
            hits[name] = len(real)          # counts only: matched strings are never stored
    return hits


FILLED_HINT = re.compile(r"filled|completed|submitted|작성본|작성완료|제출본|기재례|sample[_ -]?filled", re.I)


def review(text: str, is_completed: bool | None, is_template: bool | None, source_hint: str = "") -> tuple[str, dict]:
    """Return (privacy_review status, detail). Statuses: auto_cleared_blank_template | flagged_requires_manual_review."""
    hits = scan_pii(text)
    if FILLED_HINT.search(source_hint or ""):
        return "flagged_requires_manual_review", {"reason": "source path/name suggests a filled-in document", "pii_patterns": hits}
    if is_completed:
        return "flagged_requires_manual_review", {"reason": "document looks filled in", "pii_patterns": hits}
    if hits:
        return "flagged_requires_manual_review", {"reason": "personal-data patterns present", "pii_patterns": hits}
    if is_template:
        return "auto_cleared_blank_template", {"pii_patterns": {}}
    return "auto_cleared_no_pii_patterns", {"pii_patterns": {}}
