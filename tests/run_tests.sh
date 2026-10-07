#!/bin/bash
# End-to-end tests for port/yuppiepsycho/tools/ypport.py.
#
# Publishes tests/testgame in the packaging variants a commercial MonoGame
# game can ship with, then for each one: inspect -> prepare -> run the result
# with the framework-dependent host -> status -> restore -> byte-compare with
# the original. Needs a .NET 8 SDK on PATH (net6.0 targets are fetched from
# NuGet) and network access to nuget.org. With zig (pip install ziglang) the
# Steam stub and Theorafile are built for the host and exercised as well; with
# ffmpeg a Theora video is generated and decoded through the C# binding.
#
#   tests/run_tests.sh [workdir]

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
YPPORT="$REPO/port/yuppiepsycho/tools/ypport.py"
WORK="${1:-$(mktemp -d)}"
DOTNET="${DOTNET:-dotnet}"
DOTNET_ROOT="${DOTNET_ROOT:-$(dirname "$(readlink -f "$(command -v "$DOTNET")")")}"
export DOTNET_ROOT DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
FWDIR="$(ls -d "$DOTNET_ROOT"/shared/Microsoft.NETCore.App/8.* | sort -V | tail -1)"

pass=0
fail=0
ok()  { echo "  PASS: $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL: $*"; fail=$((fail + 1)); }

mkdir -p "$WORK/src" "$WORK/out" "$WORK/run"
cp -r "$HERE/testgame/." "$WORK/src/"

# Host builds of the port's native pieces. On the device these are aarch64;
# here they stand in as "replacements" for the libraries a game ships.
NATIVES="$WORK/natives/x86_64"
NATIVE_ARGS=()
echo "== native helpers"
if "$REPO/src/steamstub/build.sh" "$WORK/natives" x86_64 > "$WORK/natives.log" 2>&1 &&
   THEORAFILE_SRC="$WORK/theorafile-src" "$REPO/src/theorafile/build.sh" "$WORK/natives" x86_64 \
     >> "$WORK/natives.log" 2>&1; then
  NATIVE_ARGS=(--native-dir "$NATIVES" --native-arch x86_64)
  ok "Steam stub and Theorafile built for the host"
else
  echo "  SKIP: cannot build native helpers (needs zig: pip install ziglang), see $WORK/natives.log"
