# Technical notes

## What the game is built with

- The original 2019 release ran on Baroque Decay's in-house Java engine. The developer later said
  the engine was migrated to MonoGame, first for the console versions, then for PC.
- Version 2.5 (October 2022) moved the PC build to "the same engine as consoles", reworked audio
  around FMOD and restored Linux support (GamingOnLinux, Steam news "Major Engine Upgrade + Steam
  Deck Compatibility").
- MobyGames and PCGamingWiki list MonoGame, SDL and FMOD for the Executive Edition.

`ypport.py inspect` on the current Steam Linux build (1334 files, 1.4 GiB) confirms this and
fills in the details:

| Finding | Value |
|--|--|
| Packaging | self-contained .NET app, loose files (not single-file, not NativeAOT); apphosts `yuppiepsycho`, `game`, `tool-content-bridge`; 14 bundled x86_64 runtime libraries, 168 framework assemblies |
| Main assembly | `game.dll` 3.1.1.0, `.NETCoreApp,Version=v8.0` - the same major version as PortMaster's runtime |
| Engine | MonoGame.Framework **3.8.3.1111**, DesktopGL - a custom build: the stock 3.8.3-3.8.5 NuGet packages have no video playback on DesktopGL, this one adds it through Theorafile |
| Audio | FMOD Core only (`fmod`, 378 entry points; no FMOD Studio), C# wrapper header 2.02.08; NVorbis is present for MonoGame's own Ogg path |
| Video | `libtheorafile.so` (FNA's Theorafile), loaded by MonoGame's FuncLoader |
| Steam | Steamworks.NET 20.1.0 (Linux flavour, imports `steam_api` -> `libsteam_api.so`) |
| Other | NLog 5; `tool-content-bridge` (mod tooling, not needed to play); `Migration`, an x86_64 executable that converts pre-2.5 saves; folders `content`, `res`, `saves` |
| Shipped x86_64 natives | `libSDL2-2.0.so.0`, `libopenal.so`, `libfmod.so`, `libsteam_api.so`, `libtheorafile.so`, `Migration` |

`game.dll` also carries its own FuncLoader (`libdl.so.2`/`kernel32`/`libSystem.dylib` imports),
which is why `prepare` installs aarch64 replacements at the original paths instead of relying on
`LD_LIBRARY_PATH` alone.

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
   |    6. retarget CPU-stamped IL assemblies (Machine field, ReadyToRun) to aarch64
   |    7. install aarch64 replacements where x86_64 libraries were parked
   |       (libfmod.so, libopenal.so, libsteam_api.so, libtheorafile.so, ...)
   |    8. optionally store Color texture atlases as 16-bit (--textures)
   |    9. write .ypport/launch.env for the launcher
   v
gamedata/ runnable with `dotnet <game>.dll` on aarch64
```

"Park" means move into `gamedata/.ypport/backup/` preserving the relative path. `restore` puts
everything back byte-for-byte (tested), `reset` forgets the state without touching files (used
when the user copies a new game version over a prepared folder), `status` compares a fingerprint
(size and mtime) of the main assembly and of every file prepare parked, rewrote or installed, so
a re-copied game triggers a fresh setup.

### CPU-stamped IL assemblies

The first boot on the RG40XX H got through the setup and then failed with
`System.IO.FileLoadException: Could not load file or assembly 'game, Version=3.1.1.0'`.
`dotnet publish -r linux-x64` compiles the game's own assemblies with `PlatformTarget=x64`:
the code is still IL, but the PE header says `Machine=AMD64` (PE32+), and CoreCLR on aarch64
refuses to load an assembly stamped for another CPU. Steamworks.NET's linux-x64 flavour is
stamped the same way. The failure reproduces on x86_64 with the roles swapped (an assembly
stamped ARM64 gives exactly that exception).

`prepare` therefore rewrites the 2-byte Machine field of such assemblies to the device's CPU,
which makes them identical to what `PlatformTarget=arm64` would have produced. ReadyToRun images
contain precompiled code and native PE structures for their CPU; for those the native data
directories (export, import, exception, relocations, ...), the entry point and the ReadyToRun
header are cleared and the ILONLY flag set, turning them back into plain IL images whose section
layout, and therefore every metadata RVA, is unchanged; the runtime then JIT-compiles the IL.
PE32 x86-only assemblies (`32BITREQUIRED`) get that flag cleared. Original files go to the
backup like everything else.

The tests cover this mirrored: builds published for linux-arm64 (with and without ReadyToRun)
are prepared for x86_64 and run, including video decoding and the Steam stub.

A tool version is recorded in the state; `status` exits with 2 for a folder prepared by another
version, and the launcher then restores the original files and runs the setup again.

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

The package carries its own runtime in `yuppiepsycho/dotnet`: .NET 8.0.31 for linux-arm64,
laid out by `src/dotnet/assemble_runtime.py` from two immutable, Microsoft-signed NuGet packages
(`Microsoft.NETCore.App.Runtime.linux-arm64` for the shared framework and hostfxr,
`runtime.linux-arm64.Microsoft.NETCore.DotNetHost` for the `dotnet` muxer) into the standard
`DOTNET_ROOT` structure. It needs glibc 2.17. The same procedure for linux-x64 is exercised by
the tests, which run the stand-in game on the result. Built with `--no-dotnet`, the package
falls back to PortMaster's `dotnet-8.0.12.squashfs`.

Yuppie Psycho targets .NET 8, so it runs on either directly (the framework reference is
normalised to `8.0.0`, any 8.0.x patch release satisfies it); `rollForward: Major` only matters
for .NET 6 builds. Extra `configProperties` set by `prepare`:

| Property | Why |
|--|--|
| `System.Globalization.Invariant = true` | handheld firmwares have no ICU; without it .NET aborts at startup |
| `System.Runtime.Serialization.EnableUnsafeBinaryFormatterSerialization = true` | .NET 8 disables BinaryFormatter by default; harmless if unused, keeps old save code working |
| `System.GC.Concurrent = false`, `System.GC.Server = false` | no background GC thread, smaller footprint on 1 GB RAM |

Any aarch64 .NET runtime unpacked into `ports/yuppiepsycho/dotnet/` (with `dotnet`, `host/` and
`shared/`, e.g. Microsoft's `dotnet-runtime-8.0.x-linux-arm64.tar.gz`) is used the same way.

The launcher also sets `DOTNET_TieredPGO=0`, `DOTNET_TC_QuickJitForLoops=1` and
`DOTNET_EnableWriteXorExecute=0` to cut JIT overhead on the Cortex-A53.

### Native libraries

| Library | Source | Notes |
|--|--|--|
| SDL2 | the firmware | MonoGame DesktopGL loads `libSDL2-2.0.so.0` by name once the x86_64 copy is parked |
| OpenAL Soft 1.23.1 | PortMaster-New (SDL2 backend build) | shipped as `libopenal.so` (MonoGame 3.8.2+) and `libopenal.so.1` (older); MonoGame's own audio path, the game's sound goes through FMOD |
| FMOD 2.02.18 (core) | PortMaster-New | see below |
| Theorafile | `src/theorafile`, built from FNA-XNA/Theorafile at a pinned commit | see below |
| Steam API | `src/steamstub` | see below |
| gl4es | PortMaster-New | only used on firmwares whose `libgl_<CFW>.txt` asks for it; ROCKNIX has Mesa/Panfrost with desktop GL |

**FMOD.** The FMOD C# wrapper passes its header version to `System_Create`. Disassembling the
aarch64 2.02.18 library shows the check is `(headerversion >> 8) == 0x202`, i.e. only major and
minor are compared, so the game's 2.02.08 header is accepted. Yuppie Psycho uses FMOD Core only
(no Studio banks), which removes the bank-format question. For other versions the user drops
matching aarch64 libraries from the official FMOD Engine Linux package into
`yuppiepsycho/libs.fmod/`; the launcher passes that folder to `prepare` first and re-runs the
setup when its `libfmod.so` differs from the installed one.

**Theorafile.** The game's MonoGame build plays cutscenes with FNA's Theorafile. Its C# binding
allocates the `OggTheora_File` struct itself, 1472 bytes on 64-bit Unix (Theorafile-CS
`AllocTheoraFile`), and passes it to the library. x86_64 and aarch64 Linux are both LP64 with the
same alignment rules, so the layout is identical; `src/theorafile/build.sh` additionally
`_Static_assert`s that the struct (536 bytes at the pinned commit) fits. The pinned commit has
the newest API (`tf_videoinfo` with pixel format, multi-track functions). The library the game
ships exports `tf_setvideotrack`, i.e. it is of the same (2021-12+) API generation, and the
game's binding was written against it, so the two match. `inspect` also looks for the binding in
the metadata (delegate types, delegate or `delegate* unmanaged` fields, wrapper methods named
after the function) and prints the parameter count of `tf_videoinfo` it finds; the game's custom
MonoGame build hides it from the first version of that heuristic. The x86_64 build of the same
sources is tested in `tests/run_tests.sh`: a generated Theora video is decoded through a
FuncLoader-style C# binding, and valgrind is clean on 4:2:0 and 4:4:4 input.

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

## Memory on 1 GB devices

Second boot on the RG40XX H: the retargeted `game.dll` loaded and the game started loading its
content, then Mesa reported `DRM_IOCTL_PANFROST_CREATE_BO failed (err=12)` (ENOMEM: no memory
left for a GPU buffer) and the device froze. The H700 has 1 GB of RAM shared by the system, the
.NET runtime, the game and the Mali-G31; the game's PC minimum is 2 GB.

What the launcher does about it:

- **zram**: ROCKNIX has no swap by default. Without swap the kernel can only drop and re-read
  file-backed pages, and once those are gone the device freezes. The launcher sets up 1 GB of
  zstd-compressed zram for the run and removes it on exit - the same block as the
  [Fear & Hunger port](https://github.com/omka-1337/FearAndHunger-PortMaster), where it took a
  dungeon on an RG40XX V from 0.4 to 13-18 fps. GPU buffers cannot be swapped, but everything
  else the game holds (the managed heap, decoded data waiting for upload, code) compresses.
- **Watchdog** (`tools/memwatch.sh`): samples `/proc/meminfo` and the game's RSS twice a second
  into `memlog.txt` and kills the game when available RAM and free swap are both below 64 MB, so
  a run that does not fit ends with a message instead of a frozen device. The game also gets
  `oom_score_adj=1000` so the kernel's OOM killer picks it first.
- **.NET heap**: `DOTNET_GCConserveMemory=7` compacts (including the large object heap, where
  decoded images land) instead of growing; `MALLOC_ARENA_MAX=2` limits native heap arenas.
  `YP_GC_HEAP_LIMIT_MB` adds an optional hard cap.

Third boot (zram up, watchdog running, only 9 atlases converted): `memlog.txt` shows shmem -
i.e. GPU buffers - climbing from 18 MB to 702 MB within 63 seconds while available RAM fell
to ~50 MB, the game's own pages moved into zram (RSS 160 -> 8 MB, 125 MB swapped) and 727 MB
of zram stayed free. **The game uploads essentially all of its atlases at startup**, and pinned
GPU buffers cannot be swapped, so zram cannot help with them. The watchdog did not fire because
it treated free swap as headroom; it now ignores swap once GPU buffers exceed half of RAM, and
runs at a higher priority so a thrashing system does not starve it.

`ypport.py textures` on the real game: **62 texture atlases, 2048x2048 or smaller, all
`SurfaceFormat.Color`, 747.6 MB decoded** (50 MB on disk: LZ4-compressed XNB, mostly transparent
space). That alone exceeds what a 1 GB device can give the GPU if the game holds all of them.

**16-bit atlases.** `prepare --textures 16bit` (the launcher's default below 1.8 GB of RAM)
rewrites every Color texture XNB in a 16-bit format that MonoGame's `Texture2DReader` loads
natively: `Bgra5551` when the alpha is only 0/255 or within 15 of either (anti-aliasing
fringes), `Bgra4444` otherwise. `--textures auto16` converts only the former. On the real game
`textures --alpha` finds 9 binary and 53 soft-alpha atlases, so `auto16` saves almost nothing
(727.9 MB) and `16bit` halves the total (373.8 MB). Bgra4444 means 4 bits per channel; whether
the banding is acceptable is checked with `xnb2png --as 16bit` on the real atlases. The XNB
is decoded and re-encoded in pure Python: LZ4 block decoding with slice copies, channel packing
with `bytes.translate`, big-integer OR and slice interleaving, and an LZ4 encoder that only
emits runs (offset-1 matches), which is enough for transparent texture space - about 0.4 s per
2048x2048 atlas on a PC and roughly 1 MB per converted file. The codec is checked against the
reference `lz4` library in both directions, and the tests load converted XNBs through MonoGame's
`ContentManager`, draw them 1:1 into a render target under Xvfb (Mesa) and compare every pixel
with the expected quantised values. `ypport.py textures --alpha` projects the memory per mode.

If the atlases still do not fit, the next steps are half-resolution GL textures that keep their
logical size (so the game's source rectangles stay valid), which needs a hook into MonoGame's
texture upload, or evicting atlases the current scene does not use.

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

- **Video performance**: Theora decoding on the Cortex-A53 uses the NEON code paths of
  libtheora; cutscene resolution decides whether it keeps up.
- **SDL2 version**: the game's MonoGame was built against SDL 2.32; the firmware's SDL2 must
  export every function MonoGame binds at startup.
- **Graphics**: MonoGame DesktopGL needs desktop OpenGL. ROCKNIX on the H700 uses Mesa's Panfrost
  driver for the Mali-G31, which provides desktop GL; gl4es is the fallback on GLES-only firmwares.
- **Memory**: .NET 8 + the game on 1 GB RAM. ROCKNIX supports swap/zram if needed.
- **Screen**: the game is 16:9, the RG40XX H is 640x480. Expect letterboxing; the backbuffer
  size the game requests may need a settings tweak.
- **Startup time**: everything is JIT-compiled on first run. Precompiling the game assemblies to
  ReadyToRun for linux-arm64 on a PC (crossgen2) is the obvious optimisation once it runs.
- **Saves**: the game keeps them in `saves/` inside the game folder, so saves copied from a PC
  come along in `gamedata/saves`; XDG paths are redirected to `yuppiepsycho/savedata` as a
  catch-all. Pre-2.5 saves need the x86_64 `Migration` tool, so convert them on the PC first.

## Roadmap

1. ~~Run `inspect` on the real Steam Linux build~~ (done: .NET 8 self-contained, MonoGame 3.8.3
   DesktopGL + Theorafile, FMOD Core 2.02, Steamworks.NET 20.1).
2. ~~First boot on ROCKNIX / RG40XX H~~: setup and runtime work; `game.dll` needed retargeting
   (done); content loading ran out of memory until the atlases were stored as 16-bit (done) -
   the game now runs, and runs well.
3. Video playback, resolution/scaling, controls.
4. Performance: ReadyToRun precompilation, GC settings, swap.
5. Screenshot/cover, test on other firmwares (muOS, Knulli, ArkOS), submit to PortMaster-New.
