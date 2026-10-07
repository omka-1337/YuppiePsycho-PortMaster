#!/bin/bash
# Assemble the installable PortMaster package: dist/yuppiepsycho.zip
#
#   ./build.sh
#
# Needs: python3, zip, curl, and zig (`pip install ziglang`) or an aarch64
# cross compiler in $CC for the Steam stub. Third-party aarch64 binaries
# (gl4es, OpenAL Soft, FMOD) are taken from PortMaster-New at a pinned commit
# and checked against SHA-256 sums.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
STAGE="$REPO/build/stage"
CACHE="$REPO/build/cache"
DIST="$REPO/dist"
PORT="$STAGE/yuppiepsycho"

PM_COMMIT=3ee4b60a0e970476a9bba1e30ba61215e2ab53ed
PM_RAW="https://raw.githubusercontent.com/PortsMaster/PortMaster-New/$PM_COMMIT/ports"

# path-in-PortMaster-New  sha256  destination-in-port  [more destinations...]
DEPS=(
  "stardewvalleymainline/stardewvalleymainline/gl4es.aarch64/libGL.so.1 2d4dd48c8255c6fa6fc6db24c67f8600a869da5094a5952cc645abe5986af87f gl4es.aarch64/libGL.so.1"
  "stardewvalleymainline/stardewvalleymainline/gl4es.aarch64/libEGL.so.1 287d8fd88704c2429218467426b46a2ddbb7f8ac735aa5fb5ecb9fdbe1026876 gl4es.aarch64/libEGL.so.1"
  "stardewvalleymainline/stardewvalleymainline/licenses/LICENSE.GL4ES.txt 69a3049c77abd88658f6ad7cf6a5b6ceb5cf03b80e60f7c4657d90322cfab7bd licenses/LICENSE.gl4es.txt"
  # OpenAL Soft 1.23.1 (SDL2 backend)
  "belleboomerang/belleboomerang/libs/libopenal.so.1 8f7842c6ca2316b11a933e258813725bfa75c4fb676eccdc2439be27f37a1c2b libs.aarch64/libopenal.so.1"
  # FMOD Engine 2.02.18 (SONAME libfmod.so.13 / libfmodstudio.so.13)
  "celeste64/celeste64/libfmod.so 440556e5948d78dc60d08267afd8031065fe435a572bdb85954dafec2bd05ba3 libs.aarch64/libfmod.so libs.aarch64/libfmod.so.13"
  "celeste64/celeste64/libfmodstudio.so 2d8e5608c1d6fff3700c0a6659d2553fcd399d3676c8bed3ea22e60c963d2636 libs.aarch64/libfmodstudio.so libs.aarch64/libfmodstudio.so.13"
)

echo "== staging"
rm -rf "$STAGE"
mkdir -p "$STAGE" "$CACHE" "$DIST"
cp -a "$REPO/port/." "$STAGE/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$PORT/gamedata" "$PORT/savedata" "$PORT/libs.aarch64" "$PORT/gl4es.aarch64" "$PORT/licenses"
# PortMaster reads the metadata from inside the port folder once installed.
cp "$STAGE/port.json" "$STAGE/README.md" "$STAGE/gameinfo.xml" "$PORT/"
touch "$PORT/gamedata/.gitkeep" "$PORT/savedata/.gitkeep"

echo "== third-party aarch64 binaries"
for dep in "${DEPS[@]}"; do
  read -r src sum dests <<< "$dep"
  cached="$CACHE/$sum"
  if [ ! -f "$cached" ]; then
    curl -fsSL --retry 4 -o "$cached.tmp" "$PM_RAW/$src"
    mv "$cached.tmp" "$cached"
  fi
  echo "$sum  $cached" | sha256sum -c --quiet - || { echo "checksum mismatch for $src"; rm -f "$cached"; exit 1; }
  for d in $dests; do
    install -D -m 0755 "$cached" "$PORT/$d"
  done
  echo "  $src"
done
chmod 0644 "$PORT/licenses/"*.txt

echo "== Steam stub"
"$REPO/src/steamstub/build.sh" "$REPO/build/steamstub" aarch64
cp "$REPO/build/steamstub/aarch64/"*.so "$PORT/libs.aarch64/"

echo "== licenses"
cp "$REPO/LICENSE" "$PORT/licenses/LICENSE.port.txt"
cat > "$PORT/licenses/NOTICE.txt" << 'EOF'
Third-party components shipped with this port
=============================================

gl4es (libGL.so.1, libEGL.so.1) - MIT, see LICENSE.gl4es.txt
  https://github.com/ptitSeb/gl4es

OpenAL Soft 1.23.1 (libopenal.so.1) - LGPL-2.0-or-later
  https://github.com/kcat/openal-soft  (source: https://openal-soft.org/openal-releases/)

FMOD Engine 2.02.18 (libfmod.so*, libfmodstudio.so*)
  Made with FMOD Studio by Firelight Technologies Pty Ltd.
  https://www.fmod.com - redistributed under the FMOD EULA as part of the game port.

Steam API stub (libsteam_api*.so, libsdkencryptedappticket*.so) and tools/ypport.py
  Written for this port, see LICENSE.port.txt. Not affiliated with Valve.

The game itself is not included. Yuppie Psycho (c) Baroque Decay.
EOF

echo "== zip"
chmod 0755 "$STAGE/YuppiePsycho.sh" "$PORT/tools/ypport.py" "$PORT/tools/patchscript"
rm -f "$DIST/yuppiepsycho.zip"
(cd "$STAGE" && zip -q -r -X "$DIST/yuppiepsycho.zip" YuppiePsycho.sh yuppiepsycho)
(cd "$DIST" && sha256sum yuppiepsycho.zip > yuppiepsycho.zip.sha256)
echo "built $DIST/yuppiepsycho.zip ($(du -h "$DIST/yuppiepsycho.zip" | cut -f1))"
