#!/usr/bin/env bash
set -euo pipefail

# 본문은 하나로 유지하고 저자 정보 유무에 따라 두 PDF를 만든다.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v tectonic >/dev/null 2>&1; then
  echo "error: tectonic is not installed or not on PATH" >&2
  exit 1
fi
cd "$SCRIPT_DIR"
mkdir -p build/review submission
tectonic -X compile --keep-logs --outdir build main.tex
tectonic -X compile --keep-logs --outdir build/review review.tex
cp build/main.pdf main.pdf
cp build/main.pdf submission/final.pdf
cp build/review/review.pdf submission/review.pdf
echo "Wrote $SCRIPT_DIR/main.pdf"
echo "Wrote $SCRIPT_DIR/submission/final.pdf"
echo "Wrote $SCRIPT_DIR/submission/review.pdf"
