#!/usr/bin/env bash
# After discovery: fetch -> parse -> classifier refresh -> template families -> pair candidates -> review thumbnails.
set -u
cd "$(dirname "$0")/.."
. .venv/bin/activate
LOG=${1:-data/logs/process_pairs_$(date +%Y%m%dT%H%M%S)}
formbench collect cc-enqueue > "$LOG.ccenq.out" 2>&1; echo "cc-enqueue rc=$?" >> "$LOG.status"
NF=${FETCH_SHARDS:-8}                                   # parallel fetch: one process per institution shard
for k in $(seq 0 $((NF - 1))); do
  FORMBENCH_FETCH_SHARD="$k/$NF" formbench collect fetch --resume > "$LOG.fetch$k.out" 2> "$LOG.fetch$k.err" &
done
wait; echo "fetch rc=done shards=$NF" >> "$LOG.status"
formbench corpus validate > "$LOG.validate.out" 2> "$LOG.validate.err"; echo "validate rc=$?" >> "$LOG.status"
python -c "from formbench.corpus.classify import refresh_language_domain; from formbench.util import DB; print(refresh_language_domain(DB()))" >> "$LOG.status" 2>&1
formbench corpus dedupe > "$LOG.dedupe.out" 2>&1; echo "dedupe rc=$?" >> "$LOG.status"
formbench corpus pair > "$LOG.pair.out" 2> "$LOG.pair.err"; echo "pair rc=$?" >> "$LOG.status"
python scripts/prerender_previews.py > "$LOG.prerender.out" 2>&1; echo "prerender rc=$?" >> "$LOG.status"
formbench corpus review-sheet > "$LOG.sheet.out" 2>&1; echo "sheet rc=$?" >> "$LOG.status"
formbench collect strategy-report > /dev/null 2>&1; echo "strategy-report rc=$?" >> "$LOG.status"
echo "processed $(date -Is)" >> "$LOG.status"
