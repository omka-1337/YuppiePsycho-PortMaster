#!/bin/bash
# End-to-end tests for port/yuppiepsycho/tools/ypport.py.
#
# Publishes tests/testgame in the packaging variants a commercial MonoGame
# game can ship with, then for each one: inspect -> prepare -> run the result
# with the framework-dependent host -> status -> restore -> byte-compare with
# the original. Needs a .NET 8 SDK on PATH (net6.0 targets are fetched from
# NuGet) and network access to nuget.org.
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
  python3 - "$WORK/run/$name.inspect.json" "$kind" <<'EOF' && ok "inspect: kind/engine/fmod" || bad "inspect details"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["runtime_kind"] == sys.argv[2], r["runtime_kind"]
assert r["engine"]["name"] == "MonoGame.Framework" and r["engine"]["flavor"] == "DesktopGL", r["engine"]
assert r["fmod_header_version"] == "2.02.18", r["fmod_header_version"]
for key in ("fmod", "steam_api", "sdl2", "openal"):
    assert key in r["native_needs"], key
EOF

  python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" > "$WORK/run/$name.prepare.log" 2>&1 \
    && ok "prepare" || { bad "prepare (see $WORK/run/$name.prepare.log)"; return; }
  # shellcheck disable=SC1090
  ( . "$dir/.ypport/launch.env"
    cd "$dir/$YP_APPDIR" && "$DOTNET" "$(basename "$YP_ENTRY")" --smoke ) > "$WORK/run/$name.smoke.log" 2>&1
  if grep -q "SMOKE OK" "$WORK/run/$name.smoke.log" && grep -q "runtime: .NET 8" "$WORK/run/$name.smoke.log"; then
    ok "runs on $(grep 'runtime:' "$WORK/run/$name.smoke.log" | cut -d' ' -f2-)"
  else
    bad "smoke run:"; sed 's/^/      /' "$WORK/run/$name.smoke.log" | head -20
  fi
  if find "$dir" -path "$dir/.ypport" -prune -o -type f -print0 | xargs -0 -r file | grep -q 'ELF.*x86-64'; then
    bad "x86-64 ELF files left in the game folder"
  else
    ok "no x86-64 ELF left"
  fi
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

roundtrip sf-compressed dotnet
roundtrip sf-net8       dotnet
roundtrip sc-r2r        dotnet
roundtrip fd-portable   dotnet
roundtrip win-sf        dotnet

# The Steam stub, built for the host, must satisfy Steamworks.NET in both modes.
echo "== steam stub"
if "$REPO/src/steamstub/build.sh" "$WORK/stub" x86_64 > "$WORK/stub.log" 2>&1; then
  for v in sf-compressed win-sf; do
    dir="$WORK/run/stub-$v"; rm -rf "$dir"; cp -a "$WORK/out/$v" "$dir"
    python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" > /dev/null 2>&1
    for mode in off fake; do
      log="$WORK/run/stub-$v-$mode.log"
      (cd "$dir" && YP_STEAM_STUB=$mode LD_LIBRARY_PATH="$WORK/stub/x86_64" \
        "$DOTNET" TestGame.dll --smoke) > "$log" 2>&1
      want="steam init: False"; [ $mode = fake ] && want="steam achievement: False"
      grep -q "$want" "$log" && grep -q "SMOKE OK" "$log" && ok "$v with stub ($mode)" \
        || { bad "$v with stub ($mode)"; sed 's/^/      /' "$log" | tail -8; }
    done
  done
else
  echo "  SKIP: cannot build the stub (needs zig: pip install ziglang)"
fi

# A copied-over game update must invalidate the prepared state.
echo "== update detection"
dir="$WORK/run/update"; rm -rf "$dir"; cp -a "$WORK/out/sf-compressed" "$dir"
python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" > /dev/null 2>&1
cp -a "$WORK/out/sf-compressed/." "$dir/"
python3 "$YPPORT" status "$dir" && bad "status ignores re-copied game" || ok "status notices re-copied game"
python3 "$YPPORT" reset "$dir" > /dev/null
python3 "$YPPORT" prepare "$dir" --framework-dir "$FWDIR" > /dev/null 2>&1 && ok "prepare after reset" \
  || bad "prepare after reset"

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