fi
VIDEO=""
if command -v ffmpeg > /dev/null && [ ${#NATIVE_ARGS[@]} -gt 0 ]; then
  VIDEO="$WORK/test.ogv"
  ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x180:rate=30 -f lavfi -i sine=frequency=440 \
    -t 2 -pix_fmt yuv420p -c:v libtheora -c:a libvorbis "$VIDEO" || VIDEO=""
fi

publish() { # name, extra args...
  local name="$1"; shift
  if [ ! -d "$WORK/out/$name" ]; then
    echo "== publishing $name"
    rm -rf "$WORK/src/bin" "$WORK/src/obj"
    (cd "$WORK/src" && "$DOTNET" restore "$@" && \
      "$DOTNET" publish --no-restore -c Release -o "$WORK/out/$name" "$@") \
      > "$WORK/out/$name.log" 2>&1 || { bad "publish $name (see $WORK/out/$name.log)"; return 1; }
    rm -rf "$WORK/src/bin" "$WORK/src/obj"
  fi
}

roundtrip() { # name, expected-runtime-kind
  local name="$1" kind="$2" dir="$WORK/run/$1"
  echo "== $name"
  rm -rf "$dir"; cp -a "$WORK/out/$name" "$dir"

  python3 "$YPPORT" inspect --json "$dir" > "$WORK/run/$name.inspect.json"
  local rc=$?
  [ $rc -eq 0 ] && ok "inspect says portable" || bad "inspect exit $rc"
  python3 - "$WORK/run/$name.inspect.json" "$kind" <<'EOF' && ok "inspect: kind/engine/fmod/theorafile" || bad "inspect details"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["runtime_kind"] == sys.argv[2], r["runtime_kind"]
assert r["engine"]["name"] == "MonoGame.Framework" and r["engine"]["flavor"] == "DesktopGL", r["engine"]
assert r["fmod_header_version"] == "2.02.18", r["fmod_header_version"]
for key in ("fmod", "steam_api", "sdl2", "openal", "theora"):
    assert key in r["native_needs"], key
assert r["theorafile"]["videoinfo_params"] == 5, r["theorafile"]
EOF

  python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" "${NATIVE_ARGS[@]}" > "$WORK/run/$name.prepare.log" 2>&1 \
    && ok "prepare" || { bad "prepare (see $WORK/run/$name.prepare.log)"; return; }
  # shellcheck disable=SC1090
  ( . "$dir/.ypport/launch.env"
    cd "$dir/$YP_APPDIR" && YP_TEST_VIDEO="$VIDEO" LD_LIBRARY_PATH="$NATIVES" \
      "$DOTNET" "$(basename "$YP_ENTRY")" --smoke ) > "$WORK/run/$name.smoke.log" 2>&1
  if grep -q "SMOKE OK" "$WORK/run/$name.smoke.log" && grep -q "runtime: .NET 8" "$WORK/run/$name.smoke.log"; then
    ok "runs on $(grep 'runtime:' "$WORK/run/$name.smoke.log" | cut -d' ' -f2-)"
  else
    bad "smoke run:"; sed 's/^/      /' "$WORK/run/$name.smoke.log" | head -20
  fi
  if [ -n "$VIDEO" ]; then
    grep -q "video decoded: 320x180 fmt 0, 6[01] frames" "$WORK/run/$name.smoke.log" \
      && ok "Theora video decoded through the C# binding" || bad "video decode"
  fi
  # Only the replacements installed by prepare may be x86-64 here.
  python3 - "$dir" <<'EOF' && ok "no foreign x86-64 ELF left" || bad "x86-64 ELF files left in the game folder"
import json, os, sys
root = sys.argv[1]
created = set(json.load(open(os.path.join(root, ".ypport", "state.json")))["created"])
for dp, dn, fn in os.walk(root):
    if ".ypport" in dn:
        dn.remove(".ypport")
    for f in fn:
        rel = os.path.relpath(os.path.join(dp, f), root)
        with open(os.path.join(dp, f), "rb") as fh:
            head = fh.read(20)
        if head[:4] == b"\x7fELF" and head[18] == 0x3E and rel not in created:
            sys.exit("left: " + rel)
EOF
  python3 "$YPPORT" status "$dir" && ok "status: prepared" || bad "status after prepare"
  python3 "$YPPORT" prepare "$dir" > /dev/null 2>&1 && bad "second prepare accepted" || ok "second prepare refused"
  python3 "$YPPORT" restore "$dir" > /dev/null && diff -r "$WORK/out/$name" "$dir" > /dev/null \
    && ok "restore is byte-identical" || bad "restore differs"
}

LX="-p:RuntimeIdentifier=linux-x64 -p:SelfContained=true"
publish sf-compressed  $LX -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
                       -p:EnableCompressionInSingleFile=true
publish sf-net8        $LX -p:TargetFramework=net8.0 -p:PublishSingleFile=true
publish sc-r2r         $LX -p:PublishReadyToRun=true
publish fd-portable    -p:SelfContained=false
publish win-sf         -p:RuntimeIdentifier=win-x64 -p:SelfContained=true -p:PublishSingleFile=true \
                       -p:IncludeNativeLibrariesForSelfExtract=true
# Same shape as the Steam Linux build of Yuppie Psycho: self-contained .NET 8,
# loose files, x86_64 libtheorafile.so and libsteam_api.so next to the game.
if [ ! -d "$WORK/out/sc-net8" ] && publish sc-net8 $LX -p:TargetFramework=net8.0 && [ ${#NATIVE_ARGS[@]} -gt 0 ]; then
  cp "$NATIVES/libtheorafile.so" "$NATIVES/libsteam_api.so" "$WORK/out/sc-net8/"
fi

roundtrip sf-compressed dotnet
roundtrip sf-net8       dotnet
roundtrip sc-r2r        dotnet
roundtrip fd-portable   dotnet
roundtrip win-sf        dotnet
roundtrip sc-net8       dotnet
if [ ${#NATIVE_ARGS[@]} -gt 0 ]; then
  python3 - "$WORK/run/sc-net8.prepare.log" <<'EOF' && ok "sc-net8: shipped libraries replaced in place" || bad "sc-net8 replacements"
import sys
log = open(sys.argv[1]).read()
for lib in ("libtheorafile.so", "libsteam_api.so", "libSDL2-2.0.so.0"):
    assert "park " + lib in log, lib
for lib in ("libtheorafile.so", "libsteam_api.so"):
    assert "install " + lib in log, lib
assert "install libSDL2" not in log  # no replacement shipped: the system SDL2 is used
EOF
fi

# The Steam stub must satisfy Steamworks.NET in both modes.
echo "== steam stub"
if [ ${#NATIVE_ARGS[@]} -gt 0 ]; then
  for v in sf-compressed win-sf; do
    dir="$WORK/run/stub-$v"; rm -rf "$dir"; cp -a "$WORK/out/$v" "$dir"
    python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" > /dev/null 2>&1
    for mode in off fake; do
      log="$WORK/run/stub-$v-$mode.log"
      (cd "$dir" && YP_STEAM_STUB=$mode LD_LIBRARY_PATH="$NATIVES" \
        "$DOTNET" TestGame.dll --smoke) > "$log" 2>&1
      want="steam init: False"; [ $mode = fake ] && want="steam achievement: False"
      grep -q "$want" "$log" && grep -q "SMOKE OK" "$log" && ok "$v with stub ($mode)" \
        || { bad "$v with stub ($mode)"; sed 's/^/      /' "$log" | tail -8; }
    done
  done
else
  echo "  SKIP: no native helpers"
fi

# A copied-over game must invalidate the prepared state, also when only an
# installed replacement library got overwritten.
echo "== update detection"
dir="$WORK/run/update"; rm -rf "$dir"; cp -a "$WORK/out/sc-net8" "$dir"
python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" "${NATIVE_ARGS[@]}" > /dev/null 2>&1
python3 "$YPPORT" status "$dir" && ok "status: prepared" || bad "status after prepare"
cp -a "$WORK/out/sc-net8/." "$dir/"
python3 "$YPPORT" status "$dir" && bad "status ignores re-copied game" || ok "status notices re-copied game"
python3 "$YPPORT" reset "$dir" > /dev/null
python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" "${NATIVE_ARGS[@]}" > /dev/null 2>&1 \
  && ok "prepare after reset" || bad "prepare after reset"
if [ ${#NATIVE_ARGS[@]} -gt 0 ]; then
  touch -d '2001-01-01' "$dir/libtheorafile.so"
  python3 "$YPPORT" status "$dir" && bad "status ignores changed replacement" \
    || ok "status notices a changed replacement library"
fi

# NativeAOT builds carry no IL; they must be recognised and refused.
echo "== nativeaot"
if publish nativeaot $LX -p:TargetFramework=net8.0 -p:PublishAot=true; then
  dir="$WORK/run/nativeaot"; rm -rf "$dir"; cp -a "$WORK/out/nativeaot" "$dir"
  python3 "$YPPORT" inspect "$dir" > "$WORK/run/nativeaot.inspect.log"
  [ $? -eq 2 ] && grep -q "NativeAOT build" "$WORK/run/nativeaot.inspect.log" \
    && ok "NativeAOT detected and reported" || bad "NativeAOT not detected"
  python3 "$YPPORT" prepare "$dir" > /dev/null 2>&1 && bad "prepare accepted NativeAOT" \
    || ok "prepare refuses NativeAOT"
fi

echo
echo "passed: $pass, failed: $fail"
[ $fail -eq 0 ]
