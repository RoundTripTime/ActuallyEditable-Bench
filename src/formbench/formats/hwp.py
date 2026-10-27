"""HWP 5.x (OLE compound file) adapter: metadata + text extraction in Python (olefile), and HWP -> HWPX conversion /
independent HWPX parsing through the external Java tool (hwplib + hwpxlib + hwp2hwpx, Apache-2.0)."""
from __future__ import annotations

import functools
import glob
import io
import json
import os
import shutil
import struct
import tempfile
import zlib
from pathlib import Path

from ..util import Status, run_cmd

TAG_PARA_TEXT, TAG_TABLE, TAG_CTRL_HEADER = 0x10 + 51, 0x10 + 61, 0x10 + 55
_EXT_CTRL = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}      # 8-wchar controls
_INLINE_CTRL = {4, 5, 6, 7, 8, 9, 19, 20}


def hwp_info(data: bytes) -> dict:
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        hdr = ole.openstream("FileHeader").read(256)
        ver = struct.unpack("<I", hdr[32:36])[0]
        flags = struct.unpack("<I", hdr[36:40])[0]
        info = {"version": f"{ver >> 24}.{(ver >> 16) & 255}.{(ver >> 8) & 255}.{ver & 255}", "compressed": bool(flags & 1),
                "encrypted": bool(flags & 2), "distribution": bool(flags & 4), "has_script": bool(flags & 8), "drm": bool(flags & 16)}
        sections = sorted(("/".join(s) for s in ole.listdir() if s and s[0] == "BodyText"), key=lambda n: int("".join(c for c in n if c.isdigit()) or 0))
        info["sections"] = len(sections)
        text, tables = [], 0
        if not info["encrypted"] and not info["distribution"]:
            for name in sections:
                raw = ole.openstream(name).read()
                if info["compressed"]:
                    try:
                        raw = zlib.decompress(raw, -15)
                    except zlib.error:
                        continue
                pos = 0
                while pos + 4 <= len(raw):
                    h = struct.unpack("<I", raw[pos:pos + 4])[0]
                    tag, size = h & 0x3FF, h >> 20
                    pos += 4
                    if size == 0xFFF:
                        size = struct.unpack("<I", raw[pos:pos + 4])[0]
                        pos += 4
                    body = raw[pos:pos + size]
                    pos += size
                    if tag == TAG_PARA_TEXT:
                        text.append(_para_text(body))
                    elif tag == TAG_TABLE:
                        tables += 1
        info["text"] = "\n".join(t for t in text if t)
        info["tables"] = tables
        if ole.exists("PrvText"):
            info["preview_text"] = ole.openstream("PrvText").read().decode("utf-16-le", "replace")[:2000]
        info["bin_items"] = sum(1 for s in ole.listdir() if s and s[0] == "BinData")
        return info
    finally:
        ole.close()


def _para_text(body: bytes) -> str:
    out, i, n = [], 0, len(body) // 2
    while i < n:
        ch = struct.unpack_from("<H", body, i * 2)[0]
        if ch in _EXT_CTRL or ch in _INLINE_CTRL:
            i += 8
            continue
        if ch == 9:
            out.append("\t")
            i += 8
            continue
        if ch in (10, 13):
            out.append("\n" if ch == 10 else "")
        elif ch == 24:
            out.append("-")
        elif ch in (30, 31):
            out.append(" ")
        elif ch >= 32:
            out.append(chr(ch))
        i += 1
    return "".join(out).strip()


# ----------------------------------------------------------------------------- external Java tool
@functools.lru_cache(maxsize=1)
def java_tool() -> dict:
    """Locate java + jars. Returns {available, reason, cmd}."""
    java = shutil.which("java") or (os.path.expanduser("~/.sdkman/candidates/java/current/bin/java") if os.path.exists(os.path.expanduser("~/.sdkman/candidates/java/current/bin/java")) else None)
    jars = os.environ.get("FORMBENCH_JARS") or os.path.expanduser("~/opt/jars")
    if not java:
        return {"available": False, "reason": "java runtime not found"}
    libs = sorted(glob.glob(os.path.join(jars, "hwplib-*.jar")) + glob.glob(os.path.join(jars, "hwpxlib-*.jar")))
    classes = os.path.join(jars, "classes")
    if len(libs) < 2 or not os.path.exists(os.path.join(classes, "FbTool.class")):
        return {"available": False, "reason": f"hwplib/hwpxlib jars or compiled FbTool missing under {jars} (see scripts/setup_tools.sh)"}
    return {"available": True, "reason": "", "cmd": [java, "-cp", os.pathsep.join(libs + [classes]), "FbTool"],
            "versions": {"hwplib": Path(libs[0]).stem, "hwpxlib": Path(libs[1]).stem, "hwp2hwpx": "main@2026-08-31"}}


def hwp_to_hwpx(data: bytes, timeout: float = 120) -> tuple[bytes | None, str, str]:
    """Returns (hwpx bytes | None, status, detail). The converter's zip is re-packed by HwpxPackage on first save."""
    tool = java_tool()
    if not tool["available"]:
        return None, Status.BLOCKED_BY_ENVIRONMENT, tool["reason"]
    with tempfile.TemporaryDirectory(prefix="fb_hwp_") as td:
        src, dst = os.path.join(td, "in.hwp"), os.path.join(td, "out.hwpx")
        Path(src).write_bytes(data)
        rc, out, err = run_cmd(tool["cmd"] + ["convert", src, dst], timeout=timeout)
        if rc != 0 or not os.path.exists(dst):
            return None, Status.FAILED, (out or err).strip()[-300:]
        return Path(dst).read_bytes(), Status.OK, ""


def hwpxlib_check(data: bytes, timeout: float = 180) -> dict:
    """Independent parse of an HWPX file with hwpxlib (cross-validation of files written by formbench)."""
    tool = java_tool()
    if not tool["available"]:
        return {"status": Status.BLOCKED_BY_ENVIRONMENT, "reason": tool["reason"]}
    with tempfile.TemporaryDirectory(prefix="fb_chk_") as td:
        src = os.path.join(td, "in.hwpx")
        Path(src).write_bytes(data)
        rc, out, err = run_cmd(tool["cmd"] + ["check", src], timeout=timeout)
        if rc == -9:
            return {"status": Status.TIMEOUT, "reason": "external parser did not finish in time (not a verdict on the file)"}
        try:
            res = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            # the tool always prints a JSON verdict; anything else is a tool failure, not evidence about the document
            res = {"status": Status.FAILED, "reason": (err or out)[-200:]}
        return res


def hwpxlib_text(data: bytes, timeout: float = 60) -> str | None:
    tool = java_tool()
    if not tool["available"]:
        return None
    with tempfile.TemporaryDirectory(prefix="fb_txt_") as td:
        src = os.path.join(td, "in.hwpx")
        Path(src).write_bytes(data)
        rc, out, _ = run_cmd(tool["cmd"] + ["text", src], timeout=timeout)
        return out if rc == 0 else None
