# Empirical probe driver for Microsoft Word (Windows desktop): real key events via SendKeys into a Word window
# created for the probe. Setup that is not under test (insert a table, place the caret) uses the Word object model.
#   powershell -NoProfile -ExecutionPolicy Bypass -File word_sendkeys.ps1 <probes.json> <out.json>
# Safety: aborts if WINWORD is already running (no risk to the user's open documents); before every key event the
# Word window must be the foreground window, otherwise the probe is aborted (keys never go elsewhere); the user's
# AutoCorrect options are switched off for the run and restored afterwards; documents are closed without saving.
param([string]$ProbesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System; using System.Runtime.InteropServices;
public class FbFg { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); }
'@
if (Get-Process WINWORD -ErrorAction SilentlyContinue) { @{status='aborted'; reason='Word already running: close Word and retry'} | ConvertTo-Json | Set-Content -Encoding UTF8 $OutPath; exit 2 }
$probes = Get-Content -Raw -Encoding UTF8 $ProbesPath | ConvertFrom-Json
$w = New-Object -ComObject Word.Application
$w.Visible = $true
$ac = $w.AutoCorrect
$saved = @{}
foreach ($n in 'CorrectSentenceCaps','CorrectInitialCaps','ReplaceText','CorrectCapsLock','CorrectDays','CorrectTableCells') { $saved[$n] = $ac.$n; $ac.$n = $false }
$fmt = $w.Options
$savedFmt = @{}
foreach ($n in 'AutoFormatAsYouTypeReplaceQuotes','AutoFormatAsYouTypeApplyNumberedLists','AutoFormatAsYouTypeApplyBulletedLists','AutoFormatAsYouTypeReplaceHyperlinks','AutoFormatAsYouTypeReplaceOrdinals','AutoFormatAsYouTypeReplaceSymbols','AutoFormatAsYouTypeApplyTables') { try { $savedFmt[$n] = $fmt.$n; $fmt.$n = $false } catch {} }
$keymap = @{ 'TAB'='{TAB}'; 'SHIFT+TAB'='+{TAB}'; 'RETURN'='{ENTER}'; 'BACKSPACE'='{BACKSPACE}'; 'DELETE'='{DEL}'; 'HOME'='{HOME}'; 'END'='{END}';
             'LEFT'='{LEFT}'; 'RIGHT'='{RIGHT}'; 'SHIFT+LEFT'='+{LEFT}'; 'SHIFT+RIGHT'='+{RIGHT}'; 'SHIFT+DOWN'='+{DOWN}'; 'CTRL+BACKSPACE'='^{BACKSPACE}' }
function Esc([string]$t) { return ($t -replace '([+^%~(){}\[\]])', '{$1}') }
$results = @{}
try {
  foreach ($p in $probes) {
    $doc = $w.Documents.Add()
    $doc.Activate(); $w.Activate()
    $hwnd = [IntPtr]$w.ActiveWindow.Hwnd
    Start-Sleep -Milliseconds 600
    $beforeY = $null
    $aborted = $null
    function Send([string]$s) {
      [FbFg]::SetForegroundWindow($hwnd) | Out-Null; Start-Sleep -Milliseconds 120
      if ([FbFg]::GetForegroundWindow() -ne $hwnd) { throw 'Word window is not in the foreground: probe aborted, no keys sent' }
      [System.Windows.Forms.SendKeys]::SendWait($s); Start-Sleep -Milliseconds 120
    }
    try {
      foreach ($st in $p.steps) {
        if ($st.table) {
          $t = $doc.Tables.Add($doc.Range(0,0), [int]$st.table.rows, [int]$st.table.cols)
          if ($st.table.cells) { foreach ($prop in $st.table.cells.PSObject.Properties) { $rc = $prop.Name.Split(','); $t.Cell([int]$rc[0]+1, [int]$rc[1]+1).Range.Text = $prop.Value } }
          $beforeY = $doc.Paragraphs.Last.Range.Information(6)
          $r = $t.Cell(1,1).Range; $r.MoveEnd(1, -1) | Out-Null; $r.Collapse(0); $r.Select()
        } elseif ($st.type) { Send (Esc $st.type) }
        elseif ($st.key) { Send $keymap[$st.key] }
        elseif ($st.repeat) { for ($i=0; $i -lt [int]$st.repeat.n; $i++) { Send $keymap[$st.repeat.key] } }
      }
      $paras = @()
      foreach ($pa in $doc.Paragraphs) { if (-not $pa.Range.Information(12)) { $paras += ($pa.Range.Text -replace "[`r`a]+$", '') } }
      $obs = @{ paragraphs = $paras }
      if ($doc.Tables.Count -gt 0) {
        $t = $doc.Tables.Item(1); $cells = @{}
        foreach ($c in $t.Range.Cells) { $cells["$($c.RowIndex-1),$($c.ColumnIndex-1)"] = ($c.Range.Text -replace "[`r`a]+$", '') }
        $obs.cells = $cells; $obs.rows = $t.Rows.Count
        $sel = $w.Selection
        $obs.selected_cells = $(if ($sel.Information(12) -and $sel.Cells.Count -ge 1 -and $sel.Type -ne 1) { $sel.Cells.Count } else { 0 })
        $obs.selection_type = $sel.Type
        if ($beforeY -ne $null) { $obs.row_grew = ($doc.Paragraphs.Last.Range.Information(6) -gt $beforeY + 1) }
      }
      $results[$p.id] = @{ observed = $obs }
    } catch { $results[$p.id] = @{ error = "$($_.Exception.Message)" } }
    $doc.Close([ref]0)
  }
} finally {
  foreach ($n in $saved.Keys) { $ac.$n = $saved[$n] }
  foreach ($n in $savedFmt.Keys) { try { $fmt.$n = $savedFmt[$n] } catch {} }
  $ver = "$($w.Version) build $($w.Build)"
  try { $w.Quit([ref]0) } catch { $w.Quit() }
}
@{ status='ok'; editor='word'; product="Microsoft Word $ver"; os=[System.Environment]::OSVersion.VersionString; key_events='System.Windows.Forms.SendKeys into the probe window';
   settings=@{ autocorrect_off=@($saved.Keys); autoformat_off=@($savedFmt.Keys); restored=$true }; results=$results } | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $OutPath
