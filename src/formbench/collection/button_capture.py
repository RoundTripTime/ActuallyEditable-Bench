"""Strategy S4.5: documents generated in the browser by a download *button* (no file URL in the page).

Some template sites (e.g. firsthr.app, nominated by the user 2026-10-15) build the DOCX in JavaScript when
'Download (docx)' is pressed, so neither the static HTML nor the rendered DOM contains a link to fetch. Headless
Chrome is driven over the DevTools protocol: the page is opened once (same research User-Agent, no login, no form
submission), every download button that is not a plain file link is pressed once, and the files the page hands to the
browser are saved. Each file keeps what the page says about it -- the button text, its aria-label, the heading of the
card it sits in, and the suggested file name -- because the blank/example role and the pairing key come from those
publisher labels, exactly as for linked files.

Only pages already admitted by PolicyGate reach this module; it is a fallback after the static and rendered link
extraction found no pair, and it shares the host-wide render slots (render_page._slot)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import render_page

CAPTURE_VERSION = "1.0"
MAX_BUTTONS = 24             # per page
CLICK_WAIT_S = 15.0          # per button: download must begin and finish within this time

# Buttons / pseudo-links whose text or label says 'download' and that do not point at a real file (those are fetched
# statically). 'Download all' / ZIP bundles are skipped: one file per form.
FIND_JS = r"""
(() => {
  const rx = /download|다운로드|내려받기/i, skip = /download all|all\s+\d+|\bzip\b|bundle|전체/i;
  const els = [...document.querySelectorAll('button, a, [role=button]')].filter(e => {
    const t = ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')).trim();
    if (!rx.test(t) || skip.test(t)) return false;
    const h = e.getAttribute('href');
    if (h && !h.startsWith('#') && !h.toLowerCase().startsWith('javascript')) return false;
    return true;
  });
  return els.slice(0, %d).map((e, i) => {
    e.setAttribute('data-fb-idx', String(i));
    let n = e, card = null;
    for (let k = 0; k < 8 && n.parentElement; k++) {
      n = n.parentElement;
      if ((n.innerText || '').trim().length > (e.innerText || '').trim().length + 12) { card = n; break; }
    }
    const hd = card ? card.querySelector('h1,h2,h3,h4,h5,h6,strong,b,[class*=title]') : null;
    return {i, text: (e.innerText || '').trim().slice(0, 120), aria: (e.getAttribute('aria-label') || '').slice(0, 200),
            heading: hd ? (hd.innerText || '').trim().slice(0, 200) : '',
            card: card ? (card.innerText || '').trim().slice(0, 400) : ''};
  });
})()
""" % MAX_BUTTONS


def available() -> tuple[bool, str]:
    if not render_page.chrome():
        return False, "headless Chrome not installed"
    try:
        import websocket  # noqa: F401
    except ImportError:
        return False, "python package websocket-client missing (pip install .[capture])"
    return True, ""


class _Cdp:
    """Minimal synchronous DevTools client over one browser websocket (flattened page session)."""

    def __init__(self, ws_url: str, timeout: float):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self.n = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 30.0) -> dict:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        end = time.time() + timeout
        while time.time() < end:
            m = self._recv(end - time.time())
            if m is None:
                continue
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            self.events.append(m)
        raise TimeoutError(method)

    def _recv(self, left: float) -> dict | None:
        import websocket
        self.ws.settimeout(max(0.1, min(left, 2.0)))
        try:
            return json.loads(self.ws.recv())
        except websocket.WebSocketTimeoutException:
            return None

    def wait(self, pred, timeout: float) -> dict | None:
        end = time.time() + timeout
        while True:
            for i, m in enumerate(self.events):
                if pred(m):
                    return self.events.pop(i)
            if time.time() >= end:
                return None
            m = self._recv(end - time.time())
            if m is not None:
                self.events.append(m)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def capture(url: str, user_agent: str, out_dir: Path, load_wait_s: float = 5.0, timeout_s: float = 180.0,
            use_slot: bool = True) -> dict:
    """Open ``url``, press each download button once, save the files under ``out_dir``.

    Returns {"status", "reason", "buttons": [...], "files": [{"path","suggested","text","aria","heading","card","blob_url"}]}.
    status: ok | no_buttons | unavailable | busy | error."""
    ok, why = available()
    if not ok:
        return {"status": "unavailable", "reason": why, "buttons": [], "files": []}
    slot = render_page._slot() if use_slot else None
    if use_slot and slot is None:
        return {"status": "busy", "reason": "no render slot free", "buttons": [], "files": []}
    prof = tempfile.mkdtemp(prefix="fb_capture_")
    dl = tempfile.mkdtemp(prefix="fb_capture_dl_")
    proc = None
    cdp = None
    t0 = time.time()
    try:
        proc = subprocess.Popen([render_page.chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                                 "--disable-extensions", "--blink-settings=imagesEnabled=false", f"--user-data-dir={prof}",
                                 f"--user-agent={user_agent}", "--remote-debugging-port=0", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        port_file = Path(prof) / "DevToolsActivePort"
        while not port_file.exists() or len(port_file.read_text().split()) < 2:
            if time.time() - t0 > 30 or proc.poll() is not None:
                return {"status": "error", "reason": "Chrome did not start", "buttons": [], "files": []}
            time.sleep(0.2)
        port, wpath = port_file.read_text().split()[:2]
        cdp = _Cdp(f"ws://127.0.0.1:{port}{wpath}", timeout=30)
        cdp.call("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": dl, "eventsEnabled": True})
        target = next(t for t in cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page")
        sid = cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
        cdp.call("Page.enable", session=sid)
        cdp.call("Page.navigate", {"url": url}, session=sid)
        cdp.wait(lambda m: m.get("method") == "Page.loadEventFired", 45)
        time.sleep(load_wait_s)
        res = cdp.call("Runtime.evaluate", {"expression": FIND_JS, "returnByValue": True}, session=sid)
        buttons = (res.get("result") or {}).get("value") or []
        if not buttons:
            return {"status": "no_buttons", "reason": "no download button without a file link", "buttons": [], "files": []}
        files = []
        for b in buttons:
            if time.time() - t0 > timeout_s:
                break
            cdp.events = [m for m in cdp.events if not str(m.get("method", "")).startswith("Browser.download")]
            cdp.call("Runtime.evaluate", {"expression": f"document.querySelector('[data-fb-idx=\"{b['i']}\"]').click()"}, session=sid)
            begin = cdp.wait(lambda m: m.get("method") == "Browser.downloadWillBegin", CLICK_WAIT_S)
            if not begin:
                continue
            guid = begin["params"]["guid"]
            done = cdp.wait(lambda m: m.get("method") == "Browser.downloadProgress" and m["params"].get("guid") == guid
                            and m["params"].get("state") in ("completed", "canceled"), CLICK_WAIT_S)
            src = Path(dl) / guid
            if not done or done["params"]["state"] != "completed" or not src.exists():
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{b['i']:02d}_{guid}"
            shutil.move(str(src), dest)
            files.append({"path": str(dest), "suggested": begin["params"].get("suggestedFilename", ""),
                          "blob_url": begin["params"].get("url", "")[:300], **{k: b.get(k, "") for k in ("text", "aria", "heading", "card")}})
        return {"status": "ok", "reason": "", "buttons": buttons, "files": files, "version": CAPTURE_VERSION,
                "seconds": round(time.time() - t0, 1)}
    except Exception as e:                       # one page's failure is recorded, never fatal for the explorer
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"[:300], "buttons": [], "files": []}
    finally:
        if cdp:
            try:
                cdp.call("Browser.close", timeout=5)
            except Exception:
                pass
            cdp.close()
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
        shutil.rmtree(prof, ignore_errors=True)
        shutil.rmtree(dl, ignore_errors=True)
        if slot is not None:
            slot.close()
