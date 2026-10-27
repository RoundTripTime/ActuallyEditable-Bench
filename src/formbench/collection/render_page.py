"""Strategy S4.4: pages whose content is drawn by JavaScript (Angular/React form libraries, board lists loaded by
script) are read through headless Chrome. Used only as a fallback when the static HTML shows no document link; the
page itself was already allowed by PolicyGate. Same research User-Agent, no login, no form submission, one tab."""
from __future__ import annotations

import re
import shutil
import tempfile

from ..util import run_cmd

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
SCRIPTY_RX = re.compile(rb"(ng-version|data-reactroot|id=\"(app|root)\"|__NEXT_DATA__|<app-root|webpackChunk|\.bundle\.js|vue\.(min\.)?js)", re.I)


def chrome() -> str | None:
    return next((p for p in (shutil.which(c) for c in CHROME_CANDIDATES) if p), None)


def looks_scripted(html: bytes) -> bool:
    """A page that probably builds its list in the browser: framework markers, or almost no anchors in a large page."""
    if SCRIPTY_RX.search(html[:300000]):
        return True
    return len(html) > 20000 and len(re.findall(rb"<a\s", html[:300000], re.I)) < 15


MAX_CONCURRENT = 4          # host-wide headless Chrome instances (~300 MB each); many explorer workers share them


def _slot(wait_s: float = 120.0):
    """A host-wide render slot (file lock); None if none frees up in time."""
    import fcntl
    import time
    from ..util import path
    d = path("data", "logs", "render_slots")
    d.mkdir(parents=True, exist_ok=True)
    end = time.time() + wait_s
    while time.time() < end:
        for i in range(MAX_CONCURRENT):
            f = open(d / f"slot{i}.lock", "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                f.close()
        time.sleep(1.0)
    return None


def render(url: str, user_agent: str, wait_ms: int = 12000, timeout_s: float = 60) -> bytes | None:
    exe = chrome()
    if not exe:
        return None
    slot = _slot()
    if slot is None:
        return None
    try:
        return _render(exe, url, user_agent, wait_ms, timeout_s)
    finally:
        slot.close()                                   # releases the lock


def _render(exe: str, url: str, user_agent: str, wait_ms: int, timeout_s: float) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="fb-chrome-") as prof:
        rc, out, _err = run_cmd([exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", "--disable-extensions",
                                 "--blink-settings=imagesEnabled=false", f"--user-data-dir={prof}", f"--user-agent={user_agent}",
                                 f"--virtual-time-budget={wait_ms}", "--dump-dom", url], timeout=timeout_s)
    return out.encode("utf-8") if rc == 0 and out.strip() else None
