## Notes

Thanks to [Baroque Decay](https://baroquedecay.com/) for creating Yuppie Psycho, and to the
[MonoGame](https://github.com/MonoGame/MonoGame), [.NET](https://github.com/dotnet/runtime),
[gl4es](https://github.com/ptitSeb/gl4es) and [OpenAL Soft](https://github.com/kcat/openal-soft)
projects. Audio by FMOD Studio by Firelight Technologies Pty Ltd.

**Experimental.** This is a native port: the game's own .NET code runs on an aarch64 .NET
runtime. No x86 emulation is involved. The .NET 8 runtime is included in the port
(`yuppiepsycho/dotnet`); nothing else has to be installed.

Yuppie Psycho v2.5 and newer runs on MonoGame (.NET 8). Older versions (the original Java
engine) are not supported. Saves from a PC can be copied along (`gamedata/saves`); saves from
before v2.5 must be converted by running the game once on the PC.

## Installation

1. Install the game on Steam. The **Linux** build is preferred; the **Windows** build works too
   as long as it is a MonoGame DesktopGL build (the setup checks this).
   - The Linux build can be taken from a Linux PC or a Steam Deck, or downloaded on any PC with
     SteamCMD: `steamcmd +@sSteamCmdForcePlatformType linux +login <user> +app_update 597760 validate +quit`
2. On a PC (recommended): unzip the port there and run the patcher, `yuppiepsycho/patch.bat`
   on Windows or `./patch` in `yuppiepsycho/` on Linux and macOS (needs Python 3.6+). It copies
   the game from Steam into `yuppiepsycho/gamedata` and prepares it for the handheld you pick
   (maker, then model) in a few minutes. Then copy `YuppiePsycho.sh` and the `yuppiepsycho`
   folder into the ports folder on the SD card.

   Without a PC: copy the **contents** of the game's install folder (the folder with
   `game.dll`, `content/`, `res/`, `saves/`) into `ports/yuppiepsycho/gamedata/`, so that the
   file `ports/yuppiepsycho/gamedata/game.dll` exists.
3. Start *Yuppie Psycho* from the Ports menu. A game prepared by the patcher starts directly.
   Otherwise the first launch prepares the files for ARM (sets aside the x86_64 .NET runtime
   and libraries and puts aarch64 ones in their place), stores the texture atlases as 16-bit on
   1 GB devices and re-encodes the cutscenes to the screen size. This takes 5 to 30 minutes and
   only happens again when the game files or these settings change.

On the handheld, the original files that the setup sets aside are kept in
`gamedata/.ypport/backup`; nothing is deleted. The patcher keeps no such copy (the original is
in Steam): to prepare for another handheld, run it again. A game prepared for a handheld with
more memory does not start on one with less; the launcher asks for the patcher to be run
again.

## Options

Create `ports/yuppiepsycho/settings.txt` to override defaults, for example:

```
# Pretend Steam is running (offline) if the game refuses to start without it
YP_STEAM_STUB=fake
# Extra command line arguments for the game
YP_ARGS=""
# Size of the compressed swap (zram) set up on 1 GB devices, 0 = off (default 1024)
YP_ZRAM_MB=1024
# Use a swap file instead when the zram module is missing (written once, kept)
YP_SWAP_FILE=/storage/yuppiepsycho.swap
# Stop the game when available RAM and free swap both drop below this (MB, default 64)
YP_MEMWATCH_MIN_MB=64
# Optional hard cap for the .NET heap in MB (the game then fails with OutOfMemory instead)
YP_GC_HEAP_LIMIT_MB=
# Texture atlases: full = as shipped, 16bit = all 16-bit (default on 1 GB devices),
# auto16 = 16-bit only where transparency is all-or-nothing. Changing it re-runs the
# setup on the next launch.
YP_TEXTURES=16bit
# Cutscenes larger than this are re-encoded to fit it (default: the screen size, at most
# 960x540; full on chips with a Cortex-A75 class core such as Snapdragon and RK3588);
# full = keep the original videos. Changing it re-runs the setup.
YP_VIDEOS=640x480
```

### Memory

The RG40XX H and similar devices have 1 GB of RAM shared between the system, the game and the
GPU, half of the game's PC minimum. The launcher therefore sets up 1 GB of compressed swap
(zram, zstd) for the duration of the game, and runs a watchdog that stops the game before the
device runs out of memory, because an exhausted 1 GB device freezes instead of recovering. The
watchdog writes `ports/yuppiepsycho/memlog.txt` twice a second: available RAM, free swap,
GPU/shmem, game memory, CPU use of the game and of its main thread (100 = one core busy), CPU
clock and temperature.

The game's 62 texture atlases need about 750 MB once decoded, more than a 1 GB device can give
the GPU. On such devices the setup stores them as 16-bit, which halves that to about 375 MB:
atlases whose transparency is all-or-nothing (or nearly) become Bgra5551 (colours rounded to 5
bits per channel, transparency kept), the others Bgra4444 (4 bits per channel and 16 levels of
transparency; smooth gradients can show bands). On devices with 2 GB or more nothing is changed.

To see what an atlas looks like after conversion, on a PC:

```
python3 tools/ypport.py xnb2png gamedata/content/atlas/hero_0.xnb hero.png
python3 tools/ypport.py xnb2png gamedata/content/atlas/hero_0.xnb hero-16bit.png --as 16bit
```

### Cutscenes

The cutscenes are Ogg Theora videos, decoded in software on one CPU core. At their original
size a Cortex-A53 cannot decode them in real time, so they stutter, mostly in scenes with a lot
of motion. The setup therefore re-encodes videos that are larger than the screen to fit it
(640x360 on a 640x480 screen); sound and timing are kept. `patchlog.txt` lists every video with
its decoding time per frame before and after. The original videos are kept in
`gamedata/.ypport/backup`; `YP_VIDEOS=full` in `settings.txt` puts them back. To list the
videos (size, frame rate, length) on a PC or on the device:

```
python3 tools/ypport.py videos gamedata
```

On the device (over SSH), `--bench` also decodes every video and reports the time per frame.

### FMOD

The bundled aarch64 FMOD is 2.02.18, which accepts the game's 2.02 headers. If a future game
update moves to another FMOD version and there is no sound, download the *FMOD Engine* for Linux
from fmod.com (same version as reported in `log.txt`) and copy `api/core/lib/arm64/libfmod.so`
into `ports/yuppiepsycho/libs.fmod/` (a real file, not a symlink - SD cards often cannot hold
symlinks); the next launch re-runs the setup with it.

## Controls

The game supports gamepads natively through SDL2; the handheld's controls are used as a
standard gamepad.

| Button | Action |
|--|--|
| Select + Start | Quit (PortMaster hotkey) |

## Troubleshooting

- `ports/yuppiepsycho/log.txt` - last game run
- `ports/yuppiepsycho/patchlog.txt` - first-time setup
- `ports/yuppiepsycho/inspect.txt` - report about the game files when setup fails
- `ports/yuppiepsycho/memlog.txt` - memory and CPU over time during the last run
- To redo the setup from scratch, delete `gamedata/.ypport` **after** re-copying the game files.
