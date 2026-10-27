"""Render documents to PDF with their NATIVE editor (experiment v3, 2026-10-16):
DOCX -> Microsoft Word, HWPX -> Hancom Hangul (both through COM automation on the Windows host, hidden, read-only,
no key events, aborting if the user has the application open), ODT -> LibreOffice (headless).
These PDFs are the reconstruction inputs and the visual-similarity reference; the internal layout engine is only used
for hit testing inside the editing sessions."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ...util import path, run_cmd, sha256_bytes

HERE = Path(__file__).parent


def _win_tmp() -> Path:
    from ...corpus.review_open import windows_downloads
    base = windows_downloads()
    if base is None:
        raise RuntimeError("Windows interop not available")
    d = base.parent / "AppData" / "Local" / "Temp" / "formbench_render"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _com_batch(script: str, items: list[tuple[bytes, str]], ext: str, timeout: int = 7200) -> dict:
    """items: (document bytes, key). Returns key -> {status, pdf bytes, pages, renderer, reason}."""
    from ...corpus.review_open import _winpath
    ps = shutil.which("powershell.exe")
    if not ps:
        return {k: {"status": "blocked_by_environment", "reason": "powershell.exe not available"} for _, k in items}
    work = _win_tmp()
    for f in work.glob("*"):
        f.unlink()
    (work / script).write_bytes((HERE / script).read_bytes())
    lines = []
    for data, key in items:
        src, dst = work / f"{key}.{ext}", work / f"{key}.pdf"
        src.write_bytes(data)
        lines.append(f"{_winpath(src)}\t{_winpath(dst)}")
    (work / "list.txt").write_text("\n".join(lines), encoding="utf-8")
    out = work / "out.jsonl"
    subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _winpath(work / script), _winpath(work / "list.txt"),
                    _winpath(out)], capture_output=True, text=True, errors="replace", timeout=timeout)
    res, renderer = {}, None
    if not out.exists():
        return {k: {"status": "failed", "reason": "no output from the COM script"} for _, k in items}
    recs = [json.loads(x) for x in out.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    if recs and recs[0].get("status") == "aborted":
        return {k: {"status": "blocked", "reason": recs[0]["reason"]} for _, k in items}
    renderer = recs[0].get("renderer") if recs else None
    by_src = {Path(r["src"].replace("\\", "/")).name: r for r in recs[1:] if r.get("src")}
    for data, key in items:
        r = by_src.get(f"{key}.{ext}", {"status": "failed", "reason": "no record"})
        pdf = work / f"{key}.pdf"
        if r.get("status") == "ok" and pdf.exists():
            b = pdf.read_bytes()
            res[key] = {"status": "ok", "pdf": b, "pages": r.get("pages"), "renderer": renderer, "sha256": sha256_bytes(b)}
        else:
            res[key] = {"status": r.get("status", "failed"), "reason": r.get("reason", ""), "renderer": renderer}
    return res


def word_pdf(items):
    return _com_batch("word_pdf.ps1", items, "docx")


def hwp_pdf(items):
    return _com_batch("hwp_pdf.ps1", items, "hwpx")


def libreoffice_pdf(items, profile: str | None = None):
    """ODT (or any LibreOffice-readable) bytes -> PDF with headless LibreOffice."""
    import tempfile

    from ..renderers import find_soffice
    so = find_soffice()
    out = {}
    if not so:
        return {k: {"status": "blocked_by_environment", "reason": "soffice not found"} for _, k in items}
    prof = profile or "file:///tmp/claude-1000/lo_render_prof"
    ver = run_cmd([so, "--version"], timeout=60)[1].strip() if run_cmd else ""
    with tempfile.TemporaryDirectory(prefix="fb_lo_pdf_") as td:
        for data, key in items:
            src = Path(td) / f"{key}.odt"
            src.write_bytes(data)
            run_cmd([so, "--headless", "--norestore", f"-env:UserInstallation={prof}", "--convert-to", "pdf", "--outdir", td, str(src)],
                    timeout=300)
            pdf = Path(td) / f"{key}.pdf"
            if pdf.exists():
                b = pdf.read_bytes()
                import pymupdf
                out[key] = {"status": "ok", "pdf": b, "pages": len(pymupdf.open(stream=b)), "renderer": ver or "LibreOffice",
                            "sha256": sha256_bytes(b)}
            else:
                out[key] = {"status": "failed", "reason": "LibreOffice produced no PDF"}
    return out


def render_native(fmt: str, items):
    return {"docx": word_pdf, "hwpx": hwp_pdf, "odt": libreoffice_pdf}[fmt](items)


__all__ = ["render_native", "word_pdf", "hwp_pdf", "libreoffice_pdf", "path"]


WORD_OPT_KEY = r"HKCU\Software\Microsoft\Office\16.0\Word\Options"


def _reg_get(key: str, name: str):
    r = subprocess.run(["reg.exe", "query", key, "/v", name], capture_output=True, text=True, errors="replace")
    for ln in r.stdout.splitlines():
        if name in ln and "REG_DWORD" in ln:
            return int(ln.split()[-1], 16)
    return None


def _reg_set(key: str, name: str, val):
    if val is None:
        subprocess.run(["reg.exe", "delete", key, "/v", name, "/f"], capture_output=True)
    else:
        subprocess.run(["reg.exe", "add", key, "/v", name, "/t", "REG_DWORD", "/d", str(val), "/f"], capture_output=True)


def _clean_word_resiliency(marker: str = "formbench_p2o") -> list[str]:
    """Remove ONLY the Word crash-memory entries (Resiliency) that point to this runner's work directory; a killed Word
    otherwise asks "error the last time you opened X, open anyway?" on the next run. Other entries are left untouched."""
    removed = []
    base = r"HKCU\Software\Microsoft\Office\16.0\Word\Resiliency"
    r = subprocess.run(["reg.exe", "query", base, "/s"], capture_output=True, text=True, errors="replace")
    key = None
    for ln in r.stdout.splitlines():
        if ln.startswith("HKEY_"):
            key = ln.strip()
        elif "REG_BINARY" in ln and key:
            name, _, hexv = ln.split()[:3]
            try:
                txt = bytes.fromhex(hexv).decode("utf-16-le", errors="ignore").lower()
            except ValueError:
                continue
            if marker in txt:
                subprocess.run(["reg.exe", "delete", key, "/v", name, "/f"], capture_output=True)
                removed.append(f"{key}\\{name}")
    return removed


def _own_dialogs(ps: str, work: Path, pids: list[str]) -> list[str]:
    """Visible windows (and their texts) of the given application processes; used to detect modal error dialogs."""
    from ...corpus.review_open import _winpath
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _winpath(work / "winlist.ps1")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    out, cur = [], None
    for ln in r.stdout.replace("\ufeff", "").splitlines():
        if ln.startswith("PID "):
            cur = ln.split()[1]
        elif cur in pids and ln.strip():
            out.append(ln.strip())
    return out


def app_pdf_to_odt(app: str, items: list[tuple[bytes, str]], per_file_s: int = 180, poll_s: float = 3.0, src_ext: str = "pdf") -> dict:
    """Real-world reconstruction baseline: Word ("word") or Hangul ("hwp") opens each PDF and saves ODT.

    One application session per file. While it runs, the application's own windows are polled: a visible modal dialog
    (e.g. Word's "error opening the file, open anyway?") means the conversion is blocked -> the dialog text is recorded
    as the failure reason and ONLY the application process this run started (PID recorded by the script) is killed.
    No dialog is ever answered. Word's PDF-conversion notice is switched off for the run and the user's registry value
    is restored in a finally block here (a killed PowerShell could not restore it).
    src_ext: the input's extension (experiment v5 also saves DOCX / HWPX documents as ODT with their own application)."""
    import time

    from ...corpus.review_open import _winpath
    ps = shutil.which("powershell.exe")
    if not ps:
        return {k: {"status": "blocked_by_environment", "reason": "powershell.exe not available"} for _, k in items}
    work = _win_tmp().parent / "formbench_p2o"
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob("*"):
        f.unlink()
    for sc in ("pdf_to_odt.ps1", "winlist.ps1"):
        (work / sc).write_bytes((HERE / sc).read_bytes())
    old = _reg_get(WORD_OPT_KEY, "DisableConvertPdfWarning") if app == "word" else None
    res = {}
    try:
        if app == "word":
            _clean_word_resiliency()
            _reg_set(WORD_OPT_KEY, "DisableConvertPdfWarning", 1)
        for data, key in items:
            pdf, odt, lst, out = work / f"{key}.{src_ext}", work / f"{key}.odt", work / "list.txt", work / "out.jsonl"
            for f in (odt, out, Path(str(out) + ".pid")):
                f.unlink(missing_ok=True)
            pdf.write_bytes(data)
            lst.write_text(f"{_winpath(pdf)}\t{_winpath(odt)}", encoding="utf-8")
            proc = subprocess.Popen([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _winpath(work / "pdf_to_odt.ps1"),
                                     "-App", app, _winpath(lst), _winpath(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0, blocked, seen = time.monotonic(), None, 0
            while proc.poll() is None:
                time.sleep(poll_s)
                pidf = Path(str(out) + ".pid")
                pids = [p for p in (pidf.read_text().strip().split(",") if pidf.exists() else []) if p.strip().isdigit()]
                dlg = _own_dialogs(ps, work, pids) if pids else []
                # the application runs hidden: a visible Win32 dialog (#32770) of its own seen in two consecutive polls means it
                # waits for an answer nobody will give. Other visible windows (e.g. Hangul's WPF "PDF를 오피스 문서로 변환하기"
                # progress window) are not judged; the per-file time limit catches WPF questions such as "호환 문서".
                seen = seen + 1 if any(d.startswith("V #32770") for d in dlg) else 0
                if seen >= 2 or time.monotonic() - t0 > per_file_s:
                    blocked = ("modal dialog: " if seen >= 2 else f"no result within {per_file_s}s; windows: ") + " / ".join(dlg)[:500]
                    for pid in pids:
                        subprocess.run(["taskkill.exe", "/PID", pid, "/F"], capture_output=True)
                    proc.kill()
                    proc.wait()
                    break
            recs = [json.loads(x) for x in out.read_text(encoding="utf-8-sig").splitlines() if x.strip()] if out.exists() else []
            app_ver = recs[0].get("app") if recs else None
            r = next((x for x in recs[1:] if x.get("src")), None)
            if r and r.get("status") == "ok" and odt.exists():
                b = odt.read_bytes()
                res[key] = {"status": "ok", "odt": b, "app": app_ver, "sha256": sha256_bytes(b), "s": r.get("s")}
            elif r:
                res[key] = {"status": "failed", "reason": r.get("reason", ""), "app": app_ver}
            elif recs and recs[0].get("status") == "aborted":
                res[key] = {"status": "blocked", "reason": recs[0].get("reason", "")}
            else:
                res[key] = {"status": "failed" if blocked and blocked.startswith("modal") else "timeout" if blocked else "failed",
                            "reason": blocked or "no result (application aborted)", "app": app_ver}
    finally:
        if app == "word":
            _reg_set(WORD_OPT_KEY, "DisableConvertPdfWarning", old)
            _clean_word_resiliency()
    return res
