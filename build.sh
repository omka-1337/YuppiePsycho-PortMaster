#!/bin/bash
# Assemble the installable PortMaster package: dist/yuppiepsycho.zip
#
#   ./build.sh               port with its own aarch64 .NET 8 runtime (~35 MB zip)
#   ./build.sh --no-dotnet   smaller port that uses PortMaster's dotnet-8.0.12 runtime
#
# Needs: python3, zip, curl, git, and zig (`pip install ziglang`) or an
# aarch64 cross compiler in $CC for the Steam stub, Theorafile and ogvshrink.
# Third-party aarch64 binaries (gl4es, OpenAL Soft, FMOD) are taken from
# PortMaster-New at a pinned commit, the .NET runtime from Microsoft's NuGet
# packages; everything downloaded is checked against SHA-256 sums.
set -euo pipefail

BUNDLE_DOTNET=1
for arg in "$@"; do
  case "$arg" in
    --no-dotnet) BUNDLE_DOTNET=0 ;;
    *) echo "usage: $0 [--no-dotnet]"; exit 1 ;;
  esac
done

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
  # (MonoGame 3.8.2+ loads libopenal.so, older versions libopenal.so.1)
  "belleboomerang/belleboomerang/libs/libopenal.so.1 8f7842c6ca2316b11a933e258813725bfa75c4fb676eccdc2439be27f37a1c2b libs.aarch64/libopenal.so.1 libs.aarch64/libopenal.so"
  # FMOD Engine 2.02.18 core (SONAME libfmod.so.13); the game does not use FMOD Studio
  "celeste64/celeste64/libfmod.so 440556e5948d78dc60d08267afd8031065fe435a572bdb85954dafec2bd05ba3 libs.aarch64/libfmod.so libs.aarch64/libfmod.so.13"
)

# Omka-Patcher, the engine of the PC patcher (`patch`, `patch.bat`), taken from
# its sources: the latest main, or OMKA_PATCHER_REF=<tag or commit> to rebuild an
# older package. OMKA_PATCHER_SRC=/path/to/checkout uses a local copy, for working
# on both at once. The commit used is recorded in the package
# (tools/omkapatcher.commit, NOTICE.txt) and in every patch log.
OMKA_PATCHER_REPO=https://github.com/omka-1337/Omka-Patcher.git

# .NET runtime for linux-arm64 from nuget.org (packages there are immutable)
DOTNET_VERSION=8.0.31
DOTNET_PACKS=(
  "microsoft.netcore.app.runtime.linux-arm64 c88afab3f11952bce6d64dc7d02226875bed7289818040acdfbab80778674302"
  "runtime.linux-arm64.microsoft.netcore.dotnethost ba7d9d6dcbcd2cf5d40df4be6a02d531c1fa7ac4486b8e2965a9d260d4fe173a"
)

fetch() { # url sha256 -> prints cached path
  local url="$1" sum="$2" cached="$CACHE/$2"
  if [ ! -f "$cached" ]; then
    curl -fsSL --retry 4 -o "$cached.tmp" "$url"
    mv "$cached.tmp" "$cached"
  fi
  echo "$sum  $cached" | sha256sum -c --quiet - >&2 || { echo "checksum mismatch for $url" >&2; rm -f "$cached"; exit 1; }
  echo "$cached"
}

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
  cached="$(fetch "$PM_RAW/$src" "$sum")"
  for d in $dests; do
    install -D -m 0755 "$cached" "$PORT/$d"
  done
  echo "  $src"
done
chmod 0644 "$PORT/licenses/"*.txt

