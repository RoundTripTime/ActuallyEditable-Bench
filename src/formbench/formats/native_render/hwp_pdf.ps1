# Export HWPX files to PDF with Hancom Hangul (COM automation, hidden window, no key events).
# The file-path security module registered under HKCU\Software\HNC\HwpAutomation\Modules (FilePathCheckerModule)
# suppresses the per-file access dialog. Aborts if Hangul is already running (never touches the user's documents).
param([string]$ListPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
if (Get-Process Hwp -ErrorAction SilentlyContinue) {
  '{"status":"aborted","reason":"Hangul already running"}' | Set-Content -Encoding UTF8 $OutPath; exit 2 }
$h = New-Object -ComObject HWPFrame.HwpObject
$null = $h.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
try { $h.XHwpWindows.Item(0).Visible = $false } catch {}
$res = New-Object System.Collections.Generic.List[string]
$ver = "$($h.Version)"
try {
  foreach ($line in Get-Content -Encoding UTF8 $ListPath) {
    if (-not $line.Trim()) { continue }
    $src, $dst = $line.Split("`t")
    $t0 = Get-Date
    try {
      $ok = $h.Open($src, "HWPX", "forceopen:true;versionwarning:false;suspendpassword:true")
      if (-not $ok) { throw "Open returned false" }
      $pages = $h.PageCount
      $ok = $h.SaveAs($dst, "PDF", "")
      if (-not $ok) { throw "SaveAs PDF returned false" }
      $null = $h.Clear(1)                      # discard (never save the source)
      $res.Add((@{src=$src; pdf=$dst; status='ok'; pages=$pages; s=[math]::Round(((Get-Date)-$t0).TotalSeconds,2)} | ConvertTo-Json -Compress))
    } catch {
      $res.Add((@{src=$src; status='failed'; reason="$($_.Exception.Message)"} | ConvertTo-Json -Compress))
      try { $null = $h.Clear(1) } catch {}
    }
  }
} finally {
  try { $h.Quit() } catch {}
  $res.Insert(0, (@{status='done'; renderer="Hancom Hangul $ver"} | ConvertTo-Json -Compress))
  $res | Set-Content -Encoding UTF8 $OutPath
}
