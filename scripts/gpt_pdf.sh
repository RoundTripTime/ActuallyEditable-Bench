#!/usr/bin/env bash
# Experiment current supplement (docs/experiment.md §6): GPT-6-Luna with the digital PDF as well as the page images. Reconstruct all 321 tasks,
# then score visual similarity, edit time (KLM seconds) and action counts, and write reports/experiments/gpt-pdf.
# Every step memory-capped via bg.sh. Usage: scripts/gpt_pdf.sh [PID to wait for]
set -u
cd "$(dirname "$0")/.."
export FORMBENCH_OBS=experiment_documents
LOG=data/logs/current_gpt_pdf.log
PY=.venv/bin/python
waitpid() { while kill -0 "$1" 2>/dev/null; do sleep 30; done; }
bgwait() {  # name GB cmd... : start through bg.sh (retry while the memory budget is full) and wait for it
  local name=$1; shift
  local out
  until out=$(scripts/bg.sh "$name" "$@"); do sleep 120; done
  echo "$out" >> "$LOG"
  waitpid "$(echo "$out" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
}
for p in "$@"; do waitpid "$p"; done
echo "# start $(date -Is)" >> "$LOG"
bgwait current_gpt_pdf_run 6 $PY scripts/gpt_pdf.py run --workers 6
echo "# reconstructions done $(date -Is)" >> "$LOG"
bgwait current_gpt_pdf_visual 6 $PY scripts/gpt_pdf.py visual --workers 3
bgwait current_gpt_pdf_klm 4 $PY scripts/gpt_pdf.py cost --units klm --workers 3
bgwait current_gpt_pdf_act13 4 $PY scripts/gpt_pdf.py cost --units actions --workers 3
$PY scripts/gpt_pdf.py report >> "$LOG" 2>&1
echo "# done $(date -Is)" >> "$LOG"
