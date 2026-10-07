#!/usr/bin/env python3
"""Unit tests for ypport.py helpers that need no .NET SDK.

    python3 -m unittest discover -s tests -p 'test_*.py'
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "port", "yuppiepsycho", "tools"))
import ypport  # noqa: E402


def _7bit(s):
    data = s.encode("utf-8")
    n = len(data)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + data


def make_bundle(path, files, major=6, compress=False):
    """Write a minimal apphost-like file carrying a bundle (format as written
    by Microsoft.NET.HostModel's Bundler)."""
    blob = bytearray(b"\x7fELF" + b"\0" * 60)
    blob += struct.pack("<q", 0) + ypport.BUNDLE_SIGNATURE
    marker = len(blob) - len(ypport.BUNDLE_SIGNATURE) - 8
    entries = []
    for name, data, ftype in files:
        stored = data
        csize = 0
        if compress:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            stored = c.compress(data) + c.flush()
            csize = len(stored)
        entries.append((len(blob), len(data), csize, ftype, name))
        blob += stored
    header = len(blob)
    blob += struct.pack("<IIi", major, 0, len(entries)) + _7bit("testbundle")
    if major >= 2:
        blob += struct.pack("<qqqqQ", 0, 0, 0, 0, 0)
    for off, size, csize, ftype, name in entries:
        blob += struct.pack("<qq", off, size)
        if major >= 6:
            blob += struct.pack("<q", csize)
        blob += struct.pack("<B", ftype) + _7bit(name)
    struct.pack_into("<q", blob, marker, header)
    with open(path, "wb") as f:
        f.write(blob)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_roundtrip_plain_and_compressed(self):
        for major in (1, 2, 6):
            for compress in ((False, True) if major >= 6 else (False,)):
                p = os.path.join(self.tmp, "b%d%d" % (major, compress))
                files = [("App.dll", b"MZ" + b"x" * 5000, 1),
                         ("sub/dir/data.bin", os.urandom(70000), 0),
                         ("App.runtimeconfig.json", b"{}", 4)]
                make_bundle(p, files, major=major, compress=compress)
                b = ypport.Bundle(p)
                self.assertEqual((b.major, len(b.entries)), (major, 3))
                out = os.path.join(self.tmp, "out%d%d" % (major, compress))
                os.makedirs(out)
                b.extract(out)
                for name, data, _ in files:
                    with open(os.path.join(out, *name.split("/")), "rb") as f:
                        self.assertEqual(f.read(), data)

    def test_rejects_path_traversal(self):
        for evil in ("../escape.dll", "/etc/passwd", "a/../../b", "C:\\Windows\\x.dll", "..\\x"):
            p = os.path.join(self.tmp, "evil")
            make_bundle(p, [(evil, b"boom", 0)])
            with self.assertRaises(ypport.BundleError):
                ypport.Bundle(p)

    def test_rejects_out_of_range_entry(self):
        p = os.path.join(self.tmp, "range")
        make_bundle(p, [("a.dll", b"abc", 0)])
        with open(p, "r+b") as f:
            data = bytearray(f.read())
            i = data.rfind(_7bit("a.dll"))
            struct.pack_into("<q", data, i - 1 - 8 - 8, 10 ** 9)  # size field
            f.seek(0)
            f.write(data)
        with self.assertRaises(ypport.BundleError):
            ypport.Bundle(p)

    def test_non_bundle_apphost(self):
        p = os.path.join(self.tmp, "host")
        with open(p, "wb") as f:
            f.write(b"\x7fELF" + b"\0" * 100 + struct.pack("<q", 0) + ypport.BUNDLE_SIGNATURE + b"\0" * 100)
        self.assertEqual(ypport.find_bundle_header(p), 0)
        with self.assertRaises(ypport.BundleError):
            ypport.Bundle(p)


class SniffTests(unittest.TestCase):
    def test_java_class_is_not_macho(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 100)
        try:
            self.assertEqual(ypport.sniff(f.name), (None, None))
        finally:
            os.remove(f.name)

    def test_elf_arch(self):
        for machine, arch in ((0x3E, "x86_64"), (0xB7, "aarch64")):
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(b"\x7fELF\x02\x01" + b"\0" * 12 + struct.pack("<H", machine) + b"\0" * 40)
            try:
                self.assertEqual(ypport.sniff(f.name), ("elf", arch))
            finally:
                os.remove(f.name)


class RewriteTests(unittest.TestCase):
    def test_unpin_dllmaps(self):
        src = ('<configuration>\n'
               '<dllmap dll="SDL2.dll" os="linux" cpu="x86-64" target="./lib64/libSDL2-2.0.so.0"/>\n'
               '<dllmap dll="fmod" target="lib64/libfmod.so.13"/>\n'
               '</configuration>')
        out = ypport.unpin_dllmaps(src)
        self.assertIn('<dllmap dll="SDL2.dll" os="linux" target="libSDL2-2.0.so.0"/>', out)
        self.assertIn('<dllmap dll="fmod" target="libfmod.so.13"/>', out)

    def test_rewrite_deps(self):
        deps = {
            "runtimeTarget": {"name": ".NETCoreApp,Version=v6.0/linux-x64"},
            "targets": {".NETCoreApp,Version=v6.0/linux-x64": {
                "Game/1.0.0": {"dependencies": {"MonoGame.Framework.DesktopGL": "3.8.1.303"},
                               "runtime": {"Game.dll": {}}},
                "MonoGame.Framework.DesktopGL/3.8.1.303": {
                    "runtime": {"lib/net6.0/MonoGame.Framework.dll": {}},
                    "native": {"runtimes/linux-x64/native/libSDL2-2.0.so.0": {},
                               "runtimes/linux-x64/native/libopenal.so.1": {}}},
                "runtimepack.Microsoft.NETCore.App.Runtime.linux-x64/6.0.36": {
                    "runtime": {"System.Private.CoreLib.dll": {}},
                    "native": {"libcoreclr.so": {}}},
            }},
            "libraries": {"Game/1.0.0": {}, "MonoGame.Framework.DesktopGL/3.8.1.303": {},
                          "runtimepack.Microsoft.NETCore.App.Runtime.linux-x64/6.0.36": {}},
        }
        self.assertEqual(ypport.runtime_pack_files(deps), {"System.Private.CoreLib.dll", "libcoreclr.so"})
        new = ypport.rewrite_deps(deps, {"libSDL2-2.0.so.0", "libopenal.so.1"})
        t = new["targets"][".NETCoreApp,Version=v6.0/linux-x64"]
        self.assertNotIn("runtimepack.Microsoft.NETCore.App.Runtime.linux-x64/6.0.36", t)
        self.assertNotIn("native", t["MonoGame.Framework.DesktopGL/3.8.1.303"])
        self.assertIn("lib/net6.0/MonoGame.Framework.dll", t["MonoGame.Framework.DesktopGL/3.8.1.303"]["runtime"])
        self.assertEqual(sorted(new["libraries"]), ["Game/1.0.0", "MonoGame.Framework.DesktopGL/3.8.1.303"])
        # the input is left untouched
        self.assertIn("native", deps["targets"][".NETCoreApp,Version=v6.0/linux-x64"]
                      ["MonoGame.Framework.DesktopGL/3.8.1.303"])

    def test_fmod_version_str(self):
        self.assertEqual(ypport.fmod_version_str(0x00020218), "2.02.18")
        self.assertEqual(ypport.fmod_version_str(0x00020306), "2.03.06")


if __name__ == "__main__":
    unittest.main()
