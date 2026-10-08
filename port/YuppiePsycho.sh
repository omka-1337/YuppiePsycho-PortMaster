#!/bin/bash
# Yuppie Psycho: Executive Edition - PortMaster launcher
#
# The game (v2.5+) is MonoGame on .NET. Its code is CPU independent; the
# first launch strips the x86_64 packaging from the copied game files
# (tools/ypport.py prepare) and the game then runs on PortMaster's aarch64
# .NET runtime with aarch64 builds of SDL2, OpenAL, FMOD and a Steam stub.

XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

if [ -d "/opt/system/Tools/PortMaster/" ]; then
  controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then
  controlfolder="/opt/tools/PortMaster"
elif [ -d "$XDG_DATA_HOME/PortMaster/" ]; then
  controlfolder="$XDG_DATA_HOME/PortMaster"
else
  controlfolder="/roms/ports/PortMaster"
fi

source $controlfolder/control.txt
[ -f "${controlfolder}/mod_${CFW_NAME}.txt" ] && source "${controlfolder}/mod_${CFW_NAME}.txt"
get_controls

GAMEDIR="/$directory/ports/yuppiepsycho"
DATADIR="$GAMEDIR/gamedata"
cd "$GAMEDIR" || exit 1
> "$GAMEDIR/log.txt" && exec > >(tee "$GAMEDIR/log.txt") 2>&1

export controlfolder ESUDO GAMEDIR DATADIR

port_arch="${DEVICE_ARCH:-aarch64}"

# Optional user overrides (YP_STEAM_STUB=fake, YP_ARGS=..., YP_FMOD_DIR=...).
[ -f "$GAMEDIR/settings.txt" ] && source "$GAMEDIR/settings.txt"

# --- .NET runtime -----------------------------------------------------------
# The port normally carries its own aarch64 runtime in yuppiepsycho/dotnet.
# Packages built with `build.sh --no-dotnet` use PortMaster's runtime instead.
DOTNET_MOUNT=""
if [ -f "$GAMEDIR/dotnet/dotnet" ]; then
  # Unzipping on some systems drops the executable bit.
  [ -x "$GAMEDIR/dotnet/dotnet" ] || $ESUDO chmod +x "$GAMEDIR/dotnet/dotnet" "$GAMEDIR"/dotnet/shared/*/*/createdump 2>/dev/null
  export DOTNET_ROOT="$GAMEDIR/dotnet"
else
  dotnetfile="$controlfolder/libs/dotnet-8.0.12.squashfs"
  if [ ! -f "$dotnetfile" ]; then
    pm_message "No .NET runtime: yuppiepsycho/dotnet is missing and the PortMaster runtime dotnet-8.0.12 is not installed."
    sleep 5
    exit 1
  fi
  DOTNET_MOUNT="$HOME/yp-dotnet"
  $ESUDO mkdir -p "$DOTNET_MOUNT"
  $ESUDO umount "$DOTNET_MOUNT" 2>/dev/null || true
  $ESUDO mount "$dotnetfile" "$DOTNET_MOUNT"
  export DOTNET_ROOT="$DOTNET_MOUNT"
fi
FRAMEWORK_DIR="$(ls -d "$DOTNET_ROOT"/shared/Microsoft.NETCore.App/* 2>/dev/null | sort -V | tail -n 1)"
export FRAMEWORK_DIR

SWAPFILE=""
YP_ZRAM=""
cleanup() {
  # Hand back the swap this run set up.
  [ -n "$SWAPFILE" ] && $ESUDO swapoff "$SWAPFILE" 2>/dev/null
  if [ -n "$YP_ZRAM" ]; then
    $ESUDO swapoff "/dev/zram$YP_ZRAM" 2>/dev/null
    echo "$YP_ZRAM" | $ESUDO tee /sys/class/zram-control/hot_remove > /dev/null 2>&1
  fi
  [ -n "$DOTNET_MOUNT" ] && $ESUDO umount "$DOTNET_MOUNT" 2>/dev/null
}

