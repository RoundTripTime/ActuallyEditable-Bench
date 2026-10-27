#!/usr/bin/env bash
# Strategy S1.2: Common Crawl URL index scan of editable documents, by domain pattern in priority order (resumable).
set -u
cd "$(dirname "$0")/.."
. .venv/bin/activate
exec 9> data/logs/cc_scan.lock
flock -n 9 || { echo "cc scan already running" >&2; exit 3; }
for p in '*.gov.tw' '*.edu.tw' '*.org.tw' '*.go.kr' '*.ac.kr' '*.or.kr' '*.re.kr' '*.lg.jp' '*.go.jp' '*.ac.jp' \
         '*.gov.hk' '*.gov.sg' '*.gov.ph' '*.gov.in' '*.go.th' '*.go.id' '*.edu.cn' '*.gov.cn' \
         '*.gov.uk' '*.nhs.uk' '*.ac.uk' '*.gov.au' '*.edu.au' '*.gc.ca' '*.gouv.fr' '*.gob.mx' '*.gov.br' '*.gov' '*.edu'; do
  until python3 -c "import sys; from formbench.util import network_up; sys.exit(0 if network_up() else 1)"; do sleep 60; done
  echo "$(date -Is) start $p" >> data/logs/cc_scan.log
  formbench collect cc-scan --pattern "$p" >> data/logs/cc_scan.log 2>&1
done
echo "$(date -Is) all done" >> data/logs/cc_scan.log
