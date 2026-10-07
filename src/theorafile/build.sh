#!/bin/bash
# Build FNA's Theorafile (Ogg Theora video decoder used by the game's MonoGame
# build for cutscenes) as libtheorafile.so.
#   src/theorafile/build.sh OUTDIR [ARCH...]     (ARCH: aarch64 x86_64; default aarch64)
#
# The game's C# binding allocates OggTheora_File itself with a fixed size of
# 1472 bytes on 64-bit Unix (see Theorafile-CS AllocTheoraFile), so the build
# fails if the struct ever grows beyond that.
set -euo pipefail
OUT="${1:?usage: build.sh OUTDIR [ARCH...]}"
shift
ARCHS=("${@:-aarch64}")

THEORAFILE_REPO=https://github.com/FNA-XNA/Theorafile.git
THEORAFILE_COMMIT=70fef0f8f38727da61039a23c7003f481f583e1c
SRC="${THEORAFILE_SRC:-$(dirname "$OUT")/theorafile-src}"

if [ ! -f "$SRC/theorafile.c" ]; then
  rm -rf "$SRC"
  git clone -q "$THEORAFILE_REPO" "$SRC"
fi
git -C "$SRC" checkout -q "$THEORAFILE_COMMIT"

if [ -n "${CC:-}" ]; then
  cc_for() { echo "$CC"; }
elif command -v zig > /dev/null; then
  cc_for() { echo "zig cc -target $1-linux-gnu.2.17"; }
else
  cc_for() { echo "python3 -m ziglang cc -target $1-linux-gnu.2.17"; }
fi

GENERIC=(theorafile.c lib/ogg/bitwise.c lib/ogg/framing.c
  lib/vorbis/analysis.c lib/vorbis/bitrate.c lib/vorbis/block.c lib/vorbis/codebook.c
  lib/vorbis/envelope.c lib/vorbis/floor0.c lib/vorbis/floor1.c lib/vorbis/vinfo.c
  lib/vorbis/lookup.c lib/vorbis/lpc.c lib/vorbis/lsp.c lib/vorbis/mapping0.c
  lib/vorbis/mdct.c lib/vorbis/psy.c lib/vorbis/registry.c lib/vorbis/res0.c
  lib/vorbis/sharedbook.c lib/vorbis/smallft.c lib/vorbis/synthesis.c lib/vorbis/window.c
  lib/theora/apiwrapper.c lib/theora/bitpack.c lib/theora/decapiwrapper.c
  lib/theora/decinfo.c lib/theora/decode.c lib/theora/dequant.c lib/theora/fragment.c
  lib/theora/huffdec.c lib/theora/idct.c lib/theora/tinfo.c lib/theora/internal.c
  lib/theora/quant.c lib/theora/state.c)

CHECK="$(mktemp -d)"
trap 'rm -rf "$CHECK"' EXIT
cat > "$CHECK/sizecheck.c" << 'EOF'
#include "theorafile.h"
_Static_assert(sizeof(OggTheora_File) <= 1472, "OggTheora_File no longer fits the C# allocation");
EOF

for arch in "${ARCHS[@]}"; do
  cc="$(cc_for "$arch")"
  case "$arch" in
    aarch64)
      defs=(-DOC_ARM_ASM -DOC_ARM_ASM_EDSP -DOC_ARM_ASM_MEDIA -DOC_ARM_ASM_NEON)
      extra=(lib/theora/arm-intrinsics/armcpu.c lib/theora/arm-intrinsics/armfrag.c
             lib/theora/arm-intrinsics/armidct.c lib/theora/arm-intrinsics/armloop.c
             lib/theora/arm-intrinsics/armstate.c) ;;
    x86_64)
      defs=(-DOC_X86_ASM -DOC_X86_64_ASM)
      extra=(lib/theora/x86/mmxfrag.c lib/theora/x86/mmxidct.c lib/theora/x86/mmxstate.c
             lib/theora/x86/sse2idct.c lib/theora/x86/x86cpu.c lib/theora/x86/x86state.c) ;;
    *) echo "unsupported arch $arch"; exit 1 ;;
  esac
  inc=(-I"$SRC" -I"$SRC/lib" -I"$SRC/lib/ogg" -I"$SRC/lib/vorbis" -I"$SRC/lib/theora")
  $cc -c "${inc[@]}" -o "$CHECK/sizecheck.o" "$CHECK/sizecheck.c"
  mkdir -p "$OUT/$arch"
  (cd "$SRC" && $cc -O3 -fPIC -shared -s -w "${defs[@]}" "${inc[@]}" \
      -Wl,-soname,libtheorafile.so -o "$OUT/$arch/libtheorafile.so" \
      "${GENERIC[@]}" "${extra[@]}" -lm)
  echo "built $OUT/$arch/libtheorafile.so"
done
