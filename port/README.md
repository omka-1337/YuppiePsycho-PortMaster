## Notes

Thanks to [Baroque Decay](https://baroquedecay.com/) for creating Yuppie Psycho, and to the
[MonoGame](https://github.com/MonoGame/MonoGame), [.NET](https://github.com/dotnet/runtime),
[gl4es](https://github.com/ptitSeb/gl4es) and [OpenAL Soft](https://github.com/kcat/openal-soft)
projects. Audio by FMOD Studio by Firelight Technologies Pty Ltd.

**Experimental.** This is a native port: the game's own .NET code runs on an aarch64 .NET
runtime. No x86 emulation is involved.

Yuppie Psycho v2.5 and newer runs on MonoGame (.NET). Older versions (the original Java
engine) are not supported.

## Installation

1. Install the game on Steam. The **Linux** build is preferred; the **Windows** build works too
   as long as it is a MonoGame DesktopGL build (the setup checks this).
   - The Linux build can be taken from a Linux PC or a Steam Deck, or downloaded on any PC with
     SteamCMD: `steamcmd +@sSteamCmdForcePlatformType linux +login <user> +app_update 597760 validate +quit`
2. Copy the **complete contents** of the game folder into `ports/yuppiepsycho/gamedata/`.
3. Start *Yuppie Psycho* from the Ports menu. The first launch prepares the files for ARM
   (extracts the .NET single-file bundle, sets aside the x86_64 runtime and libraries). This
   takes a minute or two and only happens again when the game files change.

The original files that the setup sets aside are kept in `gamedata/.ypport/backup`; nothing is
deleted.

## Options

Create `ports/yuppiepsycho/settings.txt` to override defaults, for example:

```
# Pretend Steam is running (offline) if the game refuses to start without it
YP_STEAM_STUB=fake
# Extra command line arguments for the game
YP_ARGS=""
```

If the game uses a different FMOD version than the bundled 2.02.18 and has no sound, download
the *FMOD Engine* for Linux from fmod.com (same version as reported in `log.txt`), and copy
`api/core/lib/arm64/libfmod.so*` and `api/studio/lib/arm64/libfmodstudio.so*` into
`ports/yuppiepsycho/libs.fmod/`. Keep the versioned names (e.g. `libfmod.so.13`) and add copies
named `libfmod.so` and `libfmodstudio.so` (SD cards often cannot hold symlinks).

## Controls

The game supports gamepads natively through SDL2; the handheld's controls are used as a
standard gamepad. Buttons can be changed in the game's options.

| Button | Action |
|--|--|
| Select + Start | Quit (PortMaster hotkey) |

## Troubleshooting

- `ports/yuppiepsycho/log.txt` - last game run
- `ports/yuppiepsycho/patchlog.txt` - first-time setup
- `ports/yuppiepsycho/inspect.txt` - report about the game files when setup fails
- To redo the setup from scratch, delete `gamedata/.ypport` **after** re-copying the game files.
