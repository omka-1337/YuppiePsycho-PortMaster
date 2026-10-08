#!/bin/bash
# End-to-end tests for port/yuppiepsycho/tools/ypport.py.
#
# Publishes tests/testgame in the packaging variants a commercial MonoGame
# game can ship with, then for each one: inspect -> prepare -> run the result
# with the framework-dependent host -> status -> restore -> byte-compare with
# the original. Needs a .NET 8 SDK on PATH (net6.0 targets are fetched from
# NuGet) and network access to nuget.org. With zig (pip install ziglang) the
# Steam stub and Theorafile are built and exercised as well; with ffmpeg a
# Theora video is generated and decoded through the C# binding.
#
# Everything runs on an x86_64 PC, so the device scenario is tested mirrored:
# builds published for linux-arm64 (aarch64 runtime, aarch64 libraries,
# Machine=ARM64 assemblies, ReadyToRun arm64 code) are ported to x86_64 with
# the same code that ports the x86_64 game to aarch64 on the handheld.
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

# The test host is the "device": prepare targets x86_64 here.
NATIVES="$WORK/natives/x86_64"
FOREIGN="$WORK/natives/aarch64"
PREP_ARGS=(--framework-dir "$FWDIR" --native-arch x86_64)
HAVE_NATIVES=0
echo "== native helpers"
if "$REPO/src/steamstub/build.sh" "$WORK/natives" x86_64 aarch64 > "$WORK/natives.log" 2>&1 &&
   THEORAFILE_SRC="$WORK/theorafile-src" "$REPO/src/theorafile/build.sh" "$WORK/natives" x86_64 aarch64 \
     >> "$WORK/natives.log" 2>&1; then
  PREP_ARGS+=(--native-dir "$NATIVES")
  HAVE_NATIVES=1
  ok "Steam stub and Theorafile built for x86_64 and aarch64"
else
  echo "  SKIP: cannot build native helpers (needs zig: pip install ziglang), see $WORK/natives.log"
fi
VIDEO=""
if command -v ffmpeg > /dev/null && [ $HAVE_NATIVES = 1 ]; then
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

roundtrip() { # name, expected-runtime-kind, foreign ELF machine (0x3e x86_64, 0xb7 aarch64)
  local name="$1" kind="$2" foreign="$3" dir="$WORK/run/$1"
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
t = r["theorafile"]
assert t["videoinfo_params"] == 5, t
assert t["binding"].get("delegate d_tf_videoinfo") == 5, t["binding"]
assert t["binding"].get("function-pointer field tf_videoinfo_ptr") == 5, t["binding"]
EOF

  python3 "$YPPORT" prepare "$dir" "${PREP_ARGS[@]}" > "$WORK/run/$name.prepare.log" 2>&1 \
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
  python3 - "$dir" "$foreign" <<'EOF' && ok "no foreign-architecture ELF left" || bad "foreign ELF files left in the game folder"
import json, os, sys
root, foreign = sys.argv[1], int(sys.argv[2], 16)
created = set(json.load(open(os.path.join(root, ".ypport", "state.json")))["created"])
for dp, dn, fn in os.walk(root):
    if ".ypport" in dn:
        dn.remove(".ypport")
    for f in fn:
        rel = os.path.relpath(os.path.join(dp, f), root)
        with open(os.path.join(dp, f), "rb") as fh:
            head = fh.read(20)
        if head[:4] == b"\x7fELF" and head[18] == foreign and rel not in created:
            sys.exit("left: " + rel)
EOF
  python3 "$YPPORT" status "$dir" && ok "status: prepared" || bad "status after prepare"
  python3 "$YPPORT" prepare "$dir" > /dev/null 2>&1 && bad "second prepare accepted" || ok "second prepare refused"
  python3 "$YPPORT" restore "$dir" > /dev/null && diff -r "$WORK/out/$name" "$dir" > /dev/null \
    && ok "restore is byte-identical" || bad "restore differs"
}

# --- builds for the host (x86_64) -------------------------------------------
LX="-p:RuntimeIdentifier=linux-x64 -p:SelfContained=true"
publish sf-compressed  $LX -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
                       -p:EnableCompressionInSingleFile=true
publish sf-net8        $LX -p:TargetFramework=net8.0 -p:PublishSingleFile=true
publish sc-r2r         $LX -p:PublishReadyToRun=true
publish fd-portable    -p:SelfContained=false
publish win-sf         -p:RuntimeIdentifier=win-x64 -p:SelfContained=true -p:PublishSingleFile=true \
                       -p:IncludeNativeLibrariesForSelfExtract=true
