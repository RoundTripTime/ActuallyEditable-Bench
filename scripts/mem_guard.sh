#!/usr/bin/env bash
# Last-resort memory guard for the WSL VM (no working OOM killer before the VM freezes). Every 5 s: if MemAvailable
# drops below FLOOR_MB, kill the single largest-RSS process among this project's jobs (python under the project venvs,
# soffice.bin, the tool venvs) — never the shell, Claude, VS Code or systemd — and log it to data/logs/mem_guard.log.
set -u
cd "$(dirname "$0")/.."
FLOOR_MB="${FLOOR_MB:-3072}"
while true; do
  avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
  if [ "$avail" -lt "$FLOOR_MB" ]; then
    victim=$(ps -eo pid=,rss=,args= --sort=-rss | grep -E "ActuallyEditable/\.venv/bin/python|/opt/venvs/[a-z]+/bin/python|soffice\.bin" | grep -v grep | head -1)
    if [ -n "$victim" ]; then
      vpid=$(echo "$victim" | awk '{print $1}')
      kill -9 "$vpid" 2>/dev/null
      echo "$(date -Is) MemAvailable=${avail}MB < ${FLOOR_MB}MB -> killed $(echo "$victim" | cut -c1-200)" >> data/logs/mem_guard.log
    fi
  fi
  sleep 5
done
