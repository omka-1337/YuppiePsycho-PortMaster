# -*- coding: utf-8 -*-
"""
Yuppie Psycho recipe for Omka-Patcher (tools/omkapatcher.py, fetched by
build.sh from https://github.com/omka-1337/Omka-Patcher).

On a PC (`./patch`, `patch.bat`) the patcher copies the game from Steam into
gamedata and calls patch(), which runs the same `ypport.py prepare` the
handheld runs on first launch, with the texture and video modes profile()
picked for the chosen model. On the handheld the launcher calls
`omkapatcher.py --detect WxH`, which applies profile() to the hardware it
runs on.

The original game stays in Steam, so prepare keeps no backup of the files it
replaces (nothing extra on the SD card); for another handheld the patcher
copies the game afresh. A game prepared for a handheld with more memory does
not start on one with less (`ypport.py status --textures-max`).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ypport  # noqa: E402

NAME = "Yuppie Psycho"
LAUNCHER = "YuppiePsycho.sh"
GAME_MARKER = "game.dll"
STEAM_APP_ID = "597760"
STEAM_DIR = "Yuppie Psycho"
KEEP = ("saves",)  # the game keeps its saves in its own folder
STATE_FILE = ypport.STATE_DIR + "/state.json"
ENV_PREFIX = "YP_"  # --detect prints YP_AUTO_TEXTURES, YP_AUTO_VIDEOS, YP_DEVICE_*
ERRORS = (ypport.PrepareError, ypport.BundleError)
OPTIONS = [("textures", "Textures"), ("videos", "Cutscenes")]

# Below this much RAM (MemTotal: ~0.95 GB on a 1 GB device, ~1.9 GB on a
# 2 GB one) the texture atlases (~750 MB decoded) do not fit next to the
# system, and are stored as 16-bit.
LOW_MEMORY_MB = 1800
# Re-encoded cutscenes are never larger than this, whatever the screen.
VIDEO_BOX_MAX = (960, 540)
TEXTURE_LABELS = {
    "full": "as shipped (32-bit)",
    "auto16": "16-bit where transparency is all-or-nothing",
    "16bit": "16-bit (half the memory)",
}
STEAMCMD_LINUX = ("steamcmd +@sSteamCmdForcePlatformType linux +login <user> "
                  "+app_update %s validate +quit" % STEAM_APP_ID)


def profile(device):
    """Textures by memory; cutscenes fit the screen unless the CPU has a fast
    core (a Cortex-A53 cannot decode the HD originals in real time)."""
    textures = "16bit" if device.ram_mb < LOW_MEMORY_MB else "full"
    if device.fast_cpu:
        videos = "full"
    else:
        w, h = device.screen or (640, 480)
        videos = "%dx%d" % (min(w, VIDEO_BOX_MAX[0]), min(h, VIDEO_BOX_MAX[1]))
    return {"textures": textures, "videos": videos}


def choices(name, device, default):
    if name == "textures":
        return [(m, TEXTURE_LABELS[m]) for m in ("16bit", "auto16", "full")]
    fit = default if default != "full" else "%dx%d" % VIDEO_BOX_MAX
    return [(fit, "fit " + fit), ("full", "original size")]


def describe(options):
    t = options["textures"]
    v = options["videos"]
    return ["Textures:  " + ("keep as shipped" if t == "full" else "store " + TEXTURE_LABELS[t]),
            "Cutscenes: " + ("keep the originals" if v == "full" else "re-encode those larger than " + v)]


def warning(device, options):
    needed = profile(device)["textures"]
    if ypport.TEXTURE_MEMORY_RANK[options["textures"]] > ypport.TEXTURE_MEMORY_RANK[needed]:
        return ["Warning: these textures need more memory than %d GB; the handheld will"
                % round(device.ram_mb / 1024.0),
                "refuse to start the game and ask for the patcher to be run again."]
    return []


def check(ctx):
    """Why the patcher cannot run from this folder, or None."""
    if not os.path.isdir(os.path.join(ctx.port_dir, "libs.aarch64")):
        repo = os.path.dirname(os.path.dirname(ctx.port_dir))
        if os.path.isfile(os.path.join(repo, "build.sh")):
            # port/yuppiepsycho of a checkout: the binaries are added by build.sh
            return ("This is the port's source folder, without the built libraries and tools.\n"
                    "Run ./build.sh, then start build/stage/yuppiepsycho/patch\n"
                    "(or unzip dist/yuppiepsycho.zip and run the patch in there).")
        return "yuppiepsycho/libs.aarch64 is missing: unzip the whole yuppiepsycho.zip and run the patcher in there."
    if ctx.host_binary(ypport.VIDEO_TOOL) is None:
        return "No cutscene re-encoder (tools/bin.*/%s) for this PC." % ypport.VIDEO_TOOL
    return None


def check_game(game):
    """The macOS build keeps its content in the .app bundle's Resources,
    apart from the assemblies; the port needs the Linux or Windows layout."""
    if (".app" + os.sep + "Contents") in game:
        return ("macOS build, not usable: the port needs the Linux or Windows build.\n"
                "Download the Linux build with SteamCMD:\n  " + STEAMCMD_LINUX +
                "\nand enter its folder in the patcher.")
    return None


def _framework_dir(port_dir):
    shared = os.path.join(port_dir, "dotnet", "shared", "Microsoft.NETCore.App")
    try:
        versions = sorted(os.listdir(shared), key=lambda v: [int(x) if x.isdigit() else 0
                                                              for x in v.replace("-", ".").split(".")])
    except OSError:
        return None
    return os.path.join(shared, versions[-1]) if versions else None


def patch(ctx):
    native_dirs = []
    if os.path.isfile(os.path.join(ctx.port_dir, "libs.fmod", "libfmod.so")):
        native_dirs.append(os.path.join(ctx.port_dir, "libs.fmod"))  # FMOD supplied by the player
    native_dirs.append(os.path.join(ctx.port_dir, "libs.aarch64"))
    # No backup of the files prepare replaces: the original is the Steam copy.
    ypport.Preparer(ctx.data_dir, framework_dir=_framework_dir(ctx.port_dir), log=ctx.log,
                    native_dirs=native_dirs, native_arch="aarch64",
                    textures=ctx.options["textures"], videos=ctx.options["videos"],
                    video_tool=ctx.host_binary(ypport.VIDEO_TOOL), jobs=ctx.jobs,
                    device=ctx.device.name, backup=False).run()
