#!/usr/bin/env bash
# Continuous pair explorer: PAR independent workers (each restarts when its batch ends, no round barrier) plus a
# processing pass (fetch -> parse -> pair -> review sheet -> strategy report) every PROC_EVERY seconds.
# Restart-safe: one pool at a time (flock), half-done work requeued once at start, waits while the network is down.
set -u
cd "$(dirname "$0")/.."
. .venv/bin/activate
PAR=${PAR:-40}
BATCH=${BATCH:-20}
PROC_EVERY=${PROC_EVERY:-900}
IDLE_SLEEP=${IDLE_SLEEP:-300}
mkdir -p data/logs/pool
exec 9> data/logs/explore_loop.lock
flock -n 9 || { echo "another explore loop is running" >&2; exit 3; }
LOG=data/logs/explore_loop.log
echo "pool start $(date -Is) boot=$(cat /proc/sys/kernel/random/boot_id) pid=$$ par=$PAR" >> $LOG
formbench collect explore-pairs --queries 0 --no-seed --reset-stale >> $LOG 2>&1

net_up() { python3 -c "import sys; from formbench.util import network_up; sys.exit(0 if network_up() else 1)"; }
work_left() {
  python3 -c "
import time; from formbench.util import DB
db = DB(); d = db.one(\"SELECT count(*) n FROM explore_queries WHERE status='pending' AND engine='direct'\")['n']
e = db.one(\"SELECT blocked_until b FROM explore_engine WHERE engine='web'\")
q = db.one(\"SELECT count(*) n FROM explore_queries WHERE status='pending' AND engine!='direct'\")['n']
print(1 if d or (q and (e is None or (e['b'] or 0) < time.time())) else 0)"
}
worker() {
  local k=$1
  while true; do
    until net_up; do sleep 60; done
    if [ "$(work_left)" != "1" ]; then sleep "$IDLE_SLEEP"; continue; fi
    formbench collect explore-pairs --queries "$BATCH" --depth 3 --no-seed >> "data/logs/pool/w$k.err" 2>&1
    sleep 1
  done
}
trap 'kill $(jobs -p) 2>/dev/null; echo "pool stop $(date -Is)" >> '"$LOG"'; exit 0' INT TERM
for k in $(seq 1 "$PAR"); do worker "$k" & sleep 0.5; done
i=0
while true; do
  sleep "$PROC_EVERY"
  i=$((i + 1))
  until net_up; do sleep 60; done
  L=data/logs/pool/proc_$(date +%Y%m%dT%H%M%S)
  scripts/process_pairs.sh "$L"
  python3 -c "
import json; d=json.load(open('$L.pair.out')); c=d['pair_counts']['real_pairs']
print('pass $i $(date -Is) pairs:', {k: v.get('pairs') for k,v in c.items()}, c['observed_candidate'].get('by_format'), 'diversity', c['observed_candidate'].get('diversity'))" >> $LOG 2>&1
done