publish sc-net8        $LX -p:TargetFramework=net8.0

# --- builds for aarch64, i.e. the device situation mirrored ------------------
# Same shape as the Steam Linux build of Yuppie Psycho (self-contained .NET 8,
# loose files, libtheorafile.so and libsteam_api.so next to the game), only
# for the other CPU: aarch64 runtime and libraries, Machine=ARM64 assemblies.
LA="-p:RuntimeIdentifier=linux-arm64 -p:SelfContained=true -p:TargetFramework=net8.0"
if [ ! -d "$WORK/out/arm64-sc" ] && publish arm64-sc $LA && [ $HAVE_NATIVES = 1 ]; then
  cp "$FOREIGN/libtheorafile.so" "$FOREIGN/libsteam_api.so" "$WORK/out/arm64-sc/"
fi
publish arm64-r2r $LA -p:PublishReadyToRun=true

roundtrip sf-compressed dotnet 0xb7
roundtrip sf-net8       dotnet 0xb7
roundtrip sc-r2r        dotnet 0xb7
roundtrip fd-portable   dotnet 0xb7
roundtrip win-sf        dotnet 0xb7
roundtrip sc-net8       dotnet 0xb7
roundtrip arm64-sc      dotnet 0xb7
roundtrip arm64-r2r     dotnet 0xb7

python3 - "$WORK/run/arm64-sc.prepare.log" "$HAVE_NATIVES" <<'EOF' && ok "arm64-sc: foreign runtime parked, assemblies retargeted, libraries replaced" || bad "arm64-sc prepare details"
import sys
log, natives = open(sys.argv[1]).read(), sys.argv[2] == "1"
for f in ("libcoreclr.so", "libhostfxr.so", "libSystem.Native.so"):
    assert "park " + f in log, f
assert "patch TestGame.dll" in log and "machine aarch64 -> x86_64" in log
if natives:
    for lib in ("libtheorafile.so", "libsteam_api.so"):
        assert "park " + lib in log and "install " + lib in log, lib
EOF
grep -q "patch TestGame.dll .*ReadyToRun code for linux-aarch64 detached" "$WORK/run/arm64-r2r.prepare.log" \
  && ok "arm64-r2r: ReadyToRun code for the other CPU detached" || bad "arm64-r2r: ReadyToRun not handled"

# --- Steam stub in both modes ------------------------------------------------
echo "== steam stub"
if [ $HAVE_NATIVES = 1 ]; then
  for v in sf-compressed win-sf; do
    dir="$WORK/run/stub-$v"; rm -rf "$dir"; cp -a "$WORK/out/$v" "$dir"
    python3 "$YPPORT" prepare "$dir" "${PREP_ARGS[@]}" > /dev/null 2>&1
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

# --- a copied-over game must invalidate the prepared state ---------------------
echo "== update detection"
dir="$WORK/run/update"; rm -rf "$dir"; cp -a "$WORK/out/arm64-sc" "$dir"
python3 "$YPPORT" prepare "$dir" "${PREP_ARGS[@]}" > /dev/null 2>&1
python3 "$YPPORT" status "$dir" && ok "status: prepared" || bad "status after prepare"
cp -a "$WORK/out/arm64-sc/." "$dir/"
python3 "$YPPORT" status "$dir" && bad "status ignores re-copied game" || ok "status notices re-copied game"
python3 "$YPPORT" reset "$dir" > /dev/null
python3 "$YPPORT" prepare "$dir" "${PREP_ARGS[@]}" > /dev/null 2>&1 \
  && ok "prepare after reset" || bad "prepare after reset"
if [ $HAVE_NATIVES = 1 ]; then
  touch -d '2001-01-01' "$dir/libtheorafile.so"
  python3 "$YPPORT" status "$dir" && bad "status ignores changed replacement" \
    || ok "status notices a changed replacement library"
fi

