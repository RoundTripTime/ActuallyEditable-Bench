# Export DOCX files to PDF with Microsoft Word (hidden, read-only, no key events). Input: a text file listing
# "<docx path>`t<pdf path>" per line (Windows paths). Output: JSON lines to the result file.
# Aborts if Word is already running (never touches the user's open documents).
param([string]$ListPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
if (Get-Process WINWORD -ErrorAction SilentlyContinue) {
  '{"status":"aborted","reason":"Word already running"}' | Set-Content -Encoding UTF8 $OutPath; exit 2 }
$w = New-Object -ComObject Word.Application
$w.Visible = $false
$w.DisplayAlerts = 0
$w.Options.UpdateLinksAtOpen = $false
$res = New-Object System.Collections.Generic.List[string]
$ver = "$($w.Version) build $($w.Build)"
try {
  foreach ($line in Get-Content -Encoding UTF8 $ListPath) {
    if (-not $line.Trim()) { continue }
    $src, $dst = $line.Split("`t")
    $t0 = Get-Date
    try {
      # ReadOnly, no conversion dialog, no MRU entry, no macros (AutomationSecurity = force disable)
      $w.AutomationSecurity = 3
      $d = $w.Documents.Open($src, $false, $true, $false)
      $d.ExportAsFixedFormat($dst, 17)        # wdExportFormatPDF
      $pages = $d.ComputeStatistics(2)        # wdStatisticPages
      $d.Close([ref]0)
      $res.Add((@{src=$src; pdf=$dst; status='ok'; pages=$pages; s=[math]::Round(((Get-Date)-$t0).TotalSeconds,2)} | ConvertTo-Json -Compress))
    } catch {
      $res.Add((@{src=$src; status='failed'; reason="$($_.Exception.Message)"} | ConvertTo-Json -Compress))
      try { foreach ($x in @($w.Documents)) { $x.Close([ref]0) } } catch {}
    }
  }
} finally {
  try { $w.Quit([ref]0) } catch { try { $w.Quit() } catch {} }
  $res.Insert(0, (@{status='done'; renderer="Microsoft Word $ver"} | ConvertTo-Json -Compress))
  $res | Set-Content -Encoding UTF8 $OutPath
}
