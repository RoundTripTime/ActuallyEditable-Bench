#!/usr/bin/env bash
# Experiment current (docs/experiment.md): after every reconstruction exists in data/experiment_documents, score metric ① (visual
# similarity to the LibreOffice rendering of the preprocessed template), compute every cost run (KLM primary + the edit
# sensitivity set + action counts), the edit-task shares and the reports. Every heavy step is memory-capped via bg.sh.
# Usage: scripts/evaluate.sh [PID ...]
set -u
cd "$(dirname "$0")/.."
export FORMBENCH_OBS=experiment_documents
LOG=data/logs/current_report.log
waitpid() { while kill -0 "$1" 2>/dev/null; do sleep 30; done; }
bgwait() {  # name GB cmd... : start through bg.sh (retry while the memory budget is full) and wait for it
  local name=$1; shift
  local out
  until out=$(scripts/bg.sh "$name" "$@"); do sleep 120; done
  echo "$out" >> "$LOG"
  waitpid "$(echo "$out" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
}
for p in "$@"; do waitpid "$p"; done
bgwait current_visual 6 .venv/bin/python scripts/evaluate_visual.py
FORMBENCH_COST_UNITS=klm bgwait current_klm 4 .venv/bin/python scripts/evaluate_cost.py main --workers 3
bgwait current_act13 4 .venv/bin/python scripts/evaluate_cost.py actions --workers 3
FORMBENCH_COST_UNITS=klm FORMBENCH_COST_TOL=6 bgwait current_klm_tol6 4 .venv/bin/python scripts/evaluate_cost.py tolerance-6 --workers 3
FORMBENCH_COST_UNITS=klm FORMBENCH_COST_TOL=24 bgwait current_klm_tol24 4 .venv/bin/python scripts/evaluate_cost.py tolerance-24 --workers 3
FORMBENCH_COST_UNITS=klm FORMBENCH_COST_TYPING_SCALE=0.5 bgwait current_klm_type05 4 .venv/bin/python scripts/evaluate_cost.py typing-half --workers 3
FORMBENCH_COST_UNITS=klm FORMBENCH_COST_TYPING_SCALE=2 bgwait current_klm_type2 4 .venv/bin/python scripts/evaluate_cost.py typing-double --workers 3
FORMBENCH_COST_UNITS=klm FORMBENCH_COST_KPC_FLAT=1 bgwait current_klm_kpc1 4 .venv/bin/python scripts/evaluate_cost.py one-key-per-character --workers 3
FORMBENCH_COST_UNITS=klm bgwait current_edit_tasks 6 .venv/bin/python scripts/edit_breakdown.py --workers 3 --source-run main --out-run edit-tasks
for r in main actions tolerance-6 tolerance-24 typing-half typing-double one-key-per-character; do
  .venv/bin/python -m formbench.experiment.report "$r" >> "$LOG" 2>&1
done
.venv/bin/python -c "from formbench.experiment.report import sensitivity; print(sensitivity('main', ['tolerance-6', 'tolerance-24', 'typing-half', 'typing-double', 'one-key-per-character']))" >> "$LOG" 2>&1
echo "# current fill done $(date -Is)" >> "$LOG"
