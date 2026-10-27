"""File-type sniffing and hostile-document defences (AGENTS.md §2 security list, test #13).

Checks: magic signature vs extension/MIME, ZIP bomb limits, path traversal, XXE/DOCTYPE, encrypted or
protected packages, embedded macros/scripts, external references (never dereferenced), optional AV scan.
"""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass, field

from ..util import Status, run_cmd

MAX_ZIP_ENTRIES = 5000
MAX_ZIP_TOTAL = 400 * 1024 * 1024
MAX_ZIP_ENTRY = 150 * 1024 * 1024
MAX_ZIP_RATIO = 200.0

EXT_BY_FORMAT = {
    "hwpx": {".hwpx"}, "hwp": {".hwp"}, "docx": {".docx"}, "odt": {".odt"}, "pdf": {".pdf"}, "png": {".png"},
    "jpeg": {".jpg", ".jpeg"}, "tiff": {".tif", ".tiff"}, "doc": {".doc"}, "zip": {".zip"}, "html": {".html", ".htm"},
    "hwpml": {".hml"},
}
MIME_BY_FORMAT = {
    "hwpx": "application/hwp+zip", "hwp": "application/x-hwp", "pdf": "application/pdf", "png": "image/png",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text", "jpeg": "image/jpeg", "tiff": "image/tiff",
    "doc": "application/msword", "zip": "application/zip", "html": "text/html", "unknown": "application/octet-stream",
    "hwpml": "application/xml",
}


def sniff_format(data: bytes) -> str:
    """Identify the real format from content signatures (never trust the extension)."""
    head = data[:8]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        try:
            import olefile
            ole = olefile.OleFileIO(io.BytesIO(data))
            names = {"/".join(s) for s in ole.listdir()}
            ole.close()
            if "FileHeader" in names and any(n.startswith("BodyText") for n in names):
                return "hwp"
            if "WordDocument" in names:
                return "doc"
        except Exception:
            return "ole_unknown"
        return "ole_unknown"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                if "mimetype" in names:
                    mt = z.read("mimetype")[:80].strip()
                    if mt.startswith(b"application/hwp+zip") or mt.startswith(b"application/haansofthwp"):
                        return "hwpx"
                    if mt.startswith(b"application/vnd.oasis.opendocument.text"):
                        return "odt"
                if "Contents/content.hpf" in names or any(n.startswith("Contents/section") for n in names):
                    return "hwpx"
                if "word/document.xml" in names:
                    return "docx"
        except zipfile.BadZipFile:
            return "zip_corrupt"
        return "zip"
    stripped = data[:2048].lstrip().lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html") or b"<html" in stripped:
        return "html"
    if stripped.startswith(b"<?xml") and b"<hwpml" in data[:4096].lower():
        return "hwpml"
    return "unknown"


@dataclass
class SecurityReport:
    format: str
    ok: bool = True
    findings: list[dict] = field(default_factory=list)
    protected: bool = False
    has_macros: bool = False
    external_refs: list[str] = field(default_factory=list)
    av_status: str = Status.BLOCKED_BY_ENVIRONMENT

    def fail(self, code: str, detail: str) -> None:
        self.ok = False
        self.findings.append({"code": code, "detail": detail, "severity": "block"})

    def warn(self, code: str, detail: str) -> None:
        self.findings.append({"code": code, "detail": detail, "severity": "warn"})

    def to_json(self) -> dict:
        return {"format": self.format, "ok": self.ok, "findings": self.findings, "protected": self.protected,
                "has_macros": self.has_macros, "external_refs": self.external_refs[:50], "av_status": self.av_status}


_XXE_RX = re.compile(rb"<!DOCTYPE|<!ENTITY", re.I)
_EXT_URL_RX = re.compile(rb"(?:https?|ftp|file)://[^\s\"'<>]{4,200}")


def check_zip(data: bytes, rep: SecurityReport) -> zipfile.ZipFile | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        rep.fail("zip_corrupt", str(e))
        return None
    infos = z.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        rep.fail("zip_too_many_entries", str(len(infos)))
        return None
    total = 0
    seen = set()
    for zi in infos:
        name = zi.filename
        if name.startswith("/") or name.startswith("\\") or ".." in name.replace("\\", "/").split("/") or ":" in name.split("/")[0]:
            rep.fail("path_traversal", name[:200])
            return None
        if name in seen:
            rep.fail("zip_duplicate_entry", name[:200])
            return None
        seen.add(name)
        if (zi.external_attr >> 16) & 0o170000 == 0o120000:
            rep.fail("zip_symlink", name[:200])
            return None
        total += zi.file_size
        if zi.file_size > MAX_ZIP_ENTRY or total > MAX_ZIP_TOTAL:
            rep.fail("zip_bomb_size", f"{name[:100]} size={zi.file_size} total={total}")
            return None
        if zi.compress_size > 0 and zi.file_size / zi.compress_size > MAX_ZIP_RATIO and zi.file_size > 1_000_000:
            rep.fail("zip_bomb_ratio", f"{name[:100]} ratio={zi.file_size / zi.compress_size:.0f}")
            return None
        if zi.flag_bits & 0x1:
            rep.protected = True
            rep.fail("zip_encrypted_entry", name[:200])
            return None
    return z


