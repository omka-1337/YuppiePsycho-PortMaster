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


class ElfTests(unittest.TestCase):
    LIBC = "/lib/x86_64-linux-gnu/libc.so.6"

    @unittest.skipUnless(os.path.exists(LIBC), "needs an x86_64 glibc")
    def test_elf_exports(self):
        names = ypport.elf_exports(self.LIBC)
        self.assertIn("printf", names)
        self.assertIn("dlopen", names)

    def test_elf_exports_rejects_non_elf(self):
        self.assertIsNone(ypport.elf_exports(__file__))

    def test_theorafile_api_level(self):
        base = {"tf_fopen", "tf_videoinfo", "tf_readvideo"}
        self.assertTrue(ypport.theorafile_api_level(base).startswith("pre-2021"))
        self.assertTrue(ypport.theorafile_api_level(base | {"tf_setaudiotrack"}).startswith("2021-04"))
        self.assertTrue(ypport.theorafile_api_level(base | {"tf_setaudiotrack", "tf_setvideotrack"})
                        .startswith("2021-12"))


def _lz4_literals(data):
    """A valid LZ4 block made of one literal run (what a non-compressing
    encoder would emit)."""
    n = len(data)
    out = bytearray()
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    return bytes(out) + data


def _xnb_texture(width, height, fmt=0, levels=1, lz4=False):
    reader = b"Microsoft.Xna.Framework.Content.Texture2DReader, MonoGame.Framework"
    payload = bytes([1, len(reader)]) + reader + struct.pack("<i", 0) + bytes([0, 1])
    payload += struct.pack("<iIII", fmt, width, height, levels) + b"\0" * 64
    if lz4:
        body = struct.pack("<I", len(payload)) + _lz4_literals(payload)
        return b"XNBw\x05\x40" + struct.pack("<I", 10 + len(body)) + body
    return b"XNBw\x05\x00" + struct.pack("<I", 10 + len(payload)) + payload


class TextureSurveyTests(unittest.TestCase):
    def test_lz4_with_match(self):
        block = bytes([(3 << 4) | 5]) + b"abc" + struct.pack("<H", 3)  # "abc" + copy 9 from 3 back
        self.assertEqual(ypport.lz4_block_prefix(block, 100), b"abcabcabcabc")
        self.assertEqual(ypport.lz4_block_prefix(_lz4_literals(b"x" * 300), 300), b"x" * 300)

    def test_survey(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "content", "gfx"))
        os.makedirs(os.path.join(tmp, "res"))
        png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 2048, 1024, 8, 6, 0, 0, 0)
        with open(os.path.join(tmp, "res", "atlas.png"), "wb") as f:
            f.write(png + b"\0" * 16)
        with open(os.path.join(tmp, "res", "photo.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"JF" +
                    b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 720, 1280) + b"\0" * 16)
        with open(os.path.join(tmp, "content", "gfx", "a.xnb"), "wb") as f:
            f.write(_xnb_texture(512, 256))
        with open(os.path.join(tmp, "content", "gfx", "b.xnb"), "wb") as f:
            f.write(_xnb_texture(4096, 4096, fmt=6, lz4=True))
        with open(os.path.join(tmp, "content", "music.ogg"), "wb") as f:
            f.write(b"OggS" + b"\0" * 100)
        r = ypport.survey_textures(tmp)
        self.assertEqual(r["images"], 4)
        expected = 2048 * 1024 * 4 + 1280 * 720 * 4 + 512 * 256 * 4 + 4096 * 4096 * 1
        self.assertEqual(r["decoded_bytes"], expected)
        self.assertEqual(r["largest"][0][4], "content/gfx/b.xnb")
        self.assertEqual(r["over_2048"], 1)
        self.assertEqual(r["other"]["audio"][0], 1)


class AlphaAndPreviewTests(unittest.TestCase):
    @staticmethod
    def rgba(alphas):
        return b"".join(bytes((10, 20, 30, a)) for a in alphas)

    def test_alpha_class(self):
        self.assertEqual(ypport.alpha_class(self.rgba([0, 255, 0, 255])), ("binary", 0.0))
        cls, share = ypport.alpha_class(self.rgba([0, 255, 3, 250]))
        self.assertEqual((cls, share), ("near", 0.5))
        self.assertEqual(ypport.alpha_class(self.rgba([0, 255, 128, 255]))[0], "soft")

    def test_format_policy(self):
        f = ypport.texture_format_for
        self.assertIsNone(f(["binary"], "full"))
        self.assertEqual(f(["near"], "auto16"), 2)
        self.assertIsNone(f(["soft"], "auto16"))
        self.assertEqual(f(["soft"], "16bit"), 3)
        self.assertEqual(f(["binary", "soft"], "16bit"), 3)

    def test_preview_matches_quantisation(self):
        rgba = bytes(bytearray(range(256))) * 4
        p4 = ypport.preview_16bit(rgba, 3)
        self.assertTrue(all(v % 17 == 0 for v in p4))
        self.assertTrue(all(abs(a - b) <= 8 for a, b in zip(rgba, p4)))
        p5 = ypport.preview_16bit(rgba, 2)
        self.assertTrue(all(v in (0, 255) for v in p5[3::4]))
        self.assertTrue(all(abs(a - b) <= 4 for a, b in zip(rgba[0::4], p5[0::4])))

    def test_write_png(self):
        w, h = 5, 3
        rgba = bytes(bytearray(range(w * h * 4)))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            ypport.write_png(path, w, h, rgba)
            self.assertEqual(ypport.image_size(path), (w, h))
            data = open(path, "rb").read()
            i = data.index(b"IDAT")
            n = struct.unpack(">I", data[i - 4:i])[0]
            raw = zlib.decompress(data[i + 4:i + 4 + n])
            rows = [raw[y * (w * 4 + 1) + 1:(y + 1) * (w * 4 + 1)] for y in range(h)]
            self.assertEqual(b"".join(rows), rgba)
        finally:
            os.remove(path)


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



