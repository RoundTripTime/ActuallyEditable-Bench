"""Open a native editing session for a document of any evaluated format (no conversion of the edited document)."""
from __future__ import annotations

from .editor_backends import EditorSession
from .native_docx import NativeDocxSession
from .native_odt import NativeOdtSession

BACKENDS = {"hwpx": EditorSession, "docx": NativeDocxSession, "odt": NativeOdtSession}


def open_session(data: bytes, fmt: str):
    cls = BACKENDS.get(fmt)
    if cls is None:
        raise ValueError(f"no native editing backend for {fmt}")
    return cls.load(data)


def reopen_check(data: bytes, fmt: str) -> dict:
    """File validity after save: the package re-opens in the native backend and (for DOCX / ODT) in the reference
    libraries; returns {ok, reason}."""
    try:
        s = open_session(data, fmt)
        _ = s.ir
        if fmt == "docx":
            import io

            import docx
            docx.Document(io.BytesIO(data))
        elif fmt == "odt":
            import io

            from odf.opendocument import load
            load(io.BytesIO(data))
        return {"ok": True, "reason": ""}
    except Exception as e:  # noqa: BLE001 - any failure to re-open is a validity failure
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:300]}
