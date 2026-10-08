#!/usr/bin/env python3
"""Lay out a .NET runtime in the standard DOTNET_ROOT structure from the
official NuGet packages, so the port can carry its own runtime.

    assemble_runtime.py RUNTIME_PACK.nupkg HOST_PACK.nupkg OUTDIR

RUNTIME_PACK is Microsoft.NETCore.App.Runtime.<rid> and HOST_PACK is
runtime.<rid>.Microsoft.NETCore.DotNetHost (the `dotnet` muxer), both of the
same version. The result is what Microsoft's dotnet-runtime-<ver>-<rid>.tar.gz
installs:

    OUTDIR/dotnet
    OUTDIR/host/fxr/<ver>/libhostfxr.so
    OUTDIR/shared/Microsoft.NETCore.App/<ver>/...
"""
import os
import re
import shutil
import stat
import sys
import zipfile


def nuspec_id_version(z):
    name = next(n for n in z.namelist() if n.endswith(".nuspec") and "/" not in n)
    text = z.read(name).decode("utf-8")
    pid = re.search(r"<id>([^<]+)</id>", text).group(1)
    ver = re.search(r"<version>([^<]+)</version>", text).group(1)
    return pid, ver


def extract(z, member, dest, executable=False):
    if not os.path.isdir(os.path.dirname(dest)):
        os.makedirs(os.path.dirname(dest))
    with z.open(member) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    mode = 0o755 if executable else 0o644
    os.chmod(dest, mode)


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    runtime_pack, host_pack, out = sys.argv[1:]
    with zipfile.ZipFile(runtime_pack) as rz, zipfile.ZipFile(host_pack) as hz:
        rid_id, version = nuspec_id_version(rz)
        host_id, host_version = nuspec_id_version(hz)
        m = re.match(r"Microsoft\.NETCore\.App\.Runtime\.(.+)$", rid_id, re.I)
        if not m:
            sys.exit("%s is not a .NET runtime pack" % rid_id)
        rid = m.group(1)
        if host_id.lower() != ("runtime.%s.Microsoft.NETCore.DotNetHost" % rid).lower():
            sys.exit("%s does not match runtime pack %s" % (host_id, rid_id))
        if host_version != version:
            sys.exit("version mismatch: %s vs %s" % (version, host_version))

        if os.path.exists(out):
            shutil.rmtree(out)
        shared = os.path.join(out, "shared", "Microsoft.NETCore.App", version)
        fxr = os.path.join(out, "host", "fxr", version)
        count = 0
        for member in rz.namelist():
            parts = member.split("/")
            if len(parts) < 4 or parts[0] != "runtimes" or parts[1] != rid or member.endswith("/"):
                continue
            base = parts[-1]
            if parts[2] == "native":
                dest = os.path.join(fxr if base.startswith("libhostfxr") else shared, base)
                extract(rz, member, dest, executable=True)
            elif parts[2] == "lib" and len(parts) == 5:
                extract(rz, member, os.path.join(shared, base))
            else:
                continue
            count += 1
        for name, dest in (("LICENSE.TXT", "LICENSE.txt"), ("THIRD-PARTY-NOTICES.TXT", "ThirdPartyNotices.txt")):
            if name in rz.namelist():
                extract(rz, name, os.path.join(out, dest))

        muxer = "runtimes/%s/native/dotnet" % rid
        if muxer not in hz.namelist():
            sys.exit("%s has no %s" % (host_id, muxer))
        extract(hz, muxer, os.path.join(out, "dotnet"), executable=True)

    if not os.path.exists(os.path.join(fxr, "libhostfxr.so")) or \
            not os.path.exists(os.path.join(shared, "System.Private.CoreLib.dll")):
        sys.exit("incomplete runtime pack")
    print("assembled .NET %s (%s): %d files in %s" % (version, rid, count + 1, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
