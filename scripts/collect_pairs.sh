#!/usr/bin/env bash
# Observed (blank, completed) pair collection: one resumable round.
# Discovery units (provider x query) run in parallel (at most $PAR at a time; each process keeps its own per-host delay),
# then: fetch -> parse -> classifier refresh -> template families -> pair candidates -> review thumbnails.
set -u
cd "$(dirname "$0")/.."
. .venv/bin/activate
PAR=${PAR:-8}
LOG=data/logs/collect_pairs_$(date +%Y%m%dT%H%M%S)
echo "round start $(date -Is)" > "$LOG.status"
python - > "$LOG.units" <<'PY'
from formbench.util import DB, load_yaml, path
cfg = load_yaml(path("configs", "collection.yaml"))
db = DB()
for p in cfg["providers"]:
    if p.get("type") != "paired_pages" or not p.get("enabled", True):
        continue
    pending = [q for q in p.get("queries", []) if db.q_status("discover", f"{p['label']}::{q}") not in ("done", "blocked")]
    if p.get("serial") and pending:               # one site: its queries run one after another in a single process
        print(f"{p['label']}\t")
    else:
        for q in pending:
            print(f"{p['label']}\t{q}")
PY
echo "pending units: $(wc -l < "$LOG.units")" >> "$LOG.status"
n=0
while IFS=$'\t' read -r label query; do
  safe=$(echo "$label-$query" | tr -c 'A-Za-z0-9_-' '_')
  if [ -n "$query" ]; then qa=(--query "$query"); else qa=(); fi
  formbench collect discover --no-expand --provider "$label" "${qa[@]}" > "$LOG.discover.$safe.out" 2> "$LOG.discover.$safe.err" &
  n=$((n + 1))
  if [ "$n" -ge "$PAR" ]; then wait -n; n=$((n - 1)); fi
done < "$LOG.units"
wait
echo "discover done $(date -Is)" >> "$LOG.status"
scripts/process_pairs.sh "$LOG"
echo "round end $(date -Is)" >> "$LOG.status"