# --- aarch64 libraries replacing the game's x86_64 ones ----------------------
LIBDIR="$GAMEDIR/libs.$port_arch"
FMOD_LIBDIR="${YP_FMOD_DIR:-}"
if [ -z "$FMOD_LIBDIR" ] && [ -f "$GAMEDIR/libs.fmod/libfmod.so" ]; then
  FMOD_LIBDIR="$GAMEDIR/libs.fmod"   # FMOD libraries supplied by the user
fi
# The setup installs these next to the game in place of the parked x86_64
# files; the first folder that has a library wins.
export YP_NATIVE_DIRS="${FMOD_LIBDIR:+$FMOD_LIBDIR:}$LIBDIR"
export YP_NATIVE_ARCH="$port_arch"

# A user-supplied FMOD that differs from the copy installed in the game folder
# means the setup has to run again.
if [ -n "$FMOD_LIBDIR" ] && [ -f "$DATADIR/.ypport/launch.env" ]; then
  appdir="$DATADIR/$(. "$DATADIR/.ypport/launch.env"; echo "$YP_APPDIR")"
  if [ -f "$appdir/libfmod.so" ] && ! cmp -s "$FMOD_LIBDIR/libfmod.so" "$appdir/libfmod.so"; then
    python3 "$GAMEDIR/tools/ypport.py" restore "$DATADIR"
  fi
fi

# Texture atlases need ~750 MB decoded; on 1 GB devices store those with binary
# alpha (most of them) as 16-bit Bgra5551 - half the memory, alpha unchanged.
mem_total_mb=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)
if [ -z "${YP_TEXTURES:-}" ]; then
  if [ "${mem_total_mb:-0}" -lt 1800 ]; then YP_TEXTURES=auto16; else YP_TEXTURES=full; fi
fi
export YP_TEXTURES

# --- first launch: make the copied game runnable on aarch64 -----------------
python3 "$GAMEDIR/tools/ypport.py" status "$DATADIR" --textures "$YP_TEXTURES" > /dev/null 2>&1
setup_status=$?
if [ $setup_status -ne 0 ]; then
  if ! command -v python3 > /dev/null 2>&1; then
    pm_message "python3 is required for the first-time setup."
    cleanup; sleep 5; exit 1
  fi
  if [ $setup_status -eq 2 ]; then
    # Prepared by an older version of the port or with another texture mode:
    # undo it, then set up again.
    python3 "$GAMEDIR/tools/ypport.py" restore "$DATADIR"
  elif [ -f "$DATADIR/.ypport/state.json" ]; then
    # The game was copied again (e.g. an update): forget the old setup.
    python3 "$GAMEDIR/tools/ypport.py" reset "$DATADIR"
  fi
  if [ -f "$controlfolder/utils/patcher.txt" ]; then
    export PATCHER_FILE="$GAMEDIR/tools/patchscript"
    export PATCHER_GAME="Yuppie Psycho"
    export PATCHER_TIME="2 to 10 minutes"
    source "$controlfolder/utils/patcher.txt"
  else
    $ESUDO chmod 666 /dev/tty0 2>/dev/null
    bash "$GAMEDIR/tools/patchscript" 2>&1 | tee /dev/tty0
  fi
  if ! python3 "$GAMEDIR/tools/ypport.py" status "$DATADIR" --textures "$YP_TEXTURES" > /dev/null 2>&1; then
    pm_message "Setup failed. See yuppiepsycho/patchlog.txt and yuppiepsycho/log.txt."
    cleanup; sleep 5; exit 1
  fi
fi

source "$DATADIR/.ypport/launch.env"

# --- native libraries ---------------------------------------------------------
case "${YP_FMOD_VERSION:-}" in
  ""|2.02.*) ;;
  *) [ -z "$FMOD_LIBDIR" ] && echo "WARNING: the game was built against FMOD $YP_FMOD_VERSION; the bundled" \
       "aarch64 FMOD is 2.02.18. If there is no sound, put matching aarch64 FMOD libraries into" \
       "yuppiepsycho/libs.fmod (see README)." ;;
esac
export LD_LIBRARY_PATH="${FMOD_LIBDIR:+$FMOD_LIBDIR:}$LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"

