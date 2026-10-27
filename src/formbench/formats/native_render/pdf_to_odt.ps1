# Real-world PDF -> ODT reconstruction with a desktop application (experiment v3 baselines).
#   -App word : Microsoft Word opens the PDF (PDF Reflow conversion) and saves OpenDocument Text (wdFormatOpenDocumentText = 23)
#   -App hwp  : Hancom Hangul opens the PDF (PDF import, format auto-detected) and saves ODT (format name "ODF")
# Hidden windows, no key events; aborts if the application is already running. Input: list file "<pdf>`t<odt>".
param([string]$App, [string]$ListPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
$proc = @{word='WINWORD'; hwp='Hwp'}[$App]
if (Get-Process $proc -ErrorAction SilentlyContinue) {
  '{"status":"aborted","reason":"application already running"}' | Set-Content -Encoding UTF8 $OutPath; exit 2 }
$res = New-Object System.Collections.Generic.List[string]
$before = @(Get-Process $proc -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
function Save-Pid { $new = @(Get-Process $proc -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | ForEach-Object { $_.Id })
  ($new -join ",") | Set-Content -Encoding ASCII "$OutPath.pid" }
if ($App -eq 'word') {
  # The PDF-conversion notice is switched off by the Python caller for the duration of the run (and restored there).
  $w = New-Object -ComObject Word.Application; Save-Pid; $w.Visible = $false; $w.DisplayAlerts = 0; $w.AutomationSecurity = 3
  $ver = "Microsoft Word $($w.Version) build $($w.Build)"
  try {
    foreach ($line in Get-Content -Encoding UTF8 $ListPath) {
      if (-not $line.Trim()) { continue }; $src, $dst = $line.Split("`t"); $t0 = Get-Date
      try {
        $d = $w.Documents.Open($src, $false, $true, $false)        # ConfirmConversions=false, ReadOnly
        $d.SaveAs2([ref]$dst, [ref]23)
        $d.Close([ref]0)
        $res.Add((@{src=$src; out=$dst; status='ok'; s=[math]::Round(((Get-Date)-$t0).TotalSeconds,2)} | ConvertTo-Json -Compress))
      } catch { $res.Add((@{src=$src; status='failed'; reason="$($_.Exception.Message)"} | ConvertTo-Json -Compress)); try { foreach ($x in @($w.Documents)) { $x.Close([ref]0) } } catch {} }
    }
  } finally {
    try { $w.Quit([ref]0) } catch {}
  }
} else {
  $h = New-Object -ComObject HWPFrame.HwpObject; Save-Pid; $null = $h.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
  try { $h.XHwpWindows.Item(0).Visible = $false } catch {}
  $ver = "Hancom Hangul $($h.Version)"
  try {
    foreach ($line in Get-Content -Encoding UTF8 $ListPath) {
      if (-not $line.Trim()) { continue }; $src, $dst = $line.Split("`t"); $t0 = Get-Date
      try {
        $ok = $h.Open($src, "", "forceopen:true;versionwarning:false")   # format auto-detect: "PDF" is rejected by Open
        if (-not $ok) { throw "Open PDF returned false" }
        $ok = $h.SaveAs($dst, "ODF", "")      # Hangul names its OpenDocument Text writer "ODF" ("ODT" returns false)
        if (-not $ok) { throw "SaveAs ODF returned false" }
        $null = $h.Clear(1)
        $res.Add((@{src=$src; out=$dst; status='ok'; s=[math]::Round(((Get-Date)-$t0).TotalSeconds,2)} | ConvertTo-Json -Compress))
      } catch { $res.Add((@{src=$src; status='failed'; reason="$($_.Exception.Message)"} | ConvertTo-Json -Compress)); try { $null = $h.Clear(1) } catch {} }
    }
  } finally { try { $h.Quit() } catch {} }
}
$res.Insert(0, (@{status='done'; app=$ver} | ConvertTo-Json -Compress))
$res | Set-Content -Encoding UTF8 $OutPath