def read_zip_entry(z: zipfile.ZipFile, name: str, limit: int = MAX_ZIP_ENTRY) -> bytes:
    """Bounded read that does not trust the declared size."""
    with z.open(name) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"zip entry exceeds limit: {name}")
    return data


def scan_bytes(data: bytes, declared_ext: str | None = None, declared_mime: str | None = None,
               run_av: bool = False) -> SecurityReport:
    fmt = sniff_format(data)
    rep = SecurityReport(format=fmt)
    if declared_ext:
        ext = declared_ext.lower()
        ok_exts = EXT_BY_FORMAT.get(fmt, set())
        if ok_exts and ext and ext not in ok_exts:
            rep.warn("extension_mismatch", f"declared {ext} but content is {fmt}")
    if declared_mime and fmt in ("html",) and "html" not in declared_mime:
        rep.warn("mime_mismatch", f"declared {declared_mime} but content is html")
    if fmt in ("zip_corrupt", "ole_unknown", "unknown"):
        rep.fail("unrecognised_format", fmt)
        return rep
    if fmt in ("hwpx", "docx", "odt", "zip"):
        z = check_zip(data, rep)
        if z is None:
            return rep
        try:
            for zi in z.infolist():
                low = zi.filename.lower()
                if low.endswith((".xml", ".hpf", ".rels", ".rdf")) and zi.file_size < 60 * 1024 * 1024:
                    xml = read_zip_entry(z, zi.filename)
                    if _XXE_RX.search(xml[:20000]):
                        rep.fail("xml_doctype_or_entity", zi.filename[:200])
                    if b"encryption-data" in xml[:200000] and low.endswith("manifest.xml"):
                        rep.protected = True
                        rep.fail("encrypted_package", zi.filename)
                    for m in _EXT_URL_RX.findall(xml[:2_000_000]):
                        u = m.decode("latin-1")
                        if not any(k in u for k in ("hancom.co.kr", "w3.org", "idpf.org", "oasis", "purl.org", "openxmlformats.org",
                                                    "microsoft.com", "openoffice.org", "xmlsoap.org")):
                            rep.external_refs.append(u)
                if low.endswith(("vbaproject.bin", ".js", ".vbs", ".bas")) or low.startswith(("scripts/", "basic/")):
                    payload = read_zip_entry(z, zi.filename) if zi.file_size < 5_000_000 else b"x"
                    if payload.strip():
                        rep.has_macros = True
                        rep.warn("embedded_script", zi.filename[:200])
        except Exception as e:  # corrupt member
            rep.fail("zip_member_unreadable", str(e)[:200])
        finally:
            z.close()
    elif fmt == "doc":                                   # legacy Word: macros live in OLE storages; never executed, only flagged
        try:
            import olefile
            with olefile.OleFileIO(io.BytesIO(data)) as ole:
                names = ["/".join(x) for x in ole.listdir()]
            if any(n.lower().startswith(("macros", "_vba_project")) or "/vba/" in n.lower() for n in names):
                rep.has_macros = True
                rep.warn("embedded_script", "doc VBA storage")
            if any(n.lower().startswith("encryptedpackage") for n in names):
                rep.protected = True
                rep.fail("encrypted_package", "doc")
        except Exception as e:      # noqa: BLE001
            rep.fail("ole_unreadable", str(e)[:200])
    elif fmt == "hwp":
        try:
            import olefile
            ole = olefile.OleFileIO(io.BytesIO(data))
            hdr = ole.openstream("FileHeader").read(256)
            flags = int.from_bytes(hdr[36:40], "little") if len(hdr) >= 40 else 0
            if flags & 0x2:
                rep.protected = True
                rep.fail("hwp_password_protected", "FileHeader flag bit1")
            if flags & 0x10:
                rep.protected = True
                rep.warn("hwp_drm", "FileHeader flag bit4")
            if any(s and s[0] == "Scripts" for s in ole.listdir()):
                rep.warn("embedded_script_storage", "Scripts storage present (never executed)")
            ole.close()
        except Exception as e:
            rep.fail("hwp_unreadable", str(e)[:200])
    elif fmt == "pdf":
        if b"/Encrypt" in data[-4096:] or b"/Encrypt" in data[:4096]:
            rep.protected = True
            rep.warn("pdf_encrypted", "Encrypt dictionary present")
        for key in (b"/JavaScript", b"/Launch", b"/OpenAction"):
            if key in data:
                rep.warn("pdf_active_content", key.decode())
                rep.has_macros = rep.has_macros or key != b"/OpenAction"
    if run_av:
        rep.av_status = av_scan_bytes(data)
        if rep.av_status == "infected":
            rep.fail("av_infected", "clamscan reported a signature match")
    return rep


def av_available() -> bool:
    return shutil.which("clamscan") is not None or shutil.which("clamdscan") is not None


def av_scan_bytes(data: bytes) -> str:
    """ClamAV adapter. Returns clean|infected|blocked_by_environment|failed."""
    exe = shutil.which("clamdscan") or shutil.which("clamscan")
    if not exe:
        return Status.BLOCKED_BY_ENVIRONMENT
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(data)
        f.flush()
        rc, _out, _err = run_cmd([exe, "--no-summary", f.name], timeout=120)
    return {0: "clean", 1: "infected"}.get(rc, Status.FAILED)