if [ -f "${controlfolder}/libgl_${CFW_NAME}.txt" ]; then
  source "${controlfolder}/libgl_${CFW_NAME}.txt"
else
  source "${controlfolder}/libgl_default.txt"
fi
if [ "$LIBGL_ES" != "" ]; then
  export SDL_VIDEO_GL_DRIVER="$GAMEDIR/gl4es.$port_arch/libGL.so.1"
  export SDL_VIDEO_EGL_DRIVER="$GAMEDIR/gl4es.$port_arch/libEGL.so.1"
fi

# --- saves ----------------------------------------------------------------------
# The game keeps its saves in the game folder; anything written through the
# XDG directories (MonoGame / .NET ApplicationData) goes next to them.
mkdir -p "$GAMEDIR/savedata"
export XDG_DATA_HOME="$GAMEDIR/savedata"
export XDG_CONFIG_HOME="$GAMEDIR/savedata"

# --- runtime tuning for 1 GB devices ------------------------------------------------
export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
export DOTNET_gcServer=0
export DOTNET_gcConcurrent=0
export DOTNET_TieredPGO=0
export DOTNET_TC_QuickJitForLoops=1
export DOTNET_EnableWriteXorExecute=0
export DOTNET_CLI_TELEMETRY_OPTOUT=1
# Compact the heap (incl. the large object heap) instead of growing it.
export DOTNET_GCConserveMemory="${YP_GC_CONSERVE:-7}"
if [ -n "${YP_GC_HEAP_LIMIT_MB:-}" ]; then
  DOTNET_GCHeapHardLimit="$(printf '0x%x' $((YP_GC_HEAP_LIMIT_MB * 1024 * 1024)))"
  export DOTNET_GCHeapHardLimit
fi
export MALLOC_ARENA_MAX=2
export YP_STEAM_STUB="${YP_STEAM_STUB:-off}"

# --- compressed swap (zram) on 1 GB devices --------------------------------------
# ROCKNIX on the H700 has no swap out of the box. Without it the kernel can only
# drop file-backed pages (the runtime's and the game's code) and re-read them from
# the SD card, and once that runs out the device freezes. Same block as the Fear &
# Hunger port, which measured 0.4 fps without and 13-18 fps with 1 GB of zstd zram
# on an RG40XX V. GPU buffers stay in RAM either way; everything else compresses.
# YP_ZRAM_MB=0 turns this off; YP_SWAP_FILE=/path adds a swap file instead when
# zram is unavailable.
zram_mb="${YP_ZRAM_MB:-}"
if [ -z "$zram_mb" ]; then
  if [ "${mem_total_mb:-0}" -lt 1800 ]; then zram_mb=1024; else zram_mb=0; fi
fi
have_swap() { [ "$(awk 'NR > 1 { n++ } END { print n + 0 }' /proc/swaps)" != "0" ]; }
if [ "$zram_mb" -gt 0 ] && ! have_swap; then
  [ -e /sys/class/zram-control/hot_add ] || $ESUDO modprobe zram 2>/dev/null
  if [ -e /sys/class/zram-control/hot_add ]; then
    zram_id=$($ESUDO cat /sys/class/zram-control/hot_add 2>/dev/null)
    if [ -n "$zram_id" ] && [ -b "/dev/zram$zram_id" ]; then
      echo zstd | $ESUDO tee "/sys/block/zram$zram_id/comp_algorithm" > /dev/null 2>&1
      if echo "${zram_mb}M" | $ESUDO tee "/sys/block/zram$zram_id/disksize" > /dev/null 2>&1 &&
         $ESUDO mkswap "/dev/zram$zram_id" > /dev/null 2>&1 &&
         $ESUDO swapon -p 100 "/dev/zram$zram_id" 2>/dev/null; then
        YP_ZRAM="$zram_id"
        echo "zram swap enabled on /dev/zram$zram_id (${zram_mb} MB, zstd)"
      else
        echo "$zram_id" | $ESUDO tee /sys/class/zram-control/hot_remove > /dev/null 2>&1
      fi
    fi
  fi
