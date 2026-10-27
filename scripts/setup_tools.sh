#!/usr/bin/env bash
# Installs the optional external tools into the user's home (no root needed):
#   * hwplib / hwpxlib (Maven Central, Apache-2.0) + hwp2hwpx (GitHub, Apache-2.0)  -> HWP->HWPX conversion, independent HWPX parser
#   * LibreOffice (deb packages unpacked into ~/opt/lo) + H2Orestart extension (GPL-3.0) -> independent renderer
# Versions used for the recorded experiments are listed in configs/tool_versions.lock.
set -euo pipefail
JARS="${FORMBENCH_JARS:-$HOME/opt/jars}"; mkdir -p "$JARS"; cd "$JARS"
[ -f hwplib-1.1.11.jar ]  || curl -fsSL -o hwplib-1.1.11.jar  https://repo1.maven.org/maven2/kr/dogfoot/hwplib/1.1.11/hwplib-1.1.11.jar
[ -f hwpxlib-1.0.9.jar ] || curl -fsSL -o hwpxlib-1.0.9.jar https://repo1.maven.org/maven2/kr/dogfoot/hwpxlib/1.0.9/hwpxlib-1.0.9.jar
if [ ! -d hwp2hwpx-main ]; then curl -fsSL -o hwp2hwpx.tar.gz https://github.com/neolord0/hwp2hwpx/archive/refs/heads/main.tar.gz && tar xzf hwp2hwpx.tar.gz && rm hwp2hwpx.tar.gz; fi
mkdir -p classes
javac -nowarn -encoding UTF-8 -cp hwplib-1.1.11.jar:hwpxlib-1.0.9.jar -d classes $(find hwp2hwpx-main/src/main -name '*.java' | grep -v Test)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
javac -nowarn -encoding UTF-8 -cp hwplib-1.1.11.jar:hwpxlib-1.0.9.jar:classes -d classes "$HERE/tools/java/FbTool.java"
echo "java tool ready under $JARS"
if [ "${1:-}" = "--with-libreoffice" ]; then
  LO="$HOME/opt/lo"; mkdir -p "$LO"; cd "$LO"
  V="${LIBREOFFICE_VERSION:-26.2.6}"
  curl -fsSL -o lo.tar.gz "https://download.documentfoundation.org/libreoffice/stable/$V/deb/x86_64/LibreOffice_${V}_Linux_x86-64_deb.tar.gz"
  tar xzf lo.tar.gz && for d in LibreOffice_*/DEBS/*.deb; do dpkg-deb -x "$d" root; done && rm -rf lo.tar.gz LibreOffice_*
  mkdir -p "$HOME/opt/h2o" && curl -fsSL -o "$HOME/opt/h2o/H2Orestart.oxt" https://github.com/ebandal/H2Orestart/releases/download/v0.7.14/H2Orestart.oxt
  # --shared: visible to every LibreOffice profile (the converters run with one temporary profile per thread)
  "$(ls -d root/opt/libreoffice*/program | head -1)/unopkg" add --shared -f "$HOME/opt/h2o/H2Orestart.oxt"
  echo "LibreOffice + H2Orestart ready under $LO"
fi
