# Technical notes

## What the game is built with

- The original 2019 release ran on Baroque Decay's in-house Java engine. The developer later said
  the engine was migrated to MonoGame, first for the console versions, then for PC.
- Version 2.5 (October 2022) moved the PC build to "the same engine as consoles", reworked audio
  around FMOD and restored Linux support (GamingOnLinux, Steam news "Major Engine Upgrade + Steam
  Deck Compatibility").
- MobyGames and PCGamingWiki list MonoGame, SDL and FMOD for the Executive Edition.

None of this has been checked against the files yet; `ypport.py inspect` is the tool for that.
The port assumes v2.5+ and refuses anything it cannot recognise rather than guessing.

## The pipeline

```
gamedata/ (as copied from Steam)
   |
   |  ypport.py inspect   - classify every file, read .NET metadata
   v
   |  ypport.py prepare
   |    1. extract the single-file bundle (if any) next to it
   |    2. park x86_64 ELF files (apphost, libSDL2, libopenal, libfmod, libsteam_api, ...)
   |    3. park the bundled .NET runtime (files the deps.json attributes to the runtime pack,
   |       or that the device runtime provides)
   |    4. rewrite <game>.runtimeconfig.json: framework-dependent, rollForward=Major
   |    5. rewrite <game>.deps.json: drop the runtime pack and every parked file
   |    6. write .ypport/launch.env for the launcher
   v
gamedata/ runnable with `dotnet <game>.dll` on aarch64
```

"Park" means move into `gamedata/.ypport/backup/` preserving the relative path. `restore` puts
everything back byte-for-byte (tested), `reset` forgets the state without touching files (used
when the user copies a new game version over a prepared folder), `status` compares a fingerprint
of the main assembly and of the parked paths, so a re-copied game triggers a fresh setup.

### Single-file bundles

A .NET single-file app is an apphost executable with the managed files appended. The apphost
carries a 32-byte signature preceded by the offset of the bundle header. The header has the
format version (1 = .NET Core 3, 2 = .NET 5, 6 = .NET 6+), file count, bundle id, the locations
of deps.json and runtimeconfig.json, and one entry per file (offset, size, compressed size from
v6, type, relative path). Compressed entries are raw deflate. `ypport.py` implements this from
scratch (stdlib only) with streaming decompression so it runs on a 1 GB device, and rejects
absolute paths, `..` components and out-of-range entries. The same format is used for Linux
(ELF), Windows (PE) and macOS (Mach-O) apphosts; the tests cover ELF and PE.

On .NET 6+ the runtime itself (coreclr, the JIT) is statically linked into the single-file host,
so after extraction only managed assemblies and app-level native libraries are left.

### .NET metadata reader

`ypport.py` contains a small ECMA-335 reader (PE sections, CLI header, metadata streams, all 45
table schemas with coded-index sizing). It is used to read:

- assembly name/version, references and `TargetFrameworkAttribute` -> .NET vs. Mono, version,
- `ImplMap`/`ModuleRef` (P/Invoke) and user strings -> every native library the code loads,
- the `FMOD.VERSION.number` constant of the FMOD C# wrapper -> FMOD version the game expects,
- type names -> MonoGame flavour (DesktopGL/WindowsDX/Android), Steamworks wrapper,
- CLI flags -> IL-only vs. mixed-mode (C++/CLI); ReadyToRun images are fine because their
  precompiled x86_64 code is ignored on arm64 and the IL is JIT-compiled instead.

### Runtime

PortMaster ships `dotnet-8.0.12.squashfs` (aarch64). A game built for .NET 6 runs on it via
`rollForward: Major`. Extra `configProperties` set by `prepare`:

| Property | Why |
|--|--|
| `System.Globalization.Invariant = true` | handheld firmwares have no ICU; without it .NET aborts at startup |
| `System.Runtime.Serialization.EnableUnsafeBinaryFormatterSerialization = true` | .NET 8 disables BinaryFormatter by default; harmless if unused, keeps old save code working |
| `System.GC.Concurrent = false`, `System.GC.Server = false` | no background GC thread, smaller footprint on 1 GB RAM |

If the roll-forward to .NET 8 turns out to break something, an aarch64 .NET 6 runtime unpacked
into `ports/yuppiepsycho/dotnet/` (with `dotnet`, `host/` and `shared/`) takes precedence over the
PortMaster runtime without any other change.

The launcher also sets `DOTNET_TieredPGO=0`, `DOTNET_TC_QuickJitForLoops=1` and
`DOTNET_EnableWriteXorExecute=0` to cut JIT overhead on the Cortex-A53.

### Native libraries

| Library | Source | Notes |
|--|--|--|
| SDL2 | the firmware | MonoGame DesktopGL loads `libSDL2-2.0.so.0` by name once the x86_64 copy is parked |
| OpenAL Soft 1.23.1 | PortMaster-New (SDL2 backend build) | MonoGame's own audio path; the game's music/SFX are expected to go through FMOD |
| FMOD 2.02.18 | PortMaster-New | see below |
| Steam API | `src/steamstub` | see below |
| gl4es | PortMaster-New | only used on firmwares whose `libgl_<CFW>.txt` asks for it; ROCKNIX has Mesa/Panfrost with desktop GL |

**FMOD.** The FMOD C# wrapper passes its header version to `System_Create`, and the library
rejects a mismatched header. The expectation (to be confirmed on the device) is that any 2.02.x
library accepts a 2.02.x header and loads banks built with an equal or older 2.02 FMOD Studio, so
2.02.18 (the newest aarch64 FMOD in PortMaster; 2.02.06 is also available there) is the default. `inspect`/`log.txt`
report the version the game was built with; for 2.03 or a mismatch, the user drops the matching
aarch64 libraries from the official FMOD Engine Linux package into `yuppiepsycho/libs.fmod/`,
which the launcher puts first on `LD_LIBRARY_PATH`.

**Steam.** Valve ships `libsteam_api` only for x86/x86_64. The stub exports all 1092 flat-API
entry points used by Steamworks.NET 15.0.1 / 20.1.0 / 20.2.0 / 2024.8.0 and Facepunch.Steamworks
2.3.3 (collected from their P/Invoke tables with `ypport.py`), under both `libsteam_api.so` and
`libsteam_api64.so` (Windows builds of the wrappers ask for `steam_api64`).

- default: Steam "is not running" - `SteamAPI_Init` returns false, `RestartAppIfNecessary`
  returns false. Correct for games that treat Steam as optional.
- `YP_STEAM_STUB=fake`: init succeeds, interface accessors return a dummy object, every call on
  it returns 0/false/NULL, language queries return `english` (or `YP_STEAM_LANGUAGE`), the
  persona name is `Player`. For games that refuse to start without Steam. Verified with
  Steamworks.NET 20.2's full `CSteamAPIContext.Init()`.
- `YP_STEAM_STUB_TRACE=1` logs the first call of each entry point, which shows exactly which
  Steam features the game touches.

## If the build is NativeAOT

NativeAOT compiles the IL to machine code and drops it, so there is nothing to retarget. The
inspector reports this ("NativeAOT build"). Options, from easiest:

1. Use another build of the same version that still has IL: the Windows build (if DesktopGL),
   the GOG build, or the macOS build (MonoGame on macOS is DesktopGL; the content lives in
   `Contents/Resources` and would need to be moved next to the assemblies).
2. Use an older Steam branch, if the developer kept one with the pre-AOT packaging.
3. The Android release (Crunchyroll Game Vault, 2024) is most likely MonoGame on .NET for
   Android, which keeps IL in the APK even when it also AOT-compiles (to be confirmed). Its assemblies could run on desktop .NET with
   `MonoGame.Framework.Android` swapped for DesktopGL and a small launcher replacing the Android
   activity. This is real work but it is the "hard but possible" native path.

## If the build is MonoGame WindowsDX

WindowsDX uses Direct3D shaders compiled into the content files and SharpDX, neither of which
exists on Linux. The Steam Linux build is DesktopGL by definition; use that one.

## Open questions

These need the real game and the device:

- **Packaging of the real build**: single-file or not, .NET 6 or 8, NativeAOT or not -> `inspect`.
- **Video**: the old Java engine had `novlc`/`noffmpeg` launch options, i.e. VLC/FFmpeg cutscenes. MonoGame DesktopGL has no video
  playback, so the current engine brings its own; `inspect` lists the library it imports. An
  x86_64 video library would need an aarch64 replacement (FFmpeg/libVLC are usually available).
- **Graphics**: MonoGame DesktopGL needs desktop OpenGL. ROCKNIX on the H700 uses Mesa's Panfrost
  driver for the Mali-G31, which provides desktop GL; gl4es is the fallback on GLES-only firmwares.
- **Memory**: .NET 8 + the game on 1 GB RAM. ROCKNIX supports swap/zram if needed.
- **Screen**: the game is 16:9, the RG40XX H is 640x480. Expect letterboxing; the backbuffer
  size the game requests may need a settings tweak.
- **Startup time**: everything is JIT-compiled on first run. Precompiling the game assemblies to
  ReadyToRun for linux-arm64 on a PC (crossgen2) is the obvious optimisation once it runs.
- **Saves**: v2.5 creates its save folder inside the game folder, so saves live in
  `gamedata/`; XDG paths are redirected to `yuppiepsycho/savedata` as a catch-all.

## Roadmap

1. Run `inspect` on the real Steam Linux and Windows builds; adjust detection if needed.
2. First boot on ROCKNIX / RG40XX H: logs, Steam behaviour (`YP_STEAM_STUB_TRACE=1`), FMOD banks.
3. Video playback, resolution/scaling, controls.
4. Performance: ReadyToRun precompilation, GC settings, swap.
5. Screenshot/cover, test on other firmwares (muOS, Knulli, ArkOS), submit to PortMaster-New.