def ogg_page(serial, packets, granule=0, bos=False):
    """One Ogg page holding whole packets (the CRC is left 0: ypport does not
    check it)."""
    lacing = bytearray()
    for pkt in packets:
        n = len(pkt)
        lacing += b"\xff" * (n // 255) + bytes([n % 255])
    return (b"OggS\0" + bytes([2 if bos else 0]) + struct.pack("<qIII", granule, serial, 0, 0) +
            bytes([len(lacing)]) + bytes(lacing) + b"".join(packets))


def theora_ident(width, height, fps=(30, 1), fmt=0, shift=6, vrev=1):
    fmbw, fmbh = (width + 15) // 16, (height + 15) // 16
    bits = (48 << 10) | (shift << 5) | (fmt << 3)
    return (b"\x80theora" + bytes([3, 2, vrev]) + struct.pack(">HH", fmbw, fmbh) +
            width.to_bytes(3, "big") + height.to_bytes(3, "big") + bytes([0, 0]) +
            struct.pack(">II", *fps) + (1).to_bytes(3, "big") + (1).to_bytes(3, "big") + b"\0" +
            (0).to_bytes(3, "big") + struct.pack(">H", bits))


class VideoTests(unittest.TestCase):
    def write_ogv(self, path, last_granule, vrev=1):
        vorbis = b"\x01vorbis" + struct.pack("<IB", 0, 2) + struct.pack("<I", 48000) + b"\0" * 14
        data = (ogg_page(7, [theora_ident(1920, 1080, vrev=vrev)], bos=True) +
                ogg_page(9, [vorbis], bos=True) +
                ogg_page(7, [b"\x81theora", b"\x82theora"]) +
                ogg_page(7, [b"\0" * 600], granule=last_granule))
        with open(path, "wb") as f:
            f.write(data)

    def test_ogg_video_info(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "intro.ogv")
            # keyframe 64 plus 9 frames: 73 frames (3.2.1 granules count from 1)
            self.write_ogv(path, (64 << 6) | 9)
            v = ypport.ogg_video_info(path)
            self.assertEqual((v["width"], v["height"], v["frame_height"]), (1920, 1080, 1088))
            self.assertEqual((v["fps"], v["pixel_format"], v["frames"]), (30.0, "420", 73))
            self.assertAlmostEqual(v["duration"], 73 / 30.0)
            self.assertEqual(v["audio"], [{"channels": 2, "rate": 48000}])
            self.write_ogv(path, (64 << 6) | 9, vrev=0)
            self.assertEqual(ypport.ogg_video_info(path)["frames"], 74)
            with open(os.path.join(d, "music.ogg"), "wb") as f:
                f.write(ogg_page(3, [b"\x01vorbis" + b"\0" * 23], bos=True))
            self.assertIsNone(ypport.ogg_video_info(os.path.join(d, "music.ogg")))
            self.assertEqual([v["path"] for v in ypport.find_videos(d)], ["intro.ogv"])

    def test_video_fit(self):
        self.assertEqual(ypport.video_fit(1920, 1080, "640x480"), (640, 360))
        self.assertEqual(ypport.video_fit(1440, 1080, "640x480"), (640, 480))
        self.assertEqual(ypport.video_fit(1366, 768, "960x540"), (960, 540))
        self.assertIsNone(ypport.video_fit(640, 360, "640x480"))
        self.assertIsNone(ypport.video_fit(680, 384, "640x480"))  # shrinks less than 10%
        self.assertEqual(ypport.video_mode("854x480"), "854x480")
        self.assertRaises(Exception, ypport.video_mode, "640")

    def test_xnb_video_info(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import make_video_xnb
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "intro.xnb")
            with open(path, "wb") as f:
                f.write(make_video_xnb.video_xnb("Video\\intro.ogv", 1280, 720, 29.97))
            x = ypport.XnbFile(path)
            info = ypport.xnb_video_info(x)
            self.assertEqual((info["filename"], info["width"], info["height"]), ("Video\\intro.ogv", 1280, 720))
            self.assertAlmostEqual(info["fps"], 29.97, places=4)
            self.assertEqual(struct.unpack_from("<i", x.payload, info["height_offset"])[0], 720)
            # compressed the way prepare stores converted XNBs
            with open(path, "wb") as f:
                f.write(x.to_bytes(compress=True))
            self.assertEqual(ypport.xnb_video_info(ypport.XnbFile(path))["width"], 1280)

    def test_tool_output_and_jobs(self):
        self.assertEqual(ypport.parse_tool_output("a=1\nskip=already fits 640x480\nnoise\n"),
                         {"a": "1", "skip": "already fits 640x480"})
        seen = []
        cmds = [[sys.executable, "-c", "import sys; print('n=%d' % {}); sys.exit({})".format(i, i % 2)]
                for i in range(5)]
        ypport.run_jobs(cmds, 2, lambda i, rc, out, err: seen.append((i, rc, out.strip())))
        self.assertEqual(sorted(seen), [(i, i % 2, "n=%d" % i) for i in range(5)])

if __name__ == "__main__":
    unittest.main()
