#!/usr/bin/env bash
# Experiment current orchestration (docs/experiment.md): wait for the Word/Hangul preprocessing, choose the route per task,
# render the preprocessed templates, build the inputs, run every reconstruction (Windows apps, Docling, GPT-6-Luna, page
# image), then scripts/evaluate.sh (metrics, cost runs, reports). Every job memory-capped via bg.sh.
# Usage: scripts/run_experiment.sh <prep_apps PID> <first GPT run PID>
set -u
cd "$(dirname "$0")/.."
export FORMBENCH_OBS=experiment_documents
R=$(pwd)
LOG=data/logs/current_run.log
PY=$R/.venv/bin/python
waitpid() { while kill -0 "$1" 2>/dev/null; do sleep 30; done; }
start() {  # name GB cmd... : start through bg.sh (retry while the memory budget is full), print the PID
  local name=$1; shift
  local out
  until out=$(scripts/bg.sh "$name" "$@"); do sleep 120; done
  echo "$out" >> "$LOG"
  echo "$out" | sed -n 's/.*pid=\([0-9]*\).*/\1/p'
}
waitpid "$1"
echo "# prep apps done $(date -Is)" >> "$LOG"
p=$(start current_prep_score 6 $PY $R/scripts/preprocess.py score --workers 3); waitpid "$p"
for c in render oracle inputs; do p=$(start current_prep_$c 4 $PY $R/scripts/preprocess.py $c); waitpid "$p"; done
echo "# inputs ready $(date -Is)" >> "$LOG"
w=$(start current_recon_apps 3 $PY $R/scripts/reconstruct_apps.py)
dl=$(start current_docling_b 8 $PY $R/scripts/reconstruct_docling.py)
im=$(start current_image_only 4 $PY $R/scripts/reconstruction_helpers.py --part local --baselines image_only --inputs png_150 --workers 2)
waitpid "$2"
g=$(start current_gpt_odt_b 6 $PY $R/scripts/reconstruct_gpt.py run --all --workers 6)
echo "# reconstructions started: apps $w docling $dl image $im gpt $g $(date -Is)" >> "$LOG"
scripts/evaluate.sh "$w" "$dl" "$im" "$g"
