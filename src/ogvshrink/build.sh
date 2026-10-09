#!/bin/bash
# Build ogvshrink, the cutscene re-encoder the first-run setup uses.
#   src/ogvshrink/build.sh OUTDIR [ARCH...]     (ARCH: aarch64 x86_64; default aarch64)
#
# Decoding uses the libtheora copy inside Theorafile (the same code, NEON
# included, that plays the videos in the game); encoding uses the encoder of
# libtheora 1.2.0, which Theorafile leaves out. The two trees only differ in
# license headers and small cleanups, so the encoder files drop in.
# THEORAFILE_SRC and LIBTHEORA_SRC can point at existing checkouts;
# PROGRAM=file.c builds another program against the same libraries (tests).
set -euo pipefail
OUT="${1:?usage: build.sh OUTDIR [ARCH...]}"
shift
ARCHS=("${@:-aarch64}")
HERE="$(cd "$(dirname "$0")" && pwd)"
PROGRAM="$(cd "$(dirname "${PROGRAM:-$HERE/ogvshrink.c}")" && pwd)/$(basename "${PROGRAM:-ogvshrink.c}")"
NAME="$(basename "$PROGRAM" .c)"

THEORAFILE_REPO=https://github.com/FNA-XNA/Theorafile.git
THEORAFILE_COMMIT=70fef0f8f38727da61039a23c7003f481f583e1c
LIBTHEORA_REPO=https://github.com/xiph/theora.git
LIBTHEORA_COMMIT=8e4808736e9c181b971306cc3f05df9e61354004 # v1.2.0

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
TF="${THEORAFILE_SRC:-$(dirname "$OUT")/theorafile-src}"
LT="${LIBTHEORA_SRC:-$(dirname "$OUT")/libtheora-src}"
fetch() { # repo commit dir probe
  if [ ! -f "$3/$4" ]; then
    rm -rf "$3"
    git clone -q "$1" "$3"
  fi
  git -C "$3" checkout -q "$2"
}
fetch "$THEORAFILE_REPO" "$THEORAFILE_COMMIT" "$TF" theorafile.c
fetch "$LIBTHEORA_REPO" "$LIBTHEORA_COMMIT" "$LT" lib/encode.c

if [ -n "${CC:-}" ]; then
  cc_for() { echo "$CC"; }
elif command -v zig > /dev/null; then
  cc_for() { echo "zig cc -target $1-linux-gnu.2.17"; }
else
  cc_for() { echo "python3 -m ziglang cc -target $1-linux-gnu.2.17"; }
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/lib/theora/arm"
cp -r "$TF/lib/ogg" "$WORK/lib/"
cp -r "$TF/lib/theora/." "$WORK/lib/theora/"
ENC=(analyze.c encfrag.c encinfo.c encode.c enquant.c fdct.c huffenc.c mathops.c mcenc.c
     rate.c tokenize.c)
for f in "${ENC[@]}" encint.h enquant.h huffenc.h mathops.h modedec.h collect.h; do
  cp "$LT/lib/$f" "$WORK/lib/theora/"
done
# encint.h pulls in the 32-bit ARM encoder assembly when OC_ARM_ASM is set;
# on aarch64 only the decoder has NEON code, the encoder runs its C version.
cat > "$WORK/lib/theora/arm/armenc.h" << 'EOF'
#include "../arm-intrinsics/armint.h"
EOF

SRCS=(lib/ogg/bitwise.c lib/ogg/framing.c
  lib/theora/bitpack.c lib/theora/decinfo.c lib/theora/decode.c lib/theora/dequant.c
  lib/theora/fragment.c lib/theora/huffdec.c lib/theora/idct.c lib/theora/tinfo.c
  lib/theora/internal.c lib/theora/quant.c lib/theora/state.c)
for f in "${ENC[@]}"; do SRCS+=("lib/theora/$f"); done

for arch in "${ARCHS[@]}"; do
  cc="$(cc_for "$arch")"
  case "$arch" in
    aarch64)
      defs=(-DOC_ARM_ASM -DOC_ARM_ASM_EDSP -DOC_ARM_ASM_MEDIA -DOC_ARM_ASM_NEON)
      extra=(lib/theora/arm-intrinsics/armcpu.c lib/theora/arm-intrinsics/armfrag.c
             lib/theora/arm-intrinsics/armidct.c lib/theora/arm-intrinsics/armloop.c
             lib/theora/arm-intrinsics/armstate.c) ;;
    x86_64)
      defs=()
      extra=() ;;
    *) echo "unsupported arch $arch"; exit 1 ;;
  esac
  mkdir -p "$OUT/$arch"
  (cd "$WORK" && $cc -O3 -s -w "${defs[@]}" -Ilib -Ilib/theora -o "$OUT/$arch/$NAME" \
      "$PROGRAM" "${SRCS[@]}" "${extra[@]}" -lm)
  echo "built $OUT/$arch/$NAME"
done
