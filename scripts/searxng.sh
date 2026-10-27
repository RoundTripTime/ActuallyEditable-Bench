#!/usr/bin/env bash
# Local SearXNG (strategy S1.1c) on Docker Desktop, reachable from WSL at http://127.0.0.1:${PORT}/ only.
# Usage: scripts/searxng.sh up | down | status | logs
# Docker: the Linux socket of Docker Desktop's WSL integration may be missing after a WSL update; the Windows
# docker.exe talks to the same engine. Ports bound to 127.0.0.1 in the WSL2 VM are reachable from this distro.
set -u
cd "$(dirname "$0")/.."
PORT=${SEARXNG_PORT:-8888}
NAME=formbench-searxng
IMAGE=${SEARXNG_IMAGE:-searxng/searxng:latest}
if docker info >/dev/null 2>&1; then D=docker; else D="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"; fi
CONF=tools/searxng/settings.yml
case "${1:-status}" in
  up)
    if [ ! -f "$CONF" ]; then
      sed "s/secret_key: \".*\"/secret_key: \"$(python3 -c 'import secrets;print(secrets.token_hex(32))')\"/" tools/searxng/settings.example.yml > "$CONF"
    fi
    "$D" pull -q "$IMAGE" >/dev/null
    "$D" rm -f "$NAME" >/dev/null 2>&1
    "$D" create --name "$NAME" --restart unless-stopped -p "127.0.0.1:${PORT}:8080" \
      -e SEARXNG_BASE_URL="http://127.0.0.1:${PORT}/" "$IMAGE" >/dev/null
    "$D" cp "$(wslpath -w "$CONF" 2>/dev/null || echo "$CONF")" "$NAME:/etc/searxng/settings.yml"
    "$D" start "$NAME" >/dev/null
    for _ in $(seq 1 60); do
      curl -s -m 3 "http://127.0.0.1:${PORT}/healthz" >/dev/null && break; sleep 2
    done
    "$D" image inspect --format '{{index .RepoDigests 0}}' "$IMAGE" > tools/searxng/image.digest
    echo "searxng up: http://127.0.0.1:${PORT}/ ($(cat tools/searxng/image.digest))"
    ;;
  down) "$D" rm -f "$NAME" ;;
  logs) "$D" logs --tail 100 "$NAME" ;;
  status) "$D" ps --filter "name=$NAME" --format '{{.Names}} {{.Status}} {{.Ports}}'; curl -s -m 5 -o /dev/null -w "healthz %{http_code}\n" "http://127.0.0.1:${PORT}/healthz" ;;
esac
