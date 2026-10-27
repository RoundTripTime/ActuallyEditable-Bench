#!/usr/bin/env bash
# Retry of Common Crawl index patterns that failed (index server connection errors), Korean / English domains only
# (user 2026-10-15: collect only ko/en). Waits between patterns so the shared index server is not pressed.
set -u
cd "$(dirname "$0")/.."
. .venv/bin/activate
exec 9> data/logs/cc_scan.lock
flock -n 9 || { echo "cc scan already running" >&2; exit 3; }
for round in 1 2 3; do
  for p in '*.re.kr' '*.gov.uk' '*.nhs.uk' '*.ac.uk' '*.gov.au' '*.edu.au' '*.gc.ca' '*.gov' '*.edu'; do
    if grep -q "\"pattern\": \"$p\", \"pages\"" data/logs/cc_scan.log; then continue; fi   # done in an earlier round
    until python3 -c "import sys; from formbench.util import network_up; sys.exit(0 if network_up() else 1)"; do sleep 60; done
    echo "$(date -Is) retry$round start $p" >> data/logs/cc_scan.log
    formbench collect cc-scan --pattern "$p" >> data/logs/cc_scan.log 2>&1
    sleep 120
  done
  sleep 1800
done
echo "$(date -Is) retry done" >> data/logs/cc_scan.log