if [ "$BUNDLE_DOTNET" = 1 ]; then
  echo "== .NET $DOTNET_VERSION runtime (linux-arm64)"
  packs=()
  for pack in "${DOTNET_PACKS[@]}"; do
    read -r id sum <<< "$pack"
    packs+=("$(fetch "https://api.nuget.org/v3-flatcontainer/$id/$DOTNET_VERSION/$id.$DOTNET_VERSION.nupkg" "$sum")")
  done
  python3 "$REPO/src/dotnet/assemble_runtime.py" "${packs[0]}" "${packs[1]}" "$PORT/dotnet"
  cp "$PORT/dotnet/LICENSE.txt" "$PORT/licenses/LICENSE.dotnet.txt"
  cp "$PORT/dotnet/ThirdPartyNotices.txt" "$PORT/licenses/THIRD-PARTY-NOTICES.dotnet.txt"
  # The port carries its runtime, PortMaster does not need to download one.
  python3 - "$STAGE/port.json" "$PORT/port.json" << 'EOF'
import json, sys
for path in sys.argv[1:]:
    with open(path) as f:
        data = json.load(f)
    data["attr"]["runtime"] = []
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
EOF
fi

echo "== Steam stub"
"$REPO/src/steamstub/build.sh" "$REPO/build/steamstub" aarch64
cp "$REPO/build/steamstub/aarch64/"*.so "$PORT/libs.aarch64/"

echo "== Theorafile"
THEORAFILE_SRC="$REPO/build/theorafile-src" "$REPO/src/theorafile/build.sh" "$REPO/build/theorafile" aarch64
cp "$REPO/build/theorafile/aarch64/libtheorafile.so" "$PORT/libs.aarch64/"
for f in "$REPO/build/theorafile-src/licenses/"*; do
  cp "$f" "$PORT/licenses/theorafile-$(basename "$f").txt"
done

echo "== ogvshrink"
# aarch64 for the handheld (and Linux aarch64 PCs), the rest for the PC patcher.
PC_TARGETS=(linux-x86_64 windows-x86_64 macos-x86_64 macos-aarch64)
THEORAFILE_SRC="$REPO/build/theorafile-src" LIBTHEORA_SRC="$REPO/build/libtheora-src" \
  "$REPO/src/ogvshrink/build.sh" "$REPO/build/ogvshrink" aarch64 x86_64 windows-x86_64 macos-x86_64 macos-aarch64
mkdir -p "$PORT/tools/bin.aarch64"
cp "$REPO/build/ogvshrink/aarch64/ogvshrink" "$PORT/tools/bin.aarch64/"
for target in "${PC_TARGETS[@]}"; do
  mkdir -p "$PORT/tools/bin.$target"
  cp "$REPO/build/ogvshrink/${target#linux-}/"ogvshrink* "$PORT/tools/bin.$target/"
done
cp "$REPO/build/libtheora-src/COPYING" "$PORT/licenses/libtheora-COPYING.txt"

echo "== Omka-Patcher"
if [ -n "${OMKA_PATCHER_SRC:-}" ]; then
  OP="$(cd "$OMKA_PATCHER_SRC" && pwd)"
  OP_COMMIT="$(git -C "$OP" rev-parse HEAD 2> /dev/null || echo unknown)"
  [ -n "$(git -C "$OP" status --porcelain 2> /dev/null)" ] && OP_COMMIT="$OP_COMMIT-dirty"
  echo "  local copy $OP ($OP_COMMIT)"
else
  ref="${OMKA_PATCHER_REF:-main}"
  OP="$REPO/build/omka-patcher"
  [ -d "$OP/.git" ] || git clone -q "$OMKA_PATCHER_REPO" "$OP"
  git -C "$OP" fetch -q --tags --force origin
  # a branch is taken as on GitHub, a tag or commit as it is
  if git -C "$OP" rev-parse -q --verify "origin/$ref^{commit}" > /dev/null; then
    ref="origin/$ref"
  fi
  git -C "$OP" checkout -q --detach "$ref"
  OP_COMMIT="$(git -C "$OP" rev-parse HEAD)"
  echo "  ${OMKA_PATCHER_REF:-main} ($OP_COMMIT)"
fi
cp "$OP/omkapatcher.py" "$OP/devices.json" "$PORT/tools/"
cp "$OP/patch" "$OP/patch.bat" "$PORT/"
cp "$OP/LICENSE" "$PORT/licenses/LICENSE.omka-patcher.txt"
echo "$OP_COMMIT" > "$PORT/tools/omkapatcher.commit"

echo "== licenses"
cp "$REPO/LICENSE" "$PORT/licenses/LICENSE.port.txt"
cat > "$PORT/licenses/NOTICE.txt" << 'EOF'
Third-party components shipped with this port
=============================================

gl4es (libGL.so.1, libEGL.so.1) - MIT, see LICENSE.gl4es.txt
  https://github.com/ptitSeb/gl4es

OpenAL Soft 1.23.1 (libopenal.so.1) - LGPL-2.0-or-later
  https://github.com/kcat/openal-soft  (source: https://openal-soft.org/openal-releases/)

Theorafile (libtheorafile.so) - zlib license; bundles libogg, libvorbis and
  libtheora (BSD-style licenses). See theorafile-*.txt.
  https://github.com/FNA-XNA/Theorafile

tools/bin.*/ogvshrink - written for this port (LICENSE.port.txt), built
  with libogg and libtheora from Theorafile and the libtheora 1.2.0 encoder
  (BSD-style, see libtheora-COPYING.txt). https://github.com/xiph/theora

FMOD Engine 2.02.18 core (libfmod.so*)
  Made with FMOD Studio by Firelight Technologies Pty Ltd.
  https://www.fmod.com - redistributed under the FMOD EULA as part of the game port.

.NET runtime (dotnet/, when bundled) - MIT, see LICENSE.dotnet.txt and
  THIRD-PARTY-NOTICES.dotnet.txt. https://github.com/dotnet/runtime

Steam API stub (libsteam_api*.so, libsdkencryptedappticket*.so), tools/ypport.py,
tools/recipe.py
  Written for this port, see LICENSE.port.txt. Not affiliated with Valve.

Omka-Patcher (tools/omkapatcher.py, tools/devices.json, patch, patch.bat) - MIT,
  see LICENSE.omka-patcher.txt. https://github.com/omka-1337/Omka-Patcher
  commit OMKA_PATCHER_COMMIT

The game itself is not included. Yuppie Psycho (c) Baroque Decay.
EOF
sed -i "s/OMKA_PATCHER_COMMIT/$OP_COMMIT/" "$PORT/licenses/NOTICE.txt"

echo "== zip"
chmod 0755 "$STAGE/YuppiePsycho.sh" "$PORT/tools/ypport.py" "$PORT/tools/patchscript" "$PORT/tools/memwatch.sh" \
  "$PORT/patch" "$PORT/tools/omkapatcher.py" "$PORT/tools/bin."*/ogvshrink*
rm -f "$DIST/yuppiepsycho.zip"
(cd "$STAGE" && zip -q -r -X "$DIST/yuppiepsycho.zip" YuppiePsycho.sh yuppiepsycho)
(cd "$DIST" && sha256sum yuppiepsycho.zip > yuppiepsycho.zip.sha256)
echo "built $DIST/yuppiepsycho.zip ($(du -h "$DIST/yuppiepsycho.zip" | cut -f1))"