# A folder prepared by the previous release (0.1.0, before IL retargeting) is
# reported as outdated, restored byte for byte and prepared again.
old_tool="$WORK/ypport-0.1.0.py"
if git -C "$REPO" show d75a0d7:port/yuppiepsycho/tools/ypport.py > "$old_tool" 2>/dev/null; then
  dir="$WORK/run/upgrade"; rm -rf "$dir"; cp -a "$WORK/out/sc-net8" "$dir"
  python3 "$old_tool" prepare "$dir" "${PREP_ARGS[@]}" > /dev/null 2>&1
  python3 "$YPPORT" status "$dir"; st=$?
  [ $st -eq 2 ] && ok "status reports a setup made by 0.1.0 as outdated" || bad "outdated status ($st)"
  python3 "$YPPORT" restore "$dir" > /dev/null && diff -r "$WORK/out/sc-net8" "$dir" > /dev/null \
    && ok "0.1.0 setup restored byte-identically" || bad "restore of 0.1.0 setup"
  python3 "$YPPORT" prepare "$dir" "${PREP_ARGS[@]}" > /dev/null 2>&1 && python3 "$YPPORT" status "$dir" \
    && ok "prepared again by the current version" || bad "re-prepare after upgrade"
else
  echo "  SKIP: git history with release 0.1.0 not available"
fi

# --- users may copy the game folder itself instead of its contents --------------
echo "== game folder copied as a whole"
dir="$WORK/run/nested"; rm -rf "$dir"; mkdir -p "$dir"; cp -a "$WORK/out/arm64-sc" "$dir/YuppiePsycho"
if python3 "$YPPORT" prepare "$dir" "${PREP_ARGS[@]}" > "$WORK/run/nested.prepare.log" 2>&1; then
  ( . "$dir/.ypport/launch.env"; [ "$YP_APPDIR" = YuppiePsycho ] || exit 1
    cd "$dir/$YP_APPDIR" && LD_LIBRARY_PATH="$NATIVES" "$DOTNET" "$(basename "$YP_ENTRY")" --smoke ) \
    > "$WORK/run/nested.smoke.log" 2>&1
  grep -q "SMOKE OK" "$WORK/run/nested.smoke.log" && ok "nested game folder prepares and runs" \
    || bad "nested game folder run"
else
  bad "nested game folder prepare (see $WORK/run/nested.prepare.log)"
fi

# --- the runtime the package bundles is assembled from NuGet packages ------------
# Do the same for linux-x64 and run the stand-in game on it.
echo "== runtime assembled from NuGet"
RT_VERSION=8.0.31
rt_ok=1
packs=()
for pack in "microsoft.netcore.app.runtime.linux-x64 27a891335f180d6732c5a53b4847790753b6efaa1bc7c3f814013aa968d6aa36" \
            "runtime.linux-x64.microsoft.netcore.dotnethost fa6a20c74bb0bbd21386a81034a0682ca5671fe2dce9818b4402c9aa0a287d14"; do
  read -r id sum <<< "$pack"
  f="$WORK/$id.$RT_VERSION.nupkg"
  [ -f "$f" ] || curl -fsSL --retry 4 -o "$f" "https://api.nuget.org/v3-flatcontainer/$id/$RT_VERSION/$id.$RT_VERSION.nupkg" || rt_ok=0
  echo "$sum  $f" | sha256sum -c --quiet - || rt_ok=0
  packs+=("$f")
done
if [ $rt_ok = 1 ] && python3 "$REPO/src/dotnet/assemble_runtime.py" "${packs[@]}" "$WORK/dotnet-x64" > /dev/null; then
  dir="$WORK/run/nuget-runtime"; rm -rf "$dir"; cp -a "$WORK/out/arm64-sc" "$dir"
  python3 "$YPPORT" prepare "$dir" --framework-dir "$WORK/dotnet-x64/shared/Microsoft.NETCore.App/$RT_VERSION" \
    --native-arch x86_64 > /dev/null 2>&1
  (cd "$dir" && env -u DOTNET_ROOT DOTNET_ROOT="$WORK/dotnet-x64" LD_LIBRARY_PATH="$NATIVES" \
     "$WORK/dotnet-x64/dotnet" TestGame.dll --smoke) > "$WORK/run/nuget-runtime.log" 2>&1
  grep -q "runtime: .NET $RT_VERSION" "$WORK/run/nuget-runtime.log" && grep -q "SMOKE OK" "$WORK/run/nuget-runtime.log" \
    && ok "game runs on the NuGet-assembled .NET $RT_VERSION" || bad "NuGet-assembled runtime run"
else
  bad "assembling the runtime from NuGet"
fi

# --- NativeAOT builds carry no IL; they must be recognised and refused ----------
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
