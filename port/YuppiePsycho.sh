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

cleanup() {
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

# --- first launch: make the copied game runnable on aarch64 -----------------
python3 "$GAMEDIR/tools/ypport.py" status "$DATADIR" > /dev/null 2>&1
setup_status=$?
if [ $setup_status -ne 0 ]; then
  if ! command -v python3 > /dev/null 2>&1; then
    pm_message "python3 is required for the first-time setup."
    cleanup; sleep 5; exit 1
  fi
  if [ $setup_status -eq 2 ]; then
    # Prepared by an older version of the port: undo it, then set up again.
    python3 "$GAMEDIR/tools/ypport.py" restore "$DATADIR"
  elif [ -f "$DATADIR/.ypport/state.json" ]; then
    # The game was copied again (e.g. an update): forget the old setup.
    python3 "$GAMEDIR/tools/ypport.py" reset "$DATADIR"
  fi
  if [ -f "$controlfolder/utils/patcher.txt" ]; then
    export PATCHER_FILE="$GAMEDIR/tools/patchscript"
    export PATCHER_GAME="Yuppie Psycho"
    export PATCHER_TIME="1 to 3 minutes"
    source "$controlfolder/utils/patcher.txt"
  else
    $ESUDO chmod 666 /dev/tty0 2>/dev/null
    bash "$GAMEDIR/tools/patchscript" 2>&1 | tee /dev/tty0
  fi
  if ! python3 "$GAMEDIR/tools/ypport.py" status "$DATADIR" > /dev/null 2>&1; then
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
export YP_STEAM_STUB="${YP_STEAM_STUB:-off}"

$ESUDO chmod 666 /dev/uinput 2>/dev/null

echo "ypport: mode=$YP_MODE entry=$YP_ENTRY engine=$YP_ENGINE $YP_ENGINE_VERSION ($YP_ENGINE_FLAVOR)" \
     "fmod=$YP_FMOD_VERSION runtime=$FRAMEWORK_DIR"

cd "$DATADIR/$YP_APPDIR" || { cleanup; exit 1; }

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
  mono "$(basename "$YP_ENTRY")" $YP_ARGS
  rc=$?
  $ESUDO umount "$monodir"
else
  $GPTOKEYB "dotnet" &
  command -v pm_platform_helper > /dev/null && pm_platform_helper "$DOTNET_ROOT/dotnet"
  "$DOTNET_ROOT/dotnet" "$(basename "$YP_ENTRY")" $YP_ARGS
  rc=$?
fi
echo "Yuppie Psycho exited with status $rc"

cleanup
pm_finish
