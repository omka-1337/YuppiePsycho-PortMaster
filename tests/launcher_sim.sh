#!/bin/bash
# Run port/YuppiePsycho.sh on an x86_64 PC inside a fake PortMaster install.
#
#   tests/launcher_sim.sh GAME_BUILD_DIR NATIVES_X86_64_DIR [workdir]
#
# GAME_BUILD_DIR is a published tests/testgame (see run_tests.sh, e.g. out/sc-net8),
# NATIVES_X86_64_DIR holds x86_64 builds of the Steam stub and Theorafile
# (natives/x86_64 of a run_tests.sh work dir). Set YP_TEST_VIDEO to an .ogv file to
# have the stand-in game decode it. The host's dotnet
# stands in for the device runtime via yuppiepsycho/dotnet, and DEVICE_ARCH is
# x86_64 so the launcher picks libs.x86_64. Everything else - first-run setup,
# launch.env, library paths, Steam stub, save dirs - is the real launcher.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
GAME="$(cd "$1" && pwd)"
NATIVES="$(cd "$2" && pwd)"
WORK="${3:-$(mktemp -d)}"
DOTNET_ROOT="${DOTNET_ROOT:-$(dirname "$(readlink -f "$(command -v dotnet)")")}"

rm -rf "$WORK/root"
mkdir -p "$WORK/root/ports" "$WORK/pm"
cp -a "$REPO/port/YuppiePsycho.sh" "$WORK/root/ports/"
cp -a "$REPO/port/yuppiepsycho" "$WORK/root/ports/"
G="$WORK/root/ports/yuppiepsycho"
mkdir -p "$G/libs.x86_64" "$G/gamedata"
cp "$NATIVES"/*.so "$G/libs.x86_64/"
ln -s "$DOTNET_ROOT" "$G/dotnet"
cp -a "$GAME/." "$G/gamedata/"
printf 'YP_ARGS="--smoke"\nYP_STEAM_STUB=fake\nexport YP_TEST_VIDEO="%s"\n' "${YP_TEST_VIDEO:-}" > "$G/settings.txt"

cat > "$WORK/pm/control.txt" << EOF
directory="${WORK#/}/root"
CFW_NAME=SIM
DEVICE_ARCH=x86_64
ESUDO=""
GPTOKEYB=true
sdl_controllerconfig=""
get_controls() { :; }
pm_message() { echo "pm_message: \$*"; }
pm_finish() { echo "pm_finish"; }
EOF
echo 'export LIBGL_ES=""' > "$WORK/pm/libgl_default.txt"

# The launcher looks for control.txt in a few fixed places; point it at ours.
sed "s|^  controlfolder=\"/roms/ports/PortMaster\"|  controlfolder=\"$WORK/pm\"|" \
  "$WORK/root/ports/YuppiePsycho.sh" > "$WORK/root/ports/YuppiePsycho.sim.sh"
export XDG_DATA_HOME="$WORK/nonexistent"

for run in first second; do
  echo "== $run launch"
  # The real launcher redirects its own output into log.txt via tee.
  bash "$WORK/root/ports/YuppiePsycho.sim.sh" < /dev/null > "$WORK/$run.out" 2>&1 || true
  sleep 1
  cat "$G/log.txt"
done
