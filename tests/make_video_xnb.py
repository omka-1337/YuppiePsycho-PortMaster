#!/usr/bin/env python3
"""Write a Video XNB the way the XNA/MonoGame content pipeline does:
VideoReader with filename, duration, width, height, fps and soundtrack type,
each field written with ReadObject<T> (a type reader index, then the value).

    make_video_xnb.py OUT FILENAME WIDTH HEIGHT [FPS]
"""
import struct
import sys


def s7(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def xstr(text):
    raw = text.encode("utf-8")
    return s7(len(raw)) + raw


def video_xnb(filename, width, height, fps=30.0, duration_ms=2000):
    readers = ["Microsoft.Xna.Framework.Content.VideoReader, Microsoft.Xna.Framework.Video",
               "Microsoft.Xna.Framework.Content.StringReader",
               "Microsoft.Xna.Framework.Content.Int32Reader",
               "Microsoft.Xna.Framework.Content.SingleReader"]
    p = s7(len(readers)) + b"".join(xstr(r) + struct.pack("<i", 0) for r in readers)
    p += s7(0) + s7(1)
    p += s7(2) + xstr(filename)
    for value in (duration_ms, width, height):
        p += s7(3) + struct.pack("<i", value)
    p += s7(4) + struct.pack("<f", fps) + s7(3) + struct.pack("<i", 0)
    return b"XNBw\x05\x00" + struct.pack("<I", 10 + len(p)) + p


if __name__ == "__main__":
    out, name, w, h = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    fps = float(sys.argv[5]) if len(sys.argv) > 5 else 30.0
    with open(out, "wb") as f:
        f.write(video_xnb(name, w, h, fps))
