#!/usr/bin/env python3
"""Helpers for the texture conversion test in run_tests.sh.

    texture_check.py make CONTENT_DIR        write test XNBs (Color, plain and LZ4)
    texture_check.py check NAME DUMP [FORMAT] compare a rendered dump with what
                                              the texture should look like (FORMAT:
                                              color, 5551 or 4444; default from NAME)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "port", "yuppiepsycho", "tools"))
import ypport  # noqa: E402

W, H = 64, 32
READER = b"Microsoft.Xna.Framework.Content.Texture2DReader"


def image(soft_alpha):
    """Pixel-art-like content: a transparent background, opaque blocks in
    many colours, and (soft_alpha) a premultiplied alpha gradient row band."""
    px = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            i = (y * W + x) * 4
            if 4 <= x < 60 and 4 <= y < 20:
                px[i:i + 4] = bytes(((x * 37) & 255, (y * 53) & 255, ((x ^ y) * 29) & 255, 255))
            elif soft_alpha and y >= 24:
                a = x * 4
                px[i:i + 4] = bytes((a * 200 // 255, a * 100 // 255, a * 50 // 255, a))
    return bytes(px)


def xnb(rgba, lz4):
    payload = bytes([1, len(READER)]) + READER + struct.pack("<i", 0) + bytes([0, 1])
    payload += struct.pack("<iIII", 0, W, H, 1) + struct.pack("<I", len(rgba)) + rgba
    f = ypport.XnbFile.__new__(ypport.XnbFile)
    f.platform, f.version, f.flags, f.payload = b"d", 5, 0, payload
    return f.to_bytes(compress=lz4)


def expected(name, fmt=None):
    rgba = image(soft_alpha=name.startswith("soft"))
    fmt = fmt or name
    if fmt.endswith("5551"):
        bits = [5, 5, 5, 1]
    elif fmt.endswith("4444"):
        bits = [4, 4, 4, 4]
    else:
        return rgba
    out = bytearray(len(rgba))
    for i, v in enumerate(rgba):
        b = bits[i % 4]
        if b == 1:
            out[i] = 255 if v >= 128 else 0
            continue
        q = (v * ((1 << b) - 1) + 127) // 255
        out[i] = (q * 255 + ((1 << b) - 1) // 2) // ((1 << b) - 1)
    return bytes(out)


def main():
    if sys.argv[1] == "make":
        d = sys.argv[2]
        for name, soft, lz4 in (("binary", False, False), ("binary_lz4", False, True), ("soft", True, True)):
            with open(os.path.join(d, name + ".xnb"), "wb") as f:
                f.write(xnb(image(soft), lz4))
        return 0
    name, dump = sys.argv[2], sys.argv[3]
    want = expected(name, sys.argv[4] if len(sys.argv) > 4 else None)
    got = open(dump, "rb").read()
    if len(got) != len(want):
        print("size mismatch %d != %d" % (len(got), len(want)))
        return 1
    rows = [got[y * W * 4:(y + 1) * W * 4] for y in range(H)]
    flipped = b"".join(reversed(rows))
    for candidate, label in ((got, ""), (flipped, " (rows bottom-up)")):
        worst = max(abs(a - b) for a, b in zip(candidate, want))
        if worst <= 1:
            print("%s matches%s, max deviation %d" % (name, label, worst))
            return 0
    worst = max(abs(a - b) for a, b in zip(got, want))
    print("%s differs, max deviation %d" % (name, worst))
    return 1


if __name__ == "__main__":
    sys.exit(main())
