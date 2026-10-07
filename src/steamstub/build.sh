#!/bin/bash
# Build the Steam stubs.
#   src/steamstub/build.sh OUTDIR [ARCH...]     (ARCH: aarch64 x86_64; default aarch64)
# Uses $CC if set, otherwise `zig cc` (pip install ziglang) which cross-
# compiles without a sysroot. glibc 2.17 is targeted so the result loads on
# every PortMaster firmware.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: build.sh OUTDIR [ARCH...]}"
shift
ARCHS=("${@:-aarch64}")

if [ -n "${CC:-}" ]; then
  cc_for() { echo "$CC"; }
elif command -v zig > /dev/null; then
  cc_for() { echo "zig cc -target $1-linux-gnu.2.17"; }
else
  cc_for() { echo "python3 -m ziglang cc -target $1-linux-gnu.2.17"; }
fi

GEN="$(mktemp -d)"
trap 'rm -rf "$GEN"' EXIT
python3 "$HERE/gen_steam_stub.py" steam_api "$GEN/steam_api.c"
python3 "$HERE/gen_steam_stub.py" encryptedappticket "$GEN/ticket.c"

for arch in "${ARCHS[@]}"; do
  mkdir -p "$OUT/$arch"
  cc="$(cc_for "$arch")"
  flags=(-shared -fPIC -O2 -s -fvisibility=hidden -Wall -Wno-unused-function)
  $cc "${flags[@]}" -Wl,-soname,libsteam_api.so -o "$OUT/$arch/libsteam_api.so" "$GEN/steam_api.c"
  $cc "${flags[@]}" -Wl,-soname,libsdkencryptedappticket.so -o "$OUT/$arch/libsdkencryptedappticket.so" "$GEN/ticket.c"
  # Windows builds of the wrappers ask for steam_api64 / sdkencryptedappticket64.
  cp "$OUT/$arch/libsteam_api.so" "$OUT/$arch/libsteam_api64.so"
  cp "$OUT/$arch/libsdkencryptedappticket.so" "$OUT/$arch/libsdkencryptedappticket64.so"
  echo "built $OUT/$arch"
done
