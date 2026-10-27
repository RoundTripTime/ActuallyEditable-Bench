"""'Open pair' for the local review server (user request 2026-10-15): one button downloads both files of a candidate
pair to the Windows Downloads folder and opens them side by side, blank on the left half of the primary screen,
completed example on the right half.

The review server runs in WSL; Windows is reached through interop (powershell.exe). Each file gets a Zone.Identifier
stream (ZoneId=3, 'downloaded from the internet') so Office opens it in Protected View: these are untrusted web
documents. Windows are found by the unique file name in their title and moved with user32 MoveWindow.
Outside WSL (no powershell.exe) the files are only copied and the caller reports that."""
from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

from ..collection.discovery import blob_path
from ..util import DB, path

PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices;
public class FbWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int hh, bool r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  public static IntPtr Find(string key) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      var sb = new StringBuilder(1024); GetWindowText(h, sb, 1024);
      if (sb.ToString().IndexOf(key, StringComparison.OrdinalIgnoreCase) >= 0) { found = h; return false; }
      return true; }, IntPtr.Zero);
    return found;
  }
}
'@
$L = '__LEFT__'; $R = '__RIGHT__'; $kL = '__KEYL__'; $kR = '__KEYR__'
foreach ($f in @($L, $R)) { Set-Content -Path $f -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3" }
Start-Process -FilePath $L
Start-Sleep -Milliseconds 1500
Start-Process -FilePath $R
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$half = [int]($wa.Width / 2)
$hl = [IntPtr]::Zero; $hr = [IntPtr]::Zero
for ($i = 0; $i -lt 90; $i++) {
  if ($hl -eq [IntPtr]::Zero) { $hl = [FbWin]::Find($kL) }
  if ($hr -eq [IntPtr]::Zero) { $hr = [FbWin]::Find($kR) }
  if ($hl -ne [IntPtr]::Zero -and $hr -ne [IntPtr]::Zero) { break }
  Start-Sleep -Milliseconds 500
}
Start-Sleep -Milliseconds 800
if ($hl -ne [IntPtr]::Zero) { [FbWin]::ShowWindow($hl, 9) | Out-Null; [FbWin]::MoveWindow($hl, $wa.X, $wa.Y, $half, $wa.Height, $true) | Out-Null }
if ($hr -ne [IntPtr]::Zero) { [FbWin]::ShowWindow($hr, 9) | Out-Null; [FbWin]::MoveWindow($hr, $wa.X + $half, $wa.Y, $wa.Width - $half, $wa.Height, $true) | Out-Null; [FbWin]::SetForegroundWindow($hr) | Out-Null }
"left=$($hl -ne [IntPtr]::Zero) right=$($hr -ne [IntPtr]::Zero)"
"""


@functools.lru_cache(maxsize=1)
def windows_downloads() -> Path | None:
    """WSL path of the Windows user's Downloads folder (None outside WSL)."""
    exe = shutil.which("cmd.exe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "/c", "echo %USERPROFILE%"], capture_output=True, text=True, errors="replace", timeout=20).stdout.strip().splitlines()[-1].strip()
        wsl = subprocess.run(["wslpath", "-u", out], capture_output=True, text=True, errors="replace", timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    p = Path(wsl) / "Downloads"
    return p if p.exists() else None


def _winpath(p: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, errors="replace", timeout=10).stdout.strip()


def open_pair(db: DB, pair_id: str, log=print) -> dict:
    row = db.one("SELECT template_id, completed_id FROM pairs WHERE pair_id=?", (pair_id,))
    if not row:
        raise ValueError(f"unknown pair {pair_id}")
    docs = [db.get_doc(row["template_id"]), db.get_doc(row["completed_id"])]
    if not all(docs) or not all(blob_path(d["sha256"]).exists() for d in docs):
        raise ValueError("file of this pair is not in the store")
    base = windows_downloads()
    dest = (base / "formbench_review" / pair_id.replace("~", "__")) if base else path("reports", "review", "open", pair_id.replace("~", "__"))
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for side, d in (("L", docs[0]), ("R", docs[1])):
        key = f"FB{side}_{d['document_id'][4:12]}"
        f = dest / f"{key}_{'blank' if side == 'L' else 'example'}.{d.get('original_format') or 'bin'}"
        shutil.copyfile(blob_path(d["sha256"]), f)
        files.append((f, key))
    ps = shutil.which("powershell.exe")
    if not base or not ps:
        return {"status": "copied", "folder": str(dest), "files": [str(f) for f, _ in files],
                "reason": "Windows interop not available: files copied, open them manually"}
    script = (PS_TEMPLATE.replace("__LEFT__", _winpath(files[0][0]).replace("'", "''"))
              .replace("__RIGHT__", _winpath(files[1][0]).replace("'", "''")).replace("__KEYL__", files[0][1]).replace("__KEYR__", files[1][1]))
    sp = dest / "open_pair.ps1"
    sp.write_text(script, encoding="utf-8-sig")
    subprocess.Popen([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _winpath(sp)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    log(f"open pair {pair_id}: {dest}")
    return {"status": "opening", "folder": _winpath(dest), "files": [f.name for f, _ in files]}