fi
if [ -n "${YP_SWAP_FILE:-}" ] && ! have_swap; then
  swap_mb="${YP_SWAP_MB:-1024}"
  if [ "$(stat -c %s "$YP_SWAP_FILE" 2>/dev/null)" != "$((swap_mb * 1024 * 1024))" ]; then
    echo "Creating a ${swap_mb} MB swap file at $YP_SWAP_FILE (only once) ..." | tee /dev/tty0
    $ESUDO rm -f "$YP_SWAP_FILE"
    { $ESUDO dd if=/dev/zero of="$YP_SWAP_FILE" bs=1M count="$swap_mb" 2>/dev/null &&
      $ESUDO chmod 600 "$YP_SWAP_FILE" && $ESUDO mkswap "$YP_SWAP_FILE" > /dev/null; } ||
      $ESUDO rm -f "$YP_SWAP_FILE"
  fi
  [ -f "$YP_SWAP_FILE" ] && $ESUDO swapon "$YP_SWAP_FILE" 2>/dev/null && SWAPFILE="$YP_SWAP_FILE"
fi
if ! have_swap && [ "${mem_total_mb:-0}" -lt 1800 ]; then
  echo "WARNING: no swap could be set up (zram module missing?). On 1 GB the game is likely"
  echo "to run out of memory; the watchdog will stop it before the device freezes."
fi
echo "memory: $(awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ { printf "%s %d MB  ", $1, $2 / 1024 }' /proc/meminfo)"

$ESUDO chmod 666 /dev/uinput 2>/dev/null

echo "ypport: mode=$YP_MODE entry=$YP_ENTRY engine=$YP_ENGINE $YP_ENGINE_VERSION ($YP_ENGINE_FLAVOR)" \
     "fmod=$YP_FMOD_VERSION runtime=$FRAMEWORK_DIR"

cd "$DATADIR/$YP_APPDIR" || { cleanup; exit 1; }

# Runs the game with the memory watchdog next to it: on 1 GB devices a game
# that exhausts memory otherwise freezes the whole system.
run_game() {
  "$@" $YP_ARGS &
  game_pid=$!
  bash "$GAMEDIR/tools/memwatch.sh" "$game_pid" "$GAMEDIR/memlog.txt" &
  wait "$game_pid"
  rc=$?
  sleep 1
  if grep -q "memwatch: out of memory" "$GAMEDIR/memlog.txt" 2>/dev/null; then
    grep "memwatch:" "$GAMEDIR/memlog.txt"
    pm_message "Yuppie Psycho ran out of memory and was stopped. See yuppiepsycho/memlog.txt."
    sleep 5
  fi
  tail -n 1 "$GAMEDIR/memlog.txt" 2>/dev/null
}

if [ "$YP_MODE" = "mono" ]; then
  # Older .NET Framework style builds run on PortMaster's Mono runtime.
  monofile="$controlfolder/libs/mono-6.12.0.122-aarch64.squashfs"
  if [ ! -f "$monofile" ]; then
    pm_message "This game build needs the mono-6.12.0.122 runtime. Install it from PortMaster (Runtimes)."
    cleanup; sleep 5; exit 1
  fi
  monodir="$HOME/mono"
  $ESUDO mkdir -p "$monodir"
  $ESUDO umount "$monofile" 2>/dev/null || true
  $ESUDO mount "$monofile" "$monodir"
  export PATH="$monodir/bin:$PATH"
  $GPTOKEYB "mono" &
  command -v pm_platform_helper > /dev/null && pm_platform_helper "$monodir/bin/mono"
  run_game mono "$(basename "$YP_ENTRY")"
  $ESUDO umount "$monodir"
else
  $GPTOKEYB "dotnet" &
  command -v pm_platform_helper > /dev/null && pm_platform_helper "$DOTNET_ROOT/dotnet"
  run_game "$DOTNET_ROOT/dotnet" "$(basename "$YP_ENTRY")"
fi
echo "Yuppie Psycho exited with status $rc"

cleanup
pm_finish
