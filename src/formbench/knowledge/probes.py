"""Empirical probes of editor behaviour (second evidence class; configs/knowledge/probes.yaml).

``run_writer`` starts LibreOffice with a throw-away profile and the headless VCL backend, drives it with
knowledge/drivers/writer_uitest.py (real key events through LibreOffice's UI-test service) and stores the observed
states under data/knowledge/probes/writer/. ``judge`` compares an observation with the probe's ``expect``.
Word (COM + key events on the Windows desktop) and Hangul drivers are separate; Hangul is not installed on this
machine (blocked_by_environment)."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from ..util import load_yaml, path, read_json, sha256_bytes, utcnow, write_json

LO_PROGRAM = Path(os.path.expanduser("~/opt/lo/root/opt/libreoffice26.2/program"))
DRIVER_DIR = Path(__file__).parent / "drivers"


def load_probes() -> dict:
    return load_yaml(path("configs", "knowledge", "probes.yaml"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def judge(expect: dict, observed: dict) -> bool | None:
    """True / False against ``expect``; None when the probe only records (empty expect)."""
    if not expect:
        return None
    for k, v in expect.items():
        o = observed.get(k)
        if isinstance(v, dict) and isinstance(o, dict):
            if any(o.get(kk) != vv for kk, vv in v.items()):
                return False
        elif o != v:
            return False
    return True


def run_writer(only: list[str] | None = None, log=print) -> dict:
    probes = [p for p in load_probes()["probes"] if not only or p["id"] in only]
    driver = DRIVER_DIR / "writer_uitest.py"
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="fb_lo_probe_") as tmp:
        prof = Path(tmp) / "profile"
        pj, out = Path(tmp) / "probes.json", Path(tmp) / "out.json"
        pj.write_text(json.dumps(probes, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, SAL_USE_VCLPLUGIN="svp")

        def _core1():
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (1, 1))     # WSL crash dumps otherwise (memory note)
            os.setsid()
        proc = subprocess.Popen([str(LO_PROGRAM / "soffice"), "--norestore", "--nologo", "--nodefault", "--nolockcheck",
                                 f"-env:UserInstallation=file://{prof}", f"--accept=socket,host=127.0.0.1,port={port};urp;"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=_core1)
        try:
            r = subprocess.run([str(LO_PROGRAM / "python"), str(driver), str(pj), str(out), str(port)],
                               capture_output=True, text=True, timeout=600, preexec_fn=lambda: __import__("resource").setrlimit(
                                   __import__("resource").RLIMIT_CORE, (1, 1)))
            if not out.exists():
                return {"status": "failed", "reason": (r.stderr or r.stdout)[-800:]}
            res = json.loads(out.read_text(encoding="utf-8"))
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    res.update({"driver": driver.name, "driver_sha256": sha256_bytes(driver.read_bytes()), "probes_version": load_probes()["version"],
                "probe_defs_sha256": sha256_bytes(json.dumps(probes, sort_keys=True, ensure_ascii=False).encode()), "ran_at": utcnow()})
    for p in probes:
        r = res["results"].get(p["id"], {})
        r["concept"] = p["concept"]
        r["verdict"] = None if "error" in r else judge(p.get("expect") or {}, r.get("observed") or {})
    d = path("data", "knowledge", "probes", "writer")
    Path(d).mkdir(parents=True, exist_ok=True)
    fn = Path(d) / f"{res['ran_at'].replace(':', '')}.json"
    write_json(fn, res)
    write_json(Path(d) / "latest.json", res)
    log(f"writer probes -> {fn}")
    return {"status": "ok", "file": str(fn), "product": res.get("product"),
            "verdicts": {k: v.get("verdict") if "error" not in v else "error" for k, v in res["results"].items()}}


def latest(editor: str) -> dict | None:
    p = Path(path("data", "knowledge", "probes", editor, "latest.json"))
    return read_json(p) if p.exists() else None


def _store(editor: str, res: dict, probes: list[dict], driver: Path, log=print) -> dict:
    res.update({"driver": driver.name, "driver_sha256": sha256_bytes(driver.read_bytes()), "probes_version": load_probes()["version"],
                "probe_defs_sha256": sha256_bytes(json.dumps(probes, sort_keys=True, ensure_ascii=False).encode()), "ran_at": utcnow()})
    for p in probes:
        r = res.setdefault("results", {}).get(p["id"], {})
        r["concept"] = p["concept"]
        r["verdict"] = None if "error" in r else judge(p.get("expect") or {}, r.get("observed") or {})
        res["results"][p["id"]] = r
    d = Path(path("data", "knowledge", "probes", editor))
    d.mkdir(parents=True, exist_ok=True)
    fn = d / f"{res['ran_at'].replace(':', '')}.json"
    write_json(fn, res)
    write_json(d / "latest.json", res)
    log(f"{editor} probes -> {fn}")
    return {"status": "ok", "file": str(fn), "product": res.get("product"),
            "verdicts": {k: v.get("verdict") if "error" not in v else "error" for k, v in res["results"].items()}}


def run_word(only: list[str] | None = None, log=print) -> dict:
    """Word on the Windows desktop through WSL interop. Opens a visible Word window and sends key events to it
    (the user is told beforehand; the driver aborts if Word is already running or loses the foreground)."""
    import shutil
    from ..corpus.review_open import _winpath, windows_downloads
    ps = shutil.which("powershell.exe")
    base = windows_downloads()
    if not ps or not base:
        return {"status": "blocked_by_environment", "reason": "Windows interop / PowerShell not available"}
    probes = [p for p in load_probes()["probes"] if not only or p["id"] in only]
    driver = DRIVER_DIR / "word_sendkeys.ps1"
    work = base.parent / "AppData" / "Local" / "Temp" / "formbench_probe"
    work.mkdir(parents=True, exist_ok=True)
    (work / "word_sendkeys.ps1").write_bytes(driver.read_bytes())
    (work / "probes.json").write_text(json.dumps(probes, ensure_ascii=False), encoding="utf-8")
    out = work / "out.json"
    out.unlink(missing_ok=True)
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _winpath(work / "word_sendkeys.ps1"),
                        _winpath(work / "probes.json"), _winpath(out)], capture_output=True, text=True, errors="replace", timeout=900)
    if not out.exists():
        return {"status": "failed", "reason": (r.stderr or r.stdout)[-800:]}
    res = json.loads(out.read_text(encoding="utf-8-sig"))
    if res.get("status") != "ok":
        return res
    return _store("word", res, probes, driver, log)
