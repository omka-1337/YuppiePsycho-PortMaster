# Yuppie Psycho for PortMaster

A **native** aarch64 port of *Yuppie Psycho: Executive Edition* for Linux handhelds running
[PortMaster](https://portmaster.games) (developed on an Anbernic RG40XX H with ROCKNIX). No x86
emulation: the game's own code runs on an aarch64 .NET runtime.

> **Status: experimental, not yet booted on a device.** The inspector has been run on the real
> Steam Linux build: it is a self-contained **.NET 8** MonoGame 3.8.3 DesktopGL app with IL game
> code (no NativeAOT), FMOD Core 2.02, Theorafile video and Steamworks.NET 20.1, which the port
> covers. Every piece is tested against real .NET SDK output and a MonoGame stand-in game with the
> same shape (see [Testing](#testing)); running on the RG40XX H is the next step.

## Why a native port is possible

Since v2.5 (October 2022) the PC version runs on the same engine as the console versions:
MonoGame on .NET, with FMOD for audio and Steamworks for achievements. The game logic is
therefore CIL bytecode, which is CPU independent. What ties the PC release to x86_64 is only the
*packaging*:

| x86_64-only piece | What the port does |
|--|--|
| .NET apphost (the executable), possibly a single-file bundle | sets it aside; extracts the IL assemblies if it is a bundle |
| Bundled .NET runtime (`libcoreclr.so`, `System.Private.CoreLib.dll`, ...) | sets it aside; the port ships Microsoft's aarch64 .NET 8 runtime instead |
| `runtimeconfig.json` / `deps.json` describing a self-contained x86_64 app | rewrites them for a framework-dependent app with roll-forward |
| `libSDL2`, `libopenal` | uses aarch64 builds (system SDL2, bundled OpenAL Soft) |
| `libfmod` (FMOD Core) | bundles aarch64 FMOD 2.02.18 (user-replaceable) |
| `libtheorafile` (cutscene video) | builds FNA's Theorafile for aarch64 |
| `libsteam_api` (Valve ships no ARM build) | bundles a stub exporting the full flat Steam API |

The one case where this does **not** work is a NativeAOT build, where the IL has been compiled
away to x86_64 machine code. The inspector detects that and says so; the fallback is to use a
build from another platform or store that still ships IL (see
[docs/TECHNICAL.md](docs/TECHNICAL.md#if-the-build-is-nativeaot)).

## Repository layout

```
port/                         files that go into the PortMaster package
  YuppiePsycho.sh             launcher
  port.json, README.md, gameinfo.xml
  yuppiepsycho/tools/ypport.py      inspect / extract / prepare / restore (stdlib-only Python)
  yuppiepsycho/tools/patchscript    first-launch setup run by PortMaster's patcher UI
src/steamstub/                Steam API stub (C, generated from the wrappers' P/Invoke tables)
src/theorafile/               aarch64 build of FNA's Theorafile (pinned commit, struct-size check)
src/dotnet/                   lays out the .NET runtime from Microsoft's NuGet packages
tests/                        end-to-end tests, unit tests, launcher simulation, stand-in game
build.sh                      builds dist/yuppiepsycho.zip
docs/TECHNICAL.md             how each step works, decisions, open questions
```

## Step 1: inspect your copy of the game

Run this on a PC against the installed game folder (Python 3, nothing to install):

```
python3 port/yuppiepsycho/tools/ypport.py inspect "/path/to/Yuppie Psycho"
```

On Steam for Linux the folder is usually `~/.local/share/Steam/steamapps/common/Yuppie Psycho`,
on Windows `C:\Program Files (x86)\Steam\steamapps\common\Yuppie Psycho`. The report shows:

- how the game is packaged (single-file bundle, self-contained, framework-dependent, NativeAOT,
  .NET Framework/Mono),
- the target framework, MonoGame version and flavour (DesktopGL vs. WindowsDX),
- the FMOD header version compiled into the game,
- every native library the code imports (P/Invoke tables and library names in the code),
- a verdict on whether the build can run natively.

`--json` prints the full data. Sharing this report is the fastest way to find out what, if
anything, still needs work for the real game.

## Step 2: build the package

On a Linux PC with `python3`, `git`, `curl` and `zip`:

```
pip install ziglang     # cross compiler for the aarch64 Steam stub and Theorafile
./build.sh              # -> dist/yuppiepsycho.zip (about 33 MB)
```

`build.sh` compiles the Steam stub and Theorafile, downloads the aarch64 gl4es, OpenAL Soft and
FMOD binaries from [PortMaster-New](https://github.com/PortsMaster/PortMaster-New) at a pinned
commit and the aarch64 **.NET 8.0.31 runtime** from Microsoft's NuGet packages, verifying every
download against a SHA-256 sum. The runtime goes into the package (`yuppiepsycho/dotnet/`), so
nothing has to be installed on the device. `./build.sh --no-dotnet` makes a 3 MB package that uses
PortMaster's `dotnet-8.0.12` runtime instead (PortMaster downloads it when it installs the port).

## Step 3: install on the device

PortMaster must be present (ROCKNIX ships it under Tools; start it once so it sets itself up).

1. Extract `dist/yuppiepsycho.zip` into the ports folder: `/storage/roms/ports` on ROCKNIX
   (over SSH/SFTP/Samba), or `roms/ports` when the games SD card is in a PC.
2. Copy the **contents** of the game's install folder (the folder that has `game.dll`,
   `yuppiepsycho`, `content/`, `res/`, `saves/`) into `ports/yuppiepsycho/gamedata/`.
3. Launch *Yuppie Psycho* from Ports. The first launch runs the setup (about a minute), later
   launches start the game directly.

The result looks like this:

```
roms/ports/
  YuppiePsycho.sh
  yuppiepsycho/
    dotnet/            .NET runtime (from the zip)
    libs.aarch64/      aarch64 libraries (from the zip)
    tools/             setup scripts (from the zip)
    gamedata/
      game.dll         <- the game files go here, directly
      yuppiepsycho
      content/
      res/
      saves/
      ...
```

Copying the folder itself (`gamedata/YuppiePsycho/game.dll`) works too, the setup finds the game
one level down, but contents-only is the layout everything is tested with.

Logs: `ports/yuppiepsycho/log.txt` (game), `patchlog.txt` (setup), `inspect.txt` (written when
the setup refuses a build). Per-user settings go into `ports/yuppiepsycho/settings.txt`, see
[port/README.md](port/README.md#options).

## Testing

| Test | What it proves | Needs |
|--|--|--|
| `python3 -m unittest discover -s tests -p 'test_*.py'` | bundle parser (incl. path-traversal and range checks), deps.json/dllmap rewriting | Python 3 |
| `tests/run_tests.sh` | publishes a MonoGame + FMOD wrapper + Steamworks.NET + Theorafile-binding stand-in game in seven packagings (compressed single-file, net8 single-file, self-contained ReadyToRun, framework-dependent, Windows single-file, **self-contained .NET 8 with shipped x86_64 libraries like Yuppie Psycho**, NativeAOT), then inspect -> prepare -> **run on .NET 8** (incl. decoding a Theora video through the C# binding) -> status -> restore (byte-identical) for each; NativeAOT must be refused; Steam stub in both modes; update detection; game folder copied as a whole; the runtime assembled from NuGet packages (linux-x64 counterpart of the bundled linux-arm64 one) runs the game | .NET 8 SDK, zig, ffmpeg |
| `tests/launcher_sim.sh` | runs the real `YuppiePsycho.sh` on x86_64 inside a fake PortMaster install: first-launch setup with library replacement, second launch without setup, Steam stub, video | .NET 8 SDK |

Current result: 68/68 end-to-end checks and 12/12 unit tests pass.

What the tests cannot cover is listed in [docs/TECHNICAL.md](docs/TECHNICAL.md#open-questions):
rendering on the Mali-G31 GPU, memory on a 1 GB device, video decoding speed and the 640x480
screen.

## Legal

The game is not included; you need your own copy. The port's own code is MIT licensed
([LICENSE](LICENSE)). Third-party components and their licenses are listed in the package's
`licenses/NOTICE.txt`. Not affiliated with Baroque Decay, Valve or Firelight Technologies.
