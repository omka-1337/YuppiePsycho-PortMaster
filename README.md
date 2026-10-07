# Yuppie Psycho for PortMaster

A **native** aarch64 port of *Yuppie Psycho: Executive Edition* for Linux handhelds running
[PortMaster](https://portmaster.games) (developed on an Anbernic RG40XX H with ROCKNIX). No x86
emulation: the game's own code runs on an aarch64 .NET runtime.

> **Status: experimental, not yet run against the real game.** Every piece is tested against real
> .NET SDK output and a MonoGame stand-in game (see [Testing](#testing)), but the actual Yuppie
> Psycho files and the device have not been in the loop yet. The first step is to run the
> inspector on your copy of the game, see [Step 1](#step-1-inspect-your-copy-of-the-game).

## Why a native port is possible

Since v2.5 (October 2022) the PC version runs on the same engine as the console versions:
MonoGame on .NET, with FMOD for audio and Steamworks for achievements. The game logic is
therefore CIL bytecode, which is CPU independent. What ties the PC release to x86_64 is only the
*packaging*:

| x86_64-only piece | What the port does |
|--|--|
| .NET apphost / single-file bundle (the executable) | extracts the IL assemblies from the bundle |
| Bundled .NET runtime (`libcoreclr.so`, `System.Private.CoreLib.dll`, ...) | sets it aside, uses PortMaster's aarch64 .NET 8 runtime instead |
| `runtimeconfig.json` / `deps.json` describing a self-contained x86_64 app | rewrites them for a framework-dependent app with roll-forward |
| `libSDL2`, `libopenal` | uses aarch64 builds (system SDL2, bundled OpenAL Soft) |
| `libfmod`, `libfmodstudio` | bundles aarch64 FMOD 2.02.18 (user-replaceable) |
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

```
pip install ziglang     # cross compiler for the aarch64 Steam stub
./build.sh              # -> dist/yuppiepsycho.zip
```

`build.sh` compiles the Steam stub and downloads the aarch64 gl4es, OpenAL Soft and FMOD
binaries from [PortMaster-New](https://github.com/PortsMaster/PortMaster-New) at a pinned commit,
verifying their SHA-256 sums.

## Step 3: install on the device

1. In PortMaster, install the **dotnet-8.0.12** runtime (Options / Runtimes). It is also pulled
   in automatically when the port is installed through PortMaster.
2. Extract `dist/yuppiepsycho.zip` into the ports folder (`/storage/roms/ports` on ROCKNIX, or
   `roms/ports` on the SD card), giving `ports/YuppiePsycho.sh` and `ports/yuppiepsycho/`.
3. Copy the **complete** game folder (Steam Linux build preferred, Windows build also fine if it
   is DesktopGL) into `ports/yuppiepsycho/gamedata/`.
4. Launch *Yuppie Psycho* from Ports. The first launch runs the setup (about a minute), later
   launches start the game directly.

Logs: `ports/yuppiepsycho/log.txt` (game), `patchlog.txt` (setup), `inspect.txt` (written when
the setup refuses a build). Per-user settings go into `ports/yuppiepsycho/settings.txt`, see
[port/README.md](port/README.md#options).

## Testing

| Test | What it proves | Needs |
|--|--|--|
| `python3 -m unittest discover -s tests -p 'test_*.py'` | bundle parser (incl. path-traversal and range checks), deps.json/dllmap rewriting | Python 3 |
| `tests/run_tests.sh` | publishes a MonoGame 3.8.1 + FMOD wrapper + Steamworks.NET stand-in game in six packagings (compressed single-file, net8 single-file, self-contained ReadyToRun, framework-dependent, Windows single-file, NativeAOT), then inspect -> prepare -> **run on .NET 8** -> status -> restore (byte-identical) for each; NativeAOT must be refused; Steam stub in both modes | .NET 8 SDK, zig |
| `tests/launcher_sim.sh` | runs the real `YuppiePsycho.sh` on x86_64 inside a fake PortMaster install: first-launch setup, second launch without setup, library paths, Steam stub | .NET 8 SDK |

Current result: 48/48 end-to-end checks and 9/9 unit tests pass.

What the tests cannot cover is listed in [docs/TECHNICAL.md](docs/TECHNICAL.md#open-questions):
the real game's packaging, rendering on the Mali-G31 GPU, memory on a 1 GB device, video
playback and the 640x480 screen.

## Legal

The game is not included; you need your own copy. The port's own code is MIT licensed
([LICENSE](LICENSE)). Third-party components and their licenses are listed in the package's
`licenses/NOTICE.txt`. Not affiliated with Baroque Decay, Valve or Firelight Technologies.
