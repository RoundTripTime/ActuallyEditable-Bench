#!/usr/bin/env bash
# Launch a background job inside a memory-capped systemd user scope (WSL VM froze twice from unscoped jobs:
# 2026-10-14 and 2026-10-18). Usage: scripts/bg.sh <name> <MemoryMax GB> <command...>
# Refuses to start when the caps of running bg.sh jobs plus this one exceed BUDGET_GB (default 24 of 31 GB), so the
# sum of all caps can never exhaust the VM. Log: data/logs/<name>.log; registry: data/logs/bg_jobs.tsv.
set -u
cd "$(dirname "$0")/.."
name="$1"; mem="$2"; shift 2
BUDGET_GB="${BUDGET_GB:-24}"
reg=data/logs/bg_jobs.tsv
mkdir -p data/logs; touch "$reg"
used=0
tmp=$(mktemp)
while IFS=$'\t' read -r pid gb nm cmd; do
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then used=$((used + gb)); printf '%s\t%s\t%s\t%s\n' "$pid" "$gb" "$nm" "$cmd" >> "$tmp"; fi
done < "$reg"
mv "$tmp" "$reg"
if [ $((used + mem)) -gt "$BUDGET_GB" ]; then
  echo "bg.sh: refused '$name' (${mem}G): running caps ${used}G + ${mem}G > budget ${BUDGET_GB}G" >&2; exit 3
fi
# BG_OOM=continue: a child killed for memory does not stop the whole scope (a per-file tool process may die alone)
nohup systemd-run --user --scope -q -p MemoryMax="${mem}G" -p MemorySwapMax=0 -p OOMPolicy="${BG_OOM:-stop}" "$@" >> "data/logs/$name.log" 2>&1 &
pid=$!
printf '%s\t%s\t%s\t%s\n' "$pid" "$mem" "$name" "$*" >> "$reg"
echo "bg.sh: started '$name' pid=$pid cap=${mem}G (caps in use $((used + mem))/${BUDGET_GB}G)"
