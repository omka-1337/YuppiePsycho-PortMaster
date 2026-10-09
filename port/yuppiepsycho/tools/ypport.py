#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ypport - helper for the Yuppie Psycho PortMaster port.

Yuppie Psycho (v2.5 and later) runs on MonoGame, i.e. on .NET. The game code
itself is CPU-independent IL; only the packaging is tied to x86_64 (apphost,
bundled runtime, native libraries such as SDL2, OpenAL, FMOD, steam_api).
This tool strips that packaging so the IL can run on an aarch64 .NET runtime.

Subcommands
  inspect DIR             Describe how a game build is packaged and what it
                          needs to run natively on aarch64.
  extract BUNDLE OUTDIR   Extract a .NET single-file bundle.
  prepare GAMEDIR         Make a copied game build runnable on an aarch64
                          runtime: extract the bundle, park x86 runtime files
                          and native libraries, rewrite runtimeconfig/deps.json
                          and write .ypport/launch.env for the launcher.
  restore GAMEDIR         Undo `prepare`.
  textures DIR, videos DIR  What the game's images and cutscenes cost on the
                          device (memory, decoding time).

Only the Python standard library is used, so the same file runs on a PC and
on the handheld (Python 3.6+).
"""

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib

try:
    from shlex import quote as shquote
except ImportError:  # pragma: no cover
    from pipes import quote as shquote

VERSION = "0.4.0"
STATE_DIR = ".ypport"

# --------------------------------------------------------------------------
# Native binary sniffing
# --------------------------------------------------------------------------

ELF_MACHINES = {0x03: "x86", 0x28: "arm", 0x3E: "x86_64", 0xB7: "aarch64"}
PE_MACHINES = {0x014C: "x86", 0x8664: "x86_64", 0xAA64: "aarch64", 0x01C4: "arm"}
MACHO_CPUS = {7: "x86", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "aarch64"}


def sniff(path):
    """Return (format, arch) of a binary, or (None, None) for anything else.

    format is one of 'elf', 'pe', 'macho'. arch may be a list for universal
    Mach-O binaries.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except (IOError, OSError):
        return None, None
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        endian = "<" if head[5] == 1 else ">"
        machine = struct.unpack_from(endian + "H", head, 18)[0]
        return "elf", ELF_MACHINES.get(machine, hex(machine))
    if head[:2] == b"MZ" and len(head) >= 64:
        lfanew = struct.unpack_from("<I", head, 60)[0]
        if lfanew + 6 <= len(head) and head[lfanew:lfanew + 4] == b"PE\0\0":
            machine = struct.unpack_from("<H", head, lfanew + 4)[0]
            return "pe", PE_MACHINES.get(machine, hex(machine))
        return None, None
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cpu = struct.unpack_from("<I", head, 4)[0]
        return "macho", MACHO_CPUS.get(cpu, hex(cpu))
    if head[:4] == b"\xca\xfe\xba\xbe":
        count = struct.unpack_from(">I", head, 4)[0]
        # Java class files share this magic; their "count" is a version >= 45.
        if 0 < count < 16 and 8 + count * 20 <= len(head):
            archs = []
            for i in range(count):
                cpu = struct.unpack_from(">I", head, 8 + i * 20)[0]
                archs.append(MACHO_CPUS.get(cpu, hex(cpu)))
            return "macho", archs
    return None, None


def elf_exports(path):
    """Names of the defined dynamic symbols of a 64-bit little-endian ELF
    shared object, or None if the file is not one."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (IOError, OSError):
        return None
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    try:
        shoff, = struct.unpack_from("<Q", data, 0x28)
        shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
        sections = []
        for i in range(shnum):
            sh = struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
            sections.append(sh)
        names = set()
        for sh in sections:
            if sh[1] != 11:  # SHT_DYNSYM
                continue
            offset, size, link, entsize = sh[4], sh[5], sh[6], sh[9] or 24
            stroff = sections[link][4]
            for j in range(size // entsize):
                st_name, st_info, _other, st_shndx = struct.unpack_from("<IBBH", data, offset + j * entsize)
                if st_shndx == 0 or not st_name or (st_info >> 4) not in (1, 2):  # undefined / local
                    continue
                end = data.index(b"\0", stroff + st_name)
                names.add(data[stroff + st_name:end].decode("ascii", "replace"))
        return names
    except (struct.error, IndexError, ValueError):
        return None


def file_contains(path, needles, chunk=1 << 20):
    """Return the subset of byte strings in `needles` found in the file."""
    found = set()
    overlap = max(len(n) for n in needles) - 1
    tail = b""
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            window = tail + block
            for n in needles:
                if n not in found and n in window:
                    found.add(n)
            if len(found) == len(needles):
                break
            tail = window[-overlap:] if overlap else b""
    return found


# --------------------------------------------------------------------------
# .NET single-file bundles
# --------------------------------------------------------------------------

# Marker the apphost carries right after the 8-byte bundle header offset
# (src/native/corehost/apphost/bundle_marker.cpp in dotnet/runtime).
BUNDLE_SIGNATURE = bytes((
    0x8B, 0x12, 0x02, 0xB9, 0x6A, 0x61, 0x20, 0x38,
    0x72, 0x7B, 0x93, 0x02, 0x14, 0xD7, 0xA0, 0x32,
    0x13, 0xF5, 0xB9, 0xE6, 0xEF, 0xAE, 0x33, 0x18,
    0xEE, 0x3B, 0x2D, 0xCE, 0x24, 0xB3, 0x6A, 0xAE))

BUNDLE_FILE_TYPES = {0: "unknown", 1: "assembly", 2: "native", 3: "deps.json",
                     4: "runtimeconfig.json", 5: "symbols"}

# Exported by every NativeAOT executable (dotnet/runtime, .NET 7+).
NATIVEAOT_MARKERS = (b"DotNetRuntimeDebugHeader", b"S_P_CoreLib_System_")


class BundleError(Exception):
    pass


class BundleEntry(object):
    __slots__ = ("offset", "size", "compressed_size", "type", "path")

    def __init__(self, offset, size, compressed_size, ftype, path):
        self.offset = offset
        self.size = size
        self.compressed_size = compressed_size
        self.type = ftype
        self.path = path

    @property
    def type_name(self):
        return BUNDLE_FILE_TYPES.get(self.type, str(self.type))


def _read_7bit_string(f):
    length = 0
    shift = 0
    while True:
        raw = f.read(1)
        if not raw:
            raise BundleError("truncated bundle manifest")
        b = raw[0]
        length |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
        if shift > 35:
            raise BundleError("bad string length in bundle manifest")
    data = f.read(length)
    if len(data) != length:
        raise BundleError("truncated bundle manifest")
    return data.decode("utf-8")


def _safe_relpath(path):
    """Normalise a path taken from an untrusted archive; refuse escapes."""
    p = path.replace("\\", "/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in parts:
        raise BundleError("unsafe path in bundle: %r" % path)
    return "/".join(parts)


class Bundle(object):
    """Reader for the .NET Core 3.0+ single-file bundle format."""

    def __init__(self, path):
        self.path = path
        self.header_offset = find_bundle_header(path)
        if not self.header_offset:
            raise BundleError("%s is not a .NET single-file bundle" % path)
        with open(path, "rb") as f:
            f.seek(self.header_offset)
            self.major, self.minor, count = struct.unpack("<IIi", f.read(12))
            if self.major not in (1, 2, 6) and not 2 <= self.major <= 20:
                raise BundleError("unsupported bundle version %d.%d" % (self.major, self.minor))
            if not 0 < count < 100000:
                raise BundleError("implausible bundle file count %d" % count)
            self.bundle_id = _read_7bit_string(f)
            self.flags = 0
            if self.major >= 2:
                (self.deps_offset, self.deps_size, self.rtconfig_offset,
                 self.rtconfig_size, self.flags) = struct.unpack("<qqqqQ", f.read(40))
            self.entries = []
            for _ in range(count):
                if self.major >= 6:
                    offset, size, csize = struct.unpack("<qqq", f.read(24))
                else:
                    offset, size = struct.unpack("<qq", f.read(16))
                    csize = 0
                ftype = struct.unpack("<B", f.read(1))[0]
                rel = _safe_relpath(_read_7bit_string(f))
                self.entries.append(BundleEntry(offset, size, csize, ftype, rel))
        fsize = os.path.getsize(path)
        for e in self.entries:
            stored = e.compressed_size or e.size
            if e.offset < 0 or e.size < 0 or e.offset + stored > fsize:
                raise BundleError("entry %s points outside the file" % e.path)

    @property
    def compressed(self):
        return any(e.compressed_size for e in self.entries)

    def find(self, suffix):
        return [e for e in self.entries if e.path.lower().endswith(suffix.lower())]

    def iter_chunks(self, entry, chunk=1 << 20):
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            remaining = entry.compressed_size or entry.size
            dec = zlib.decompressobj(-15) if entry.compressed_size else None
            produced = 0
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    raise BundleError("truncated entry %s" % entry.path)
                remaining -= len(data)
                if dec is not None:
                    data = dec.decompress(data)
                produced += len(data)
                if data:
                    yield data
            if dec is not None:
                rest = dec.flush()
                produced += len(rest)
                if rest:
                    yield rest
            if produced != entry.size:
                raise BundleError("size mismatch for %s (%d != %d)" % (entry.path, produced, entry.size))

    def read(self, entry):
        return b"".join(self.iter_chunks(entry))

    def extract(self, outdir, overwrite=False, log=None):
        """Extract every entry below outdir. Returns the written rel paths."""
        written = []
        root = os.path.realpath(outdir)
        for e in self.entries:
            dest = os.path.realpath(os.path.join(root, *e.path.split("/")))
            if not dest.startswith(root + os.sep):
                raise BundleError("unsafe path in bundle: %r" % e.path)
            if os.path.exists(dest) and not overwrite:
                if os.path.getsize(dest) == e.size:
                    continue
                raise BundleError("%s already exists with different content" % dest)
            d = os.path.dirname(dest)
            if not os.path.isdir(d):
                os.makedirs(d)
            tmp = dest + ".yptmp"
            with open(tmp, "wb") as out:
                for data in self.iter_chunks(e):
                    out.write(data)
            os.rename(tmp, dest)
            written.append(e.path)
            if log:
                log("  extracted %s (%s)" % (e.path, human(e.size)))
        return written


def find_bundle_header(path):
    """Return the bundle header offset of an apphost, 0 if it is not a bundle,
    None if the file is not an apphost at all."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        chunk = 1 << 20
        pos = 0
        tail = b""
        while pos < size:
            f.seek(pos)
            block = f.read(chunk)
            if not block:
                break
            window = tail + block
            i = window.find(BUNDLE_SIGNATURE)
            if i >= 8:
                offset = struct.unpack_from("<q", window, i - 8)[0]
                if 0 < offset < size:
                    return offset
                return 0
            tail = window[-(len(BUNDLE_SIGNATURE) + 8):]
            pos += len(block)
    return None


# --------------------------------------------------------------------------
# PE / ECMA-335 metadata reader (just what we need)
# --------------------------------------------------------------------------

class NotManaged(Exception):
    pass


_SCHEMA = [
    (0x00, ["u16", "str", "guid", "guid", "guid"]),                     # Module
    (0x01, [("c", "ResolutionScope"), "str", "str"]),                    # TypeRef
    (0x02, ["u32", "str", "str", ("c", "TypeDefOrRef"), ("t", 0x04), ("t", 0x06)]),  # TypeDef
    (0x03, [("t", 0x04)]),                                               # FieldPtr
    (0x04, ["u16", "str", "blob"]),                                      # Field
    (0x05, [("t", 0x06)]),                                               # MethodPtr
    (0x06, ["u32", "u16", "u16", "str", "blob", ("t", 0x08)]),           # MethodDef
    (0x07, [("t", 0x08)]),                                               # ParamPtr
    (0x08, ["u16", "u16", "str"]),                                       # Param
    (0x09, [("t", 0x02), ("c", "TypeDefOrRef")]),                        # InterfaceImpl
    (0x0A, [("c", "MemberRefParent"), "str", "blob"]),                   # MemberRef
    (0x0B, ["u16", ("c", "HasConstant"), "blob"]),                       # Constant
    (0x0C, [("c", "HasCustomAttribute"), ("c", "CustomAttributeType"), "blob"]),  # CustomAttribute
    (0x0D, [("c", "HasFieldMarshal"), "blob"]),                          # FieldMarshal
    (0x0E, ["u16", ("c", "HasDeclSecurity"), "blob"]),                   # DeclSecurity
    (0x0F, ["u16", "u32", ("t", 0x02)]),                                 # ClassLayout
    (0x10, ["u32", ("t", 0x04)]),                                        # FieldLayout
    (0x11, ["blob"]),                                                    # StandAloneSig
    (0x12, [("t", 0x02), ("t", 0x14)]),                                  # EventMap
    (0x13, [("t", 0x14)]),                                               # EventPtr
    (0x14, ["u16", "str", ("c", "TypeDefOrRef")]),                       # Event
    (0x15, [("t", 0x02), ("t", 0x17)]),                                  # PropertyMap
    (0x16, [("t", 0x17)]),                                               # PropertyPtr
    (0x17, ["u16", "str", "blob"]),                                      # Property
    (0x18, ["u16", ("t", 0x06), ("c", "HasSemantics")]),                 # MethodSemantics
    (0x19, [("t", 0x02), ("c", "MethodDefOrRef"), ("c", "MethodDefOrRef")]),  # MethodImpl
    (0x1A, ["str"]),                                                     # ModuleRef
    (0x1B, ["blob"]),                                                    # TypeSpec
    (0x1C, ["u16", ("c", "MemberForwarded"), "str", ("t", 0x1A)]),       # ImplMap
    (0x1D, ["u32", ("t", 0x04)]),                                        # FieldRVA
    (0x1E, ["u32", "u32"]),                                              # EncLog
    (0x1F, ["u32"]),                                                     # EncMap
    (0x20, ["u32", "u16", "u16", "u16", "u16", "u32", "blob", "str", "str"]),  # Assembly
    (0x21, ["u32"]),                                                     # AssemblyProcessor
    (0x22, ["u32", "u32", "u32"]),                                       # AssemblyOS
    (0x23, ["u16", "u16", "u16", "u16", "u32", "blob", "str", "str", "blob"]),  # AssemblyRef
    (0x24, ["u32", ("t", 0x23)]),                                        # AssemblyRefProcessor
    (0x25, ["u32", "u32", "u32", ("t", 0x23)]),                          # AssemblyRefOS
    (0x26, ["u32", "str", "blob"]),                                      # File
    (0x27, ["u32", "u32", "str", "str", ("c", "Implementation")]),       # ExportedType
    (0x28, ["u32", "u32", "str", ("c", "Implementation")]),              # ManifestResource
    (0x29, [("t", 0x02), ("t", 0x02)]),                                  # NestedClass
    (0x2A, ["u16", "u16", ("c", "TypeOrMethodDef"), "str"]),             # GenericParam
    (0x2B, [("c", "MethodDefOrRef"), "blob"]),                           # MethodSpec
    (0x2C, [("t", 0x2A), ("c", "TypeDefOrRef")]),                        # GenericParamConstraint
]

_CODED = {
    "TypeDefOrRef": (2, (0x02, 0x01, 0x1B)),
    "HasConstant": (2, (0x04, 0x08, 0x17)),
    "HasCustomAttribute": (5, (0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00, 0x0E, 0x17,
                               0x14, 0x11, 0x1A, 0x1B, 0x20, 0x23, 0x26, 0x27, 0x28, 0x2A,
                               0x2C, 0x2B)),
    "HasFieldMarshal": (1, (0x04, 0x08)),
    "HasDeclSecurity": (2, (0x02, 0x06, 0x20)),
    "MemberRefParent": (3, (0x02, 0x01, 0x1A, 0x06, 0x1B)),
    "HasSemantics": (1, (0x14, 0x17)),
    "MethodDefOrRef": (1, (0x06, 0x0A)),
    "MemberForwarded": (1, (0x04, 0x06)),
    "Implementation": (2, (0x26, 0x23, 0x27)),
    "CustomAttributeType": (3, (None, None, 0x06, 0x0A, None)),
    "ResolutionScope": (2, (0x00, 0x1A, 0x23, 0x01)),
    "TypeOrMethodDef": (1, (0x02, 0x06)),
}

COMIMAGE_FLAGS_ILONLY = 0x1
COMIMAGE_FLAGS_32BITREQUIRED = 0x2
R2R_OS_IDS = {"osx": 0x4644, "freebsd": 0xADC4, "linux": 0x7B79, "netbsd": 0x1993}


class Assembly(object):
    """Minimal reader for a managed PE image held in memory."""

    def __init__(self, data, name="<memory>"):
        self.data = data
        self.name = name
        if data[:2] != b"MZ":
            raise NotManaged("%s: not a PE file" % name)
        lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[lfanew:lfanew + 4] != b"PE\0\0":
            raise NotManaged("%s: not a PE file" % name)
        coff = lfanew + 4
        self.machine, nsect = struct.unpack_from("<HH", data, coff)
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        opt = coff + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        dd = opt + (96 if magic == 0x10B else 112)
        ndirs = struct.unpack_from("<I", data, dd - 4)[0]
        if ndirs < 15:
            raise NotManaged("%s: no CLI header" % name)
        self.sections = []
        s = opt + opt_size
        for _ in range(nsect):
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, s + 8)
            self.sections.append((va, max(vsize, rawsize), rawptr))
            s += 40
        cli_rva, cli_size = struct.unpack_from("<II", data, dd + 14 * 8)
        if not cli_rva:
            raise NotManaged("%s: native image (no CLI header)" % name)
        cli = self._off(cli_rva)
        (_, self.rt_major, self.rt_minor, md_rva, _md_size, self.cli_flags,
         _ep) = struct.unpack_from("<IHHIIII", data, cli)
        self.lfanew = lfanew
        self.opt_offset = opt
        self.dirs_offset = dd
        self.pe32_plus = magic == 0x20B
        self.cli_offset = cli
        mnh_rva = struct.unpack_from("<I", data, cli + 64)[0]
        self.ready_to_run = False
        self.r2r_flags = 0
        if mnh_rva:
            off = self._off(mnh_rva)
            self.ready_to_run = off is not None and data[off:off + 4] == b"RTR\0"
            if self.ready_to_run:
                self.r2r_flags = struct.unpack_from("<I", data, off + 8)[0]
        self._parse_metadata(self._off(md_rva))

    # -- PE helpers --------------------------------------------------------
    def _off(self, rva):
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        return None

    @property
    def arch(self):
        if self.machine in PE_MACHINES:
            return PE_MACHINES[self.machine]
        # ReadyToRun images for non-Windows targets xor the machine with an OS id.
        for os_name, os_id in R2R_OS_IDS.items():
            m = self.machine ^ os_id
            if m in PE_MACHINES:
                return "%s-%s" % (os_name, PE_MACHINES[m])
        return hex(self.machine)

    @property
    def il_only(self):
        # ReadyToRun images keep their IL; precompiled code for another CPU is
        # simply ignored by the runtime and the IL is JIT-compiled instead.
        return bool(self.cli_flags & COMIMAGE_FLAGS_ILONLY) or self.ready_to_run

    @property
    def requires_32bit(self):
        return bool(self.cli_flags & COMIMAGE_FLAGS_32BITREQUIRED)

    # -- metadata ----------------------------------------------------------
    def _parse_metadata(self, root):
        d = self.data
        if struct.unpack_from("<I", d, root)[0] != 0x424A5342:
            raise NotManaged("%s: bad metadata signature" % self.name)
        vlen = struct.unpack_from("<I", d, root + 12)[0]
        self.runtime_version = d[root + 16:root + 16 + vlen].split(b"\0")[0].decode("ascii", "replace")
        p = root + 16 + vlen
        nstreams = struct.unpack_from("<H", d, p + 2)[0]
        p += 4
        self.streams = {}
        for _ in range(nstreams):
            off, size = struct.unpack_from("<II", d, p)
            end = d.index(b"\0", p + 8)
            sname = d[p + 8:end].decode("ascii")
            p = (end + 4) & ~3
            self.streams[sname] = (root + off, size)
        tables = self.streams.get("#~") or self.streams.get("#-")
        if not tables:
            raise NotManaged("%s: no metadata tables" % self.name)
        t = tables[0]
        heap_sizes = d[t + 6]
        valid = struct.unpack_from("<Q", d, t + 8)[0]
        p = t + 24
        self.rows = [0] * 64
        for i in range(64):
            if valid >> i & 1:
                self.rows[i] = struct.unpack_from("<I", d, p)[0]
                p += 4
        if heap_sizes & 0x40:  # extra data (uncompressed/EnC streams)
            p += 4
        self._str_w = 4 if heap_sizes & 1 else 2
        self._guid_w = 4 if heap_sizes & 2 else 2
        self._blob_w = 4 if heap_sizes & 4 else 2
        self._layout = {}
        for tid, cols in _SCHEMA:
            widths = [self._col_width(c) for c in cols]
            self._layout[tid] = (p, sum(widths), widths)
            p += sum(widths) * self.rows[tid]
        self._cache = {}

    def _col_width(self, col):
        if col == "u16":
            return 2
        if col == "u32":
            return 4
        if col == "str":
            return self._str_w
        if col == "guid":
            return self._guid_w
        if col == "blob":
            return self._blob_w
        kind, ref = col
        if kind == "t":
            return 2 if self.rows[ref] < 0x10000 else 4
        bits, targets = _CODED[ref]
        biggest = max(self.rows[x] for x in targets if x is not None)
        return 2 if biggest < (1 << (16 - bits)) else 4

    def table(self, tid):
        if tid in self._cache:
            return self._cache[tid]
        start, rsize, widths = self._layout[tid]
        d = self.data
        out = []
        for r in range(self.rows[tid]):
            p = start + r * rsize
            row = []
            for w in widths:
                row.append(struct.unpack_from("<H" if w == 2 else "<I", d, p)[0])
                p += w
            out.append(row)
        self._cache[tid] = out
        return out

    def string(self, idx):
        base = self.streams["#Strings"][0]
        end = self.data.index(b"\0", base + idx)
        return self.data[base + idx:end].decode("utf-8", "replace")

    def blob(self, idx):
        base = self.streams["#Blob"][0]
        p = base + idx
        n, hdr = _compressed_uint(self.data, p)
        return self.data[p + hdr:p + hdr + n]

    def user_strings(self):
        if "#US" not in self.streams:
            return
        base, size = self.streams["#US"]
        p = base + 1
        end = base + size
        while p < end:
            n, hdr = _compressed_uint(self.data, p)
            p += hdr
            if n == 0:
                continue
            raw = self.data[p:p + n - 1]
            p += n
            try:
                yield raw.decode("utf-16-le")
            except UnicodeDecodeError:
                pass

    @staticmethod
    def _decode(coded_name, value):
        bits, targets = _CODED[coded_name]
        return targets[value & ((1 << bits) - 1)], value >> bits

    # -- convenience queries ---------------------------------------------
    def assembly_name(self):
        rows = self.table(0x20)
        if not rows:
            return None, None
        r = rows[0]
        return self.string(r[7]), "%d.%d.%d.%d" % (r[1], r[2], r[3], r[4])

    def assembly_refs(self):
        return [(self.string(r[6]), "%d.%d.%d.%d" % (r[0], r[1], r[2], r[3]))
                for r in self.table(0x23)]

    def module_refs(self):
        return [self.string(r[0]) for r in self.table(0x1A)]

    def pinvokes(self):
        """Map of native module name -> sorted list of imported entry points."""
        mods = self.module_refs()
        methods = self.table(0x06)
        out = {}
        for flags, member, import_name, scope in self.table(0x1C):
            if not 0 < scope <= len(mods):
                continue
            entry = self.string(import_name)
            if not entry:
                tab, idx = self._decode("MemberForwarded", member)
                if tab == 0x06 and 0 < idx <= len(methods):
                    entry = self.string(methods[idx - 1][3])
            out.setdefault(mods[scope - 1], set()).add(entry)
        return dict((k, sorted(v)) for k, v in out.items())

    def _invoke_param_count(self, td_index):
        """Parameter count of the Invoke method of TypeDef #td_index (1-based)."""
        typedefs = self.table(0x02)
        methods = self.table(0x06)
        td = typedefs[td_index - 1]
        last = typedefs[td_index][5] if td_index < len(typedefs) else len(methods) + 1
        for midx in range(td[5], last):
            if self.string(methods[midx - 1][3]) == "Invoke":
                return _method_sig_param_count(self.blob(methods[midx - 1][4]))
        return None

    def native_binding_arity(self, func_name):
        """Best-effort parameter counts of a native function bound by name.

        Covers the usual binding styles: delegate types (FuncLoader:
        `d_tf_videoinfo`), fields holding a delegate or an unmanaged function
        pointer, and wrapper methods named after the function. Returns
        {"<kind> <member>": count}."""
        key = func_name.lower()
        typedefs = self.table(0x02)
        found = {}
        for i, td in enumerate(typedefs):
            name = self.string(td[1])
            if key in name.lower():
                n = self._invoke_param_count(i + 1)
                if n is not None:
                    found["delegate " + name] = n
        for flags, fname_idx, sig_idx in self.table(0x04):
            fname = self.string(fname_idx)
            if key not in fname.lower():
                continue
            sig = self.blob(sig_idx)
            n, kind = self._field_callable_arity(sig)
            if n is not None:
                found["%s field %s" % (kind, fname)] = n
        for _rva, _impl, _flags, mname_idx, sig_idx, _params in self.table(0x06):
            mname = self.string(mname_idx)
            if mname != "Invoke" and key in mname.lower():
                n = _method_sig_param_count(self.blob(sig_idx))
                if n is not None:
                    found["method " + mname] = n
        return found

    def _field_callable_arity(self, sig):
        """For a field signature holding a function pointer or a delegate
        defined in this assembly: (parameter count, kind)."""
        if not sig or sig[0] != 0x06:
            return None, None
        p = 1
        while p < len(sig) and sig[p] in (0x1F, 0x20):  # custom modifiers
            _, hdr = _compressed_uint(sig, p + 1)
            p += 1 + hdr
        if p >= len(sig):
            return None, None
        if sig[p] == 0x1B:  # FNPTR
            return _method_sig_param_count(sig[p + 1:]), "function-pointer"
        if sig[p] == 0x12:  # CLASS
            coded, _ = _compressed_uint(sig, p + 1)
            if coded & 3 == 0:  # TypeDef
                return self._invoke_param_count(coded >> 2), "delegate"
        return None, None

    def type_defs(self):
        return [(self.string(r[2]), self.string(r[1])) for r in self.table(0x02)]

    def type_refs(self):
        return [(self.string(r[2]), self.string(r[1])) for r in self.table(0x01)]

    def custom_attribute_strings(self, attr_type_name):
        """First string argument of every custom attribute of the given type."""
        typerefs = self.table(0x01)
        typedefs = self.table(0x02)
        memberrefs = self.table(0x0A)
        values = []
        for _parent, ctype, value in self.table(0x0C):
            tab, idx = self._decode("CustomAttributeType", ctype)
            if tab != 0x0A or not 0 < idx <= len(memberrefs):
                continue
            ptab, pidx = self._decode("MemberRefParent", memberrefs[idx - 1][0])
            if ptab == 0x01 and 0 < pidx <= len(typerefs):
                tname = self.string(typerefs[pidx - 1][1])
            elif ptab == 0x02 and 0 < pidx <= len(typedefs):
                tname = self.string(typedefs[pidx - 1][1])
            else:
                continue
            if tname != attr_type_name:
                continue
            blob = self.blob(value)
            if len(blob) < 3 or blob[:2] != b"\x01\x00" or blob[2] == 0xFF:
                continue
            n, hdr = _compressed_uint(blob, 2)
            values.append(blob[2 + hdr:2 + hdr + n].decode("utf-8", "replace"))
        return values

    def target_framework(self):
        v = self.custom_attribute_strings("TargetFrameworkAttribute")
        return v[0] if v else None

    def const_value(self, namespace, type_name, field_name):
        """Value of a literal int32/int64/string field, or None."""
        typedefs = self.table(0x02)
        fields = self.table(0x04)
        for i, td in enumerate(typedefs):
            if self.string(td[1]) != type_name or self.string(td[2]) != namespace:
                continue
            first = td[4]
            last = typedefs[i + 1][4] if i + 1 < len(typedefs) else len(fields) + 1
            for fidx in range(first, last):
                if self.string(fields[fidx - 1][1]) != field_name:
                    continue
                for ctype, parent, value in self.table(0x0B):
                    tab, idx = self._decode("HasConstant", parent)
                    if tab == 0x04 and idx == fidx:
                        raw = self.blob(value)
                        etype = ctype & 0xFF
                        if etype == 0x08 and len(raw) == 4:
                            return struct.unpack("<i", raw)[0]
                        if etype == 0x09 and len(raw) == 4:
                            return struct.unpack("<I", raw)[0]
                        if etype == 0x0E:
                            return raw.decode("utf-16-le")
                        return raw
        return None


def _compressed_uint(data, p):
    b0 = data[p]
    if b0 & 0x80 == 0:
        return b0, 1
    if b0 & 0xC0 == 0x80:
        return ((b0 & 0x3F) << 8) | data[p + 1], 2
    return (((b0 & 0x1F) << 24) | (data[p + 1] << 16) | (data[p + 2] << 8) | data[p + 3]), 4


def _method_sig_param_count(sig):
    """Parameter count from a MethodDefSig/MethodRefSig/StandAloneMethodSig."""
    if len(sig) < 2:
        return None
    p = 1
    if sig[0] & 0x10:  # GENERIC: skip the generic parameter count
        _, hdr = _compressed_uint(sig, p)
        p += hdr
    count, _ = _compressed_uint(sig, p)
    return count


ARCH_MACHINES = {"x86_64": 0x8664, "aarch64": 0xAA64}
COMIMAGE_FLAGS_IL_LIBRARY = 0x4
COMIMAGE_FLAGS_32BITPREFERRED = 0x20000


def retarget_assembly(data, arch):
    """Make an IL assembly that is tied to another CPU loadable on `arch`.

    `dotnet publish -r linux-x64` stamps Machine=AMD64 into every IL-only
    assembly it compiles; CoreCLR on aarch64 then refuses to load it
    ("FileLoadException: Could not load file or assembly"). The IL itself is
    CPU independent, so the Machine field is rewritten to the target CPU.
    ReadyToRun images additionally carry precompiled code and native PE
    structures for their CPU; they are turned back into plain IL images (the
    native directories, entry point and ReadyToRun header are cleared, the
    section layout and therefore every metadata RVA stays as it is), so the
    runtime JIT-compiles the IL instead.

    Returns (new_data, description) or (None, None) when nothing has to change.
    """
    target = ARCH_MACHINES[arch]
    asm = load_assembly(data, "<retarget>")
    if asm is None or not asm.il_only:
        return None, None
    out = bytearray(data)
    flags = asm.cli_flags
    changes = []
    if asm.pe32_plus:
        if asm.machine == target and not asm.ready_to_run:
            return None, None
        if asm.ready_to_run:
            if asm.machine == target ^ R2R_OS_IDS["linux"]:
                return None, None  # precompiled for this very platform
            # export, import, exception, base relocation, TLS, load config,
            # bound import, IAT and delay import directories: native only
            for index in (0, 1, 3, 5, 9, 10, 11, 12, 13):
                struct.pack_into("<II", out, asm.dirs_offset + 8 * index, 0, 0)
            struct.pack_into("<I", out, asm.opt_offset + 16, 0)  # AddressOfEntryPoint
            struct.pack_into("<II", out, asm.cli_offset + 64, 0, 0)  # ManagedNativeHeader
            flags = (flags | COMIMAGE_FLAGS_ILONLY) & ~COMIMAGE_FLAGS_IL_LIBRARY
            changes.append("ReadyToRun code for %s detached" % asm.arch)
        if asm.machine != target:
            changes.append("machine %s -> %s" % (asm.arch, arch))
        struct.pack_into("<H", out, asm.lfanew + 4, target)
    else:
        if not flags & (COMIMAGE_FLAGS_32BITREQUIRED | COMIMAGE_FLAGS_32BITPREFERRED):
            return None, None  # PE32 AnyCPU: already loadable everywhere
        flags &= ~(COMIMAGE_FLAGS_32BITREQUIRED | COMIMAGE_FLAGS_32BITPREFERRED)
        changes.append("32-bit requirement cleared")
    struct.pack_into("<I", out, asm.cli_offset + 16, flags)
    return bytes(out), ", ".join(changes)


def load_assembly(data, name):
    try:
        return Assembly(data, name)
    except (NotManaged, struct.error, ValueError, IndexError, KeyError):
        return None


# --------------------------------------------------------------------------
# Knowledge about the game's dependencies
# --------------------------------------------------------------------------

# Files that belong to the .NET runtime itself (the "runtime pack"). On a
# self-contained build they sit next to the game and are x86_64 specific.
RUNTIME_NATIVE_FILES = set("""
libcoreclr.so libclrjit.so libhostfxr.so libhostpolicy.so libclrgc.so libclrgcexp.so
libcoreclrtraceptprovider.so libmscordaccore.so libmscordbi.so createdump
libSystem.Native.so libSystem.Globalization.Native.so libSystem.IO.Compression.Native.so
libSystem.Net.Security.Native.so libSystem.Security.Cryptography.Native.OpenSsl.so
libSystem.IO.Ports.Native.so libSystem.Security.Cryptography.Native.Apple.dylib
libcoreclr.dylib libclrjit.dylib libhostfxr.dylib libhostpolicy.dylib libclrgc.dylib
libmscordaccore.dylib libmscordbi.dylib libSystem.Native.dylib
libSystem.Globalization.Native.dylib libSystem.IO.Compression.Native.dylib
libSystem.Net.Security.Native.dylib
coreclr.dll clrjit.dll hostfxr.dll hostpolicy.dll clrgc.dll clretwrc.dll mscordaccore.dll
mscordbi.dll mscorrc.dll mscordaccore_amd64_amd64_6.0.0.dll Microsoft.DiaSymReader.Native.amd64.dll
System.IO.Compression.Native.dll msquic.dll
""".split())

MANAGED_FRAMEWORK_PREFIXES = ("System.", "Microsoft.CSharp", "Microsoft.VisualBasic",
                              "Microsoft.Win32.", "Microsoft.Extensions.", "Microsoft.NETCore.",
                              "mscorlib", "netstandard", "WindowsBase")

# Mono/MonoKickstart runtime pieces shipped with old-style Linux/macOS builds.
MONO_RUNTIME_FILES = set("""
libmonosgen-2.0.so.1 libmono-2.0.so.1 libmonosgen-2.0.so libmono-native.so libMonoPosixHelper.so
libmonosgen-2.0.1.dylib libmono-native.dylib libMonoPosixHelper.dylib
""".split())

KNOWN_NATIVE = [
    # (regex on library name, key, description)
    (r"(^|lib)fmodstudio", "fmodstudio", "FMOD Studio (audio)"),
    (r"(^|lib)fmod(L)?($|\.|64|_)", "fmod", "FMOD Core (audio)"),
    (r"steam_api", "steam_api", "Steamworks"),
    (r"(^|lib)sdkencryptedappticket", "steam_ticket", "Steam encrypted app ticket"),
    (r"(^|lib)SDL2", "sdl2", "SDL2 (window, input)"),
    (r"(^|lib)SDL3", "sdl3", "SDL3"),
    (r"(soft_oal|openal|OpenAL)", "openal", "OpenAL"),
    (r"(^|lib)vlc", "vlc", "libVLC (video)"),
    (r"(avcodec|avformat|avutil|swscale|swresample)", "ffmpeg", "FFmpeg (video)"),
    (r"(^|lib)theorafile|theora", "theora", "Theora (video)"),
    (r"(^|lib)FNA3D", "fna3d", "FNA3D"),
    (r"(^|lib)FAudio", "faudio", "FAudio"),
    (r"(^|lib)(GL|opengl32|libGL)\b", "opengl", "OpenGL"),
    (r"(^|lib)vulkan", "vulkan", "Vulkan"),
    (r"(^|lib)discord", "discord", "Discord RPC"),
    (r"(^|lib)(eos|EOSSDK)", "eos", "Epic Online Services"),
    (r"(^|lib)Galaxy", "galaxy", "GOG Galaxy"),
    (r"(^|lib)SkiaSharp", "skia", "SkiaSharp"),
    (r"(^|lib)(e_sqlite3|sqlite)", "sqlite", "SQLite"),
    (r"(^|lib)(user32|kernel32|gdi32|imm32|winmm|shell32|ole32|dwmapi|xinput)", "win32", "Win32 API"),
]

NATIVE_NAME_RE = re.compile(r"^[\w.+-]{2,64}?(\.(so(\.\d+)*|dll|dylib))?$", re.I)
NATIVE_FILE_RE = re.compile(r"\.(so(\.\d+)*|dylib|dll)$", re.I)
LIB_STRING_RE = re.compile(r"^[\w.+-]+\.(so(\.\d+)*|dylib)$", re.I)


def classify_native(name):
    for rx, key, desc in KNOWN_NATIVE:
        if re.search(rx, name, re.I):
            return key, desc
    return None, None


def theorafile_api_level(exports):
    """Which FNA Theorafile API generation a library exports."""
    if "tf_setvideotrack" in exports:
        return "2021-12+ (multiple video tracks)"
    if "tf_setaudiotrack" in exports:
        return "2021-04+ (multiple audio tracks)"
    if "tf_videoinfo" in exports:
        return "pre-2021"
    return "unknown"


def fmod_version_str(num):
    if not isinstance(num, int):
        return None
    return "%x.%02x.%02x" % (num >> 16, (num >> 8) & 0xFF, num & 0xFF)


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------

def _walk(root, skip_state=True):
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_state and STATE_DIR in dirnames:
            dirnames.remove(STATE_DIR)
        dirnames.sort()
        for fn in sorted(filenames):
            yield os.path.join(dirpath, fn)


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


class ManagedSource(object):
    """One managed assembly, either on disk or inside a bundle."""

    def __init__(self, relpath, loader, size):
        self.relpath = relpath
        self.loader = loader
        self.size = size
        self._asm = False

    @property
    def asm(self):
        if self._asm is False:
            self._asm = load_assembly(self.loader(), self.relpath)
        return self._asm


def _looks_like_framework(filename):
    return filename.startswith(MANAGED_FRAMEWORK_PREFIXES) and filename.lower().endswith(".dll")


def inspect_dir(root, skip=()):
    """Collect everything we know about a game directory into a dict.

    `skip` holds relative paths to ignore (e.g. a bundle already extracted).
    """
    root = os.path.abspath(root)
    skip = set(skip)
    report = {
        "tool": "ypport %s" % VERSION,
        "root": root,
        "total_size": 0,
        "file_count": 0,
        "bundles": [],
        "nativeaot": [],
        "apphosts": [],
        "runtimeconfigs": [],
        "native_libs": [],
        "runtime_files": [],
        "mono_runtime_files": [],
        "managed": [],
        "content_dirs": [],
        "videos": [],
        "notes": [],
    }
    managed_sources = []
    top_dirs = set()

    for path in _walk(root):
        rel = _rel(root, path)
        if rel in skip:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        report["total_size"] += size
        report["file_count"] += 1
        if "/" in rel:
            top_dirs.add(rel.split("/")[0])
        base = os.path.basename(path)
        lower = base.lower()
        if lower.endswith(VIDEO_EXTS):
            try:
                info = ogg_video_info(path)
            except (OSError, IOError, struct.error, ValueError):
                info = None
            if info is not None:
                info["path"] = rel
                report["videos"].append(info)
            continue
        if lower.endswith(".runtimeconfig.json"):
            report["runtimeconfigs"].append(rel)
            continue
        fmt, arch = sniff(path)
        if fmt is None:
            continue
        if base in RUNTIME_NATIVE_FILES:
            report["runtime_files"].append({"path": rel, "format": fmt, "arch": arch})
            continue
        if base in MONO_RUNTIME_FILES or lower.startswith("libmono"):
            report["mono_runtime_files"].append({"path": rel, "format": fmt, "arch": arch})
            continue
        if fmt == "pe" and lower.endswith((".dll", ".exe")):
            if _looks_like_framework(base):
                managed_sources.append(ManagedSource(rel, None, size))
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except (IOError, OSError):
                continue
            asm = load_assembly(data, rel)
            if asm is not None:
                src = ManagedSource(rel, None, size)
                src._asm = asm
                managed_sources.append(src)
                continue
        # Native executable or library from here on.
        hdr = find_bundle_header(path) if size > 64 * 1024 else None
        if hdr:
            try:
                b = Bundle(path)
            except BundleError as e:
                report["notes"].append("%s looks like a bundle but cannot be read: %s" % (rel, e))
                continue
            info = {
                "path": rel, "format": fmt, "arch": arch,
                "version": "%d.%d" % (b.major, b.minor), "files": len(b.entries),
                "compressed": b.compressed,
                "content_size": sum(e.size for e in b.entries),
                "entries_by_type": {},
            }
            for e in b.entries:
                info["entries_by_type"][e.type_name] = info["entries_by_type"].get(e.type_name, 0) + 1
            for e in b.entries:
                lp = e.path.lower()
                ebase = os.path.basename(e.path)
                if lp.endswith(".runtimeconfig.json"):
                    info["runtimeconfig"] = e.path
                    try:
                        info["runtimeconfig_json"] = json.loads(b.read(e).decode("utf-8-sig"))
                    except ValueError:
                        pass
                elif lp.endswith(".deps.json"):
                    info["deps_json"] = e.path
                elif e.type in (0, 1, 5) and lp.endswith((".dll", ".exe")):
                    if _looks_like_framework(ebase):
                        managed_sources.append(ManagedSource("%s!/%s" % (rel, e.path), None, e.size))
                    else:
                        managed_sources.append(ManagedSource(
                            "%s!/%s" % (rel, e.path), (lambda b=b, e=e: b.read(e)), e.size))
                elif e.type == 2 or NATIVE_FILE_RE.search(ebase):
                    if ebase in RUNTIME_NATIVE_FILES:
                        continue
                    report["native_libs"].append({"path": "%s!/%s" % (rel, e.path),
                                                  "format": "bundled", "arch": arch})
            report["bundles"].append(info)
            continue
        if hdr == 0:
            report["apphosts"].append({"path": rel, "format": fmt, "arch": arch})
            continue
        is_exe_like = not NATIVE_FILE_RE.search(base) or lower.endswith(".exe")
        if is_exe_like and size > 64 * 1024 and file_contains(path, NATIVEAOT_MARKERS):
            report["nativeaot"].append({"path": rel, "format": fmt, "arch": arch, "size": size})
            continue
        report["native_libs"].append({"path": rel, "format": fmt, "arch": arch})

    report["content_dirs"] = sorted(top_dirs)

    for item in report["native_libs"]:
        if item["format"] != "elf" or "!/" in item["path"]:
            continue
        key, _ = classify_native(os.path.basename(item["path"]))
        if key in ("theora", "fmod", "steam_api"):
            exports = elf_exports(os.path.join(root, *item["path"].split("/")))
            if exports is not None:
                item["exports"] = len(exports)
                if key == "theora":
                    item["theorafile_api"] = theorafile_api_level(exports)

    # Managed assemblies -------------------------------------------------
    for src in managed_sources:
        if src.loader is None and src._asm is False:
            report["managed"].append({
                "path": src.relpath, "name": os.path.splitext(os.path.basename(src.relpath))[0],
                "version": None, "size": src.size, "framework_assembly": True})
            continue
        asm = src.asm
        if asm is None:
            continue
        name, ver = asm.assembly_name()
        entry = {
            "path": src.relpath,
            "name": name,
            "version": ver,
            "size": src.size,
            "framework_assembly": bool(name) and name.startswith(MANAGED_FRAMEWORK_PREFIXES),
            "runtime": asm.runtime_version,
            "il_only": asm.il_only,
            "requires_32bit": asm.requires_32bit,
            "ready_to_run": asm.ready_to_run,
            "pe_machine": asm.arch,
            "cpu_specific": asm.pe32_plus,
        }
        if not entry["framework_assembly"]:
            try:
                entry["target_framework"] = asm.target_framework()
                entry["references"] = asm.assembly_refs()
                entry["pinvoke"] = asm.pinvokes()
                libstrings = sorted(set(s for s in asm.user_strings() if LIB_STRING_RE.match(s)))
                if libstrings:
                    entry["library_strings"] = libstrings
                fmodv = asm.const_value("FMOD", "VERSION", "number")
                if isinstance(fmodv, int):
                    entry["fmod_header_version"] = fmod_version_str(fmodv)
                types = set(asm.type_defs())
                refs_types = set(asm.type_refs())
                flavor = monogame_flavor(name, types, refs_types, entry["references"])
                if flavor:
                    entry["monogame_flavor"] = flavor
                if ("Steamworks", "SteamAPI") in types:
                    entry["steamworks_wrapper"] = "Steamworks.NET"
                elif ("Steamworks", "SteamClient") in types:
                    entry["steamworks_wrapper"] = "Facepunch.Steamworks"
                tf = asm.native_binding_arity("tf_videoinfo")
                if tf:
                    entry["theorafile_binding"] = tf
            except (struct.error, ValueError, IndexError, KeyError) as e:
                entry["error"] = "metadata: %s" % e
        report["managed"].append(entry)

    _summarise(report)
    return report


def monogame_flavor(name, types, typerefs, refs):
    ref_names = set(r[0] for r in refs)
    if name not in ("MonoGame.Framework", "FNA") and not any(
            n in ("MonoGame.Framework", "FNA") for n in ref_names):
        return None
    if name == "FNA" or "FNA" in ref_names:
        return "FNA"
    if name != "MonoGame.Framework":
        return None
    if any(n.startswith("SharpDX") for n in ref_names):
        return "WindowsDX"
    namespaces = set(t[0] for t in types)
    if any(t[1] in ("SdlGamePlatform", "SdlGameWindow") for t in types) or "Sdl" in set(t[1] for t in types):
        return "DesktopGL"
    if any(ns.startswith("Android") for ns in namespaces) or any(r.startswith("Mono.Android") for r in ref_names):
        return "Android"
    if any(r in ("Xamarin.iOS", "Microsoft.iOS") for r in ref_names):
        return "iOS"
    return "unknown"


def _summarise(report):
    """Derive the high level verdict from the raw findings."""
    managed = report["managed"]
    app = [m for m in managed if not m["framework_assembly"]]

    # Main assembly: whatever a runtimeconfig/bundle names, else the biggest
    # assembly that references MonoGame/FNA. Loose files win over bundled ones.
    app.sort(key=lambda m: ("!/" in m["path"], m["path"].count("/"), m["path"]))
    main = None
    candidates = []
    for rc in report["runtimeconfigs"]:
        candidates.append(os.path.basename(rc)[:-len(".runtimeconfig.json")])
    for b in report["bundles"]:
        if b.get("runtimeconfig"):
            candidates.append(os.path.basename(b["runtimeconfig"])[:-len(".runtimeconfig.json")])
    for c in candidates:
        for m in app:
            if os.path.basename(m["path"]).lower() in (c.lower() + ".dll", c.lower() + ".exe"):
                main = m
                break
        if main:
            break
    if main is None:
        game = [m for m in app if any(r[0] in ("MonoGame.Framework", "FNA")
                                      for r in m.get("references", []))]
        game.sort(key=lambda m: ("!/" in m["path"], -m["size"]))
        if game:
            main = game[0]
    report["main_assembly"] = main["path"] if main else None
    by_name = {}
    for m in reversed(app):
        if m.get("name"):
            by_name[m["name"]] = m

    tfm = main.get("target_framework") if main else None
    report["target_framework"] = tfm
    ref_names = set(r[0] for r in (main or {}).get("references", []))
    if tfm and tfm.startswith(".NETCoreApp"):
        report["runtime_kind"] = "dotnet"
    elif tfm and tfm.startswith(".NETFramework"):
        report["runtime_kind"] = "mono"
    elif main and "mscorlib" in ref_names and "System.Runtime" not in ref_names \
            and "System.Private.CoreLib" not in ref_names:
        report["runtime_kind"] = "mono"
    elif main:
        report["runtime_kind"] = "dotnet"
    else:
        report["runtime_kind"] = None

    mg = by_name.get("MonoGame.Framework") or by_name.get("FNA")
    report["engine"] = None
    if mg:
        report["engine"] = {"name": mg["name"], "version": mg["version"],
                            "flavor": mg.get("monogame_flavor"), "path": mg["path"]}
    elif main:
        for r in main.get("references", []):
            if r[0] in ("MonoGame.Framework", "FNA"):
                report["engine"] = {"name": r[0], "version": r[1], "flavor": None, "path": None}

    report["theorafile"] = None
    binding = {}
    for m in app:
        binding.update(m.get("theorafile_binding", {}))
    libs = [n for n in report["native_libs"] if n.get("theorafile_api")]
    if binding or libs:
        counts = sorted(set(binding.values()))
        vi = counts[-1] if counts else None
        report["theorafile"] = {
            "binding": binding,
            "videoinfo_params": vi,
            "videoinfo_api": {4: "pre-2020 (no pixel format)", 5: "2020+ (pixel format)"}.get(vi),
            "shipped_library_api": libs[0]["theorafile_api"] if libs else None,
        }

    fmod = sorted(set(m["fmod_header_version"] for m in app if m.get("fmod_header_version")))
    report["fmod_header_version"] = fmod[0] if fmod else None

    needs = {}
    for m in app:
        for lib, funcs in sorted(m.get("pinvoke", {}).items()):
            key, desc = classify_native(lib)
            n = needs.setdefault(key or lib, {"description": desc or "?", "names": set(),
                                               "used_by": set(), "functions": 0})
            n["names"].add(lib)
            n["used_by"].add(m["name"])
            n["functions"] += len(funcs)
        for s in m.get("library_strings", []):
            key, desc = classify_native(s)
            if key:
                n = needs.setdefault(key, {"description": desc, "names": set(),
                                           "used_by": set(), "functions": 0})
                n["names"].add(s)
                n["used_by"].add(m["name"])
    for n in needs.values():
        n["names"] = sorted(n["names"])
        n["used_by"] = sorted(n["used_by"])
    report["native_needs"] = needs

    archs = set()
    for item in report["native_libs"] + report["runtime_files"] + report["apphosts"] + report["bundles"]:
        a = item.get("arch")
        if isinstance(a, list):
            archs.update(a)
        elif a:
            archs.add(a)
    report["native_archs"] = sorted(archs)

    # Verdict ---------------------------------------------------------------
    verdict = []
    ok = True
    if report["nativeaot"] and not main:
        ok = False
        verdict.append("NativeAOT build: the game code was compiled to %s machine code and "
                       "no IL is left. This build cannot be retargeted; use the Linux, macOS "
                       "or Windows build of another store/version that still ships IL "
                       "assemblies." % ", ".join(sorted(set(str(x["arch"]) for x in report["nativeaot"]))))
    if not main and not report["nativeaot"]:
        ok = False
        verdict.append("No game assembly found. Copy the complete game folder.")
    if main:
        if not main.get("il_only", True):
            ok = False
            verdict.append("%s is a mixed-mode (C++/CLI) assembly and cannot run on aarch64." % main["name"])
        if main.get("requires_32bit"):
            verdict.append("%s is flagged 32-bit-required; the flag is ignored on a 64-bit "
                           "runtime but may hint at native x86 dependencies." % main["name"])
    if report["engine"] and report["engine"].get("flavor") == "WindowsDX":
        ok = False
        verdict.append("MonoGame WindowsDX build: shaders are compiled for Direct3D and the "
                       "framework needs SharpDX. Use the Linux or macOS (DesktopGL) build instead.")
    mixed = [m["name"] for m in app if not m.get("il_only", True)]
    if mixed:
        verdict.append("Mixed-mode assemblies present: %s" % ", ".join(mixed))
    tied = ["%s (%s)" % (os.path.basename(m["path"]), m["pe_machine"]) for m in app
            if m.get("cpu_specific") and m.get("il_only", True) and "!/" not in m["path"]]
    if tied:
        verdict.append("IL assemblies stamped for one CPU, which another CPU's runtime refuses to "
                       "load: %s. `prepare` retargets them." % ", ".join(sorted(tied)))
    if ok and main:
        verdict.append("IL game code found (%s, %s). It can run on an aarch64 %s runtime "
                       "once the x86_64 packaging is removed (`ypport.py prepare`)."
                       % (main["name"], tfm or main.get("runtime"),
                          ".NET" if report["runtime_kind"] == "dotnet" else "Mono"))
    report["portable"] = ok and main is not None
    report["verdict"] = verdict


def print_report(r, out=sys.stdout):
    w = out.write
    w("ypport inspect - %s\n" % r["root"])
    w("=" * 72 + "\n")
    w("Files: %d, %s\n" % (r["file_count"], human(r["total_size"])))
    if r["content_dirs"]:
        w("Top-level folders: %s\n" % ", ".join(r["content_dirs"][:20]))
    w("Native architectures seen: %s\n" % (", ".join(r["native_archs"]) or "-"))
    w("\nPackaging\n---------\n")
    for b in r["bundles"]:
        w("  single-file bundle: %s (%s %s, bundle v%s, %d files, %s%s)\n" % (
            b["path"], b["format"], b["arch"], b["version"], b["files"], human(b["content_size"]),
            ", compressed" if b["compressed"] else ""))
        w("    entries: %s\n" % ", ".join("%s=%d" % kv for kv in sorted(b["entries_by_type"].items())))
        rc = b.get("runtimeconfig_json", {}).get("runtimeOptions", {})
        if rc:
            w("    runtimeconfig: %s\n" % json.dumps(rc.get("includedFrameworks") or rc.get("framework") or rc.get("frameworks")))
    for a in r["apphosts"]:
        w("  apphost: %s (%s %s)\n" % (a["path"], a["format"], a["arch"]))
    for a in r["nativeaot"]:
        w("  NativeAOT executable: %s (%s %s, %s)\n" % (a["path"], a["format"], a["arch"], human(a["size"])))
    for rc in r["runtimeconfigs"]:
        w("  runtimeconfig: %s\n" % rc)
    if r["runtime_files"]:
        w("  bundled .NET runtime files: %d (%s)\n" % (
            len(r["runtime_files"]), ", ".join(sorted(set(str(x["arch"]) for x in r["runtime_files"])))))
    if r["mono_runtime_files"]:
        w("  bundled Mono runtime files: %s\n" % ", ".join(x["path"] for x in r["mono_runtime_files"]))
    w("\nGame code\n---------\n")
    w("  main assembly: %s\n" % r["main_assembly"])
    w("  target framework: %s (runtime: %s)\n" % (r["target_framework"], r["runtime_kind"]))
    if r["engine"]:
        e = r["engine"]
        w("  engine: %s %s, flavor %s\n" % (e["name"], e["version"], e["flavor"]))
    if r["fmod_header_version"]:
        w("  FMOD C# wrapper header version: %s\n" % r["fmod_header_version"])
    if r.get("theorafile"):
        t = r["theorafile"]
        if t["videoinfo_params"] is not None:
            binding = "tf_videoinfo takes %d params -> %s" % (t["videoinfo_params"], t["videoinfo_api"])
        else:
            binding = "not identified in metadata"
        w("  Theorafile (video): binding %s; shipped library API: %s\n" % (
            binding, t["shipped_library_api"] or "-"))
        for where, n in sorted(t["binding"].items()):
            w("    %s: %d params\n" % (where, n))
    app = [m for m in r["managed"] if not m["framework_assembly"]]
    fw = [m for m in r["managed"] if m["framework_assembly"]]
    w("  app assemblies (%d):\n" % len(app))
    for m in sorted(app, key=lambda m: m["path"]):
        flags = []
        if m.get("ready_to_run"):
            flags.append("R2R")
        elif m.get("cpu_specific"):
            flags.append("%s-only" % m.get("pe_machine"))
        if not m.get("il_only", True):
            flags.append("MIXED-MODE")
        if m.get("steamworks_wrapper"):
            flags.append(m["steamworks_wrapper"])
        if m.get("monogame_flavor"):
            flags.append("MonoGame " + m["monogame_flavor"])
        w("    %-48s %-12s %-22s %s\n" % (m["path"][-48:], m["version"], (m.get("target_framework") or "")[:22],
                                         " ".join(flags)))
    w("  framework assemblies: %d\n" % len(fw))
    w("\nNative libraries the code asks for\n----------------------------------\n")
    for key, n in sorted(r["native_needs"].items()):
        w("  %-12s %-28s %s  (used by %s%s)\n" % (
            key, n["description"], ", ".join(n["names"]), ", ".join(n["used_by"]),
            (", %d entry points" % n["functions"]) if n["functions"] else ""))
    w("\nNative libraries shipped\n------------------------\n")
    for n in r["native_libs"]:
        key, _ = classify_native(os.path.basename(n["path"]))
        extra = (" (%d exports)" % n["exports"]) if n.get("exports") else ""
        w("  %-60s %-6s %-8s %s%s\n" % (n["path"][-60:], n["format"], n["arch"], key or "", extra))
    if r.get("videos"):
        vids = r["videos"]
        sizes = {}
        for v in vids:
            key = "%dx%d %s" % (v["width"], v["height"], v["pixel_format"])
            sizes[key] = sizes.get(key, 0) + 1
        w("\nVideos (Ogg Theora)\n-------------------\n")
        w("  %d files, %s, %s of video; %s\n" % (
            len(vids), human(sum(v["size"] for v in vids)), _fmt_duration(sum(v["duration"] or 0 for v in vids)),
            ", ".join("%s x%d" % kv for kv in sorted(sizes.items()))))
    for note in r["notes"]:
        w("\nnote: %s\n" % note)
    w("\nVerdict\n-------\n")
    for v in r["verdict"]:
        w("  * %s\n" % v)
    w("\n")


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    return str(o)


# --------------------------------------------------------------------------
# Texture survey (how much memory the game's images need once decoded)
# --------------------------------------------------------------------------

# Bytes per pixel on the GPU for MonoGame SurfaceFormat values.
SURFACE_BPP = {0: 4, 1: 2, 2: 2, 3: 2, 4: 0.5, 5: 1, 6: 1, 7: 2, 8: 4, 9: 4, 10: 4, 11: 8,
               12: 1, 13: 4, 14: 8, 15: 16, 16: 2, 17: 4, 18: 8, 19: 8, 20: 4, 21: 4,
               60: 0.5, 61: 0.5, 62: 1, 63: 1, 64: 0.5, 65: 1, 66: 1}
SURFACE_NAMES = {0: "Color", 1: "Bgr565", 2: "Bgra5551", 3: "Bgra4444", 4: "Dxt1", 5: "Dxt3",
                 6: "Dxt5", 12: "Alpha8", 20: "Bgr32", 21: "Bgra32"}
COMPRESSED_FORMATS = (4, 5, 6, 60, 61, 62, 63, 64, 65, 66)


def lz4_block_prefix(src, want):
    """Decode the first `want` bytes of an LZ4 block (enough for headers)."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < want:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n or len(out) >= want:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(out) - offset
        if offset == 0 or start < 0:
            raise ValueError("corrupt LZ4 data")
        for k in range(length):
            out.append(out[start + k])
    return bytes(out[:want])


def lz4_block_decompress(src, size):
    """Decode a complete LZ4 block of known output size (C-speed slices)."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(out) - offset
        if offset == 0 or start < 0:
            raise ValueError("corrupt LZ4 data")
        if offset >= length:
            out += out[start:start + length]
        else:
            pattern = bytes(out[start:])
            out += (pattern * (length // offset + 1))[:length]
    if len(out) != size:
        raise ValueError("LZ4 data decoded to %d bytes, expected %d" % (len(out), size))
    return bytes(out)


_RUN_RE = re.compile(br"(?s)(.)\1{15,}")


def lz4_block_compress_runs(data):
    """Encode data as an LZ4 block that only compresses runs of a repeated
    byte (offset-1 matches). Fast in pure Python and enough for textures,
    whose transparent areas are long runs of zeros."""
    out = bytearray()
    n = len(data)
    limit = n - 12  # LZ4: the last match must start 12 bytes before the end

    def length_bytes(value):
        b = bytearray()
        while value >= 255:
            b.append(255)
            value -= 255
        b.append(value)
        return b

    pos = 0
    for m in _RUN_RE.finditer(data):
        start = m.start() + 1  # the run's first byte goes out as a literal
        end = min(m.end(), limit)
        length = end - start
        if start >= limit or length < 4:
            continue
        lit = start - pos
        token = (min(lit, 15) << 4) | min(length - 4, 15)
        out.append(token)
        if lit >= 15:
            out += length_bytes(lit - 15)
        out += data[pos:start]
        out += b"\x01\x00"
        if length - 4 >= 15:
            out += length_bytes(length - 4 - 15)
        pos = end
    lit = n - pos
    out.append(min(lit, 15) << 4)
    if lit >= 15:
        out += length_bytes(lit - 15)
    out += data[pos:]
    return bytes(out)


class XnbFile(object):
    """An XNB container: header fields plus the decompressed payload."""

    def __init__(self, path):
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) < 10 or raw[:3] != b"XNB":
            raise ValueError("not an XNB file")
        self.platform = raw[3:4]
        self.version = raw[4]
        self.flags = raw[5]
        if self.flags & 0x80:
            raise ValueError("LZX-compressed XNB files are not supported")
        if self.flags & 0x40:
            size = struct.unpack_from("<I", raw, 10)[0]
            self.payload = lz4_block_decompress(raw[14:], size)
        else:
            self.payload = raw[10:]

    def to_bytes(self, compress=True):
        flags = self.flags & ~0xC0
        if compress:
            body = struct.pack("<I", len(self.payload)) + lz4_block_compress_runs(self.payload)
            flags |= 0x40
        else:
            body = self.payload
        return (b"XNB" + self.platform + bytes([self.version, flags]) +
                struct.pack("<I", 10 + len(body)) + body)

    def texture(self):
        """Texture2D layout: (type_id_end, format, width, height, levels with
        (offset, size) of each level's data) or None for other content."""
        data = self.payload
        count, p = _read_7bit(data, 0)
        readers = []
        for _ in range(count):
            n, p = _read_7bit(data, p)
            readers.append(data[p:p + n].decode("utf-8", "replace"))
            p += n + 4
        _shared, p = _read_7bit(data, p)
        type_id, p = _read_7bit(data, p)
        if not 0 < type_id <= len(readers) or "Texture2DReader" not in readers[type_id - 1]:
            return None
        fmt, width, height, levels = struct.unpack_from("<iIII", data, p)
        q = p + 16
        level_data = []
        for _ in range(levels):
            size = struct.unpack_from("<I", data, q)[0]
            level_data.append((q + 4, size))
            q += 4 + size
        return {"header_end": p, "format": fmt, "width": width, "height": height,
                "levels": level_data, "end": q}


def _channel_table(fn):
    return bytes(bytearray(fn(v) & 0xFF for v in range(256)))


_R5_HI = _channel_table(lambda v: ((v * 31 + 127) // 255) << 2)          # bits 2-6 of high byte
_G5_HI = _channel_table(lambda v: ((v * 31 + 127) // 255) >> 3)          # bits 0-1 of high byte
_G5_LO = _channel_table(lambda v: (((v * 31 + 127) // 255) & 7) << 5)    # bits 5-7 of low byte
_B5_LO = _channel_table(lambda v: (v * 31 + 127) // 255)                 # bits 0-4 of low byte
_A1_HI = _channel_table(lambda v: 0x80 if v >= 128 else 0)
_HI4 = _channel_table(lambda v: ((v * 15 + 127) // 255) << 4)
_LO4 = _channel_table(lambda v: (v * 15 + 127) // 255)


def _or_bytes(*parts):
    acc = 0
    for part in parts:
        acc |= int.from_bytes(part, "little")
    return acc.to_bytes(len(parts[0]), "little")


def _interleave(lo, hi):
    out = bytearray(len(lo) * 2)
    out[0::2] = lo
    out[1::2] = hi
    return bytes(out)


def rgba_alpha_is_binary(rgba):
    return not rgba[3::4].translate(None, b"\x00\xff")


_NEAR_BINARY_ALPHA = bytes(bytearray(range(0, 16))) + bytes(bytearray(range(240, 256)))


def alpha_class(rgba):
    """('binary', 0) when alpha is only 0 or 255; ('near', share) when the
    other values all lie within 15 of either end (anti-aliasing fringes that
    1-bit alpha reproduces invisibly); otherwise ('soft', share). share is the
    fraction of pixels that are neither fully transparent nor opaque."""
    a = rgba[3::4]
    rest = a.translate(None, b"\x00\xff")
    if not rest:
        return "binary", 0.0
    share = len(rest) / float(len(a))
    if not rest.translate(None, _NEAR_BINARY_ALPHA):
        return "near", share
    return "soft", share


def texture_format_for(classes, mode):
    """Target SurfaceFormat (2 = Bgra5551, 3 = Bgra4444) or None to keep Color."""
    if mode == "full":
        return None
    if all(c in ("binary", "near") for c in classes):
        return 2
    return 3 if mode == "16bit" else None


_PREVIEW_5 = _channel_table(lambda v: (((v * 31 + 127) // 255) << 3) | (((v * 31 + 127) // 255) >> 2))
_PREVIEW_4 = _channel_table(lambda v: ((v * 15 + 127) // 255) * 17)
_PREVIEW_A1 = _channel_table(lambda v: 255 if v >= 128 else 0)


def preview_16bit(rgba, fmt):
    """What a Color image looks like on the GPU after conversion to fmt."""
    out = bytearray(rgba)
    if fmt == 2:
        for c in range(3):
            out[c::4] = rgba[c::4].translate(_PREVIEW_5)
        out[3::4] = rgba[3::4].translate(_PREVIEW_A1)
    elif fmt == 3:
        for c in range(4):
            out[c::4] = rgba[c::4].translate(_PREVIEW_4)
    return bytes(out)


def write_png(path, width, height, rgba):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    stride = width * 4
    raw = b"".join(b"\0" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def rgba_to_bgra5551(rgba):
    """SurfaceFormat.Color (R,G,B,A bytes) -> XNA Bgra5551 (A<<15|R<<10|G<<5|B, LE)."""
    r, g, b, a = rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4]
    lo = _or_bytes(g.translate(_G5_LO), b.translate(_B5_LO))
    hi = _or_bytes(a.translate(_A1_HI), r.translate(_R5_HI), g.translate(_G5_HI))
    return _interleave(lo, hi)


def rgba_to_bgra4444(rgba):
    """SurfaceFormat.Color -> XNA Bgra4444 (A<<12|R<<8|G<<4|B, LE)."""
    r, g, b, a = rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4]
    lo = _or_bytes(g.translate(_HI4), b.translate(_LO4))
    hi = _or_bytes(a.translate(_HI4), r.translate(_LO4))
    return _interleave(lo, hi)


TEXTURE_MODES = ("full", "auto16", "16bit")


def convert_texture_xnb(xnb, mode):
    """Rewrite a Color texture to a 16-bit format. auto16: only textures whose
    alpha is (nearly) 0 or 255 everywhere, to Bgra5551. 16bit: additionally
    everything else to Bgra4444. Returns a description or None."""
    tex = xnb.texture()
    if tex is None or tex["format"] != 0 or mode == "full":
        return None
    data = xnb.payload
    levels = [data[off:off + size] for off, size in tex["levels"]]
    classes = [alpha_class(level) for level in levels]
    fmt = texture_format_for([c for c, _share in classes], mode)
    if fmt is None:
        return None
    convert, name = (rgba_to_bgra5551, "Bgra5551") if fmt == 2 else (rgba_to_bgra4444, "Bgra4444")
    name += ", alpha %s" % classes[0][0]
    parts = [data[:tex["header_end"]],
             struct.pack("<iIII", fmt, tex["width"], tex["height"], len(levels))]
    for level in levels:
        new = convert(level)
        parts.append(struct.pack("<I", len(new)))
        parts.append(new)
    parts.append(data[tex["end"]:])
    xnb.payload = b"".join(parts)
    return "%dx%d Color -> %s" % (tex["width"], tex["height"], name)


def _read_7bit(data, p):
    value = shift = 0
    while True:
        b = data[p]
        p += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, p
        shift += 7


def xnb_texture_info(path):
    """(width, height, surface_format, levels) of an XNB Texture2D, or a
    string explaining why it could not be read."""
    with open(path, "rb") as f:
        head = f.read(14)
        if len(head) < 10 or head[:3] != b"XNB":
            return "not an XNB file"
        flags = head[5]
        if flags & 0x80:
            return "LZX-compressed"
        if flags & 0x40:
            f.seek(14)
            data = lz4_block_prefix(f.read(4096), 1024)
        else:
            f.seek(10)
            data = f.read(1024)
    try:
        count, p = _read_7bit(data, 0)
        readers = []
        for _ in range(count):
            n, p = _read_7bit(data, p)
            readers.append(data[p:p + n].decode("utf-8", "replace"))
            p += n + 4
        _shared, p = _read_7bit(data, p)
        type_id, p = _read_7bit(data, p)
        if not 0 < type_id <= len(readers) or "Texture2DReader" not in readers[type_id - 1]:
            return "not a texture (%s)" % (readers[type_id - 1].split(",")[0] if 0 < type_id <= len(readers) else "?")
        fmt, width, height, levels = struct.unpack_from("<iIII", data, p)
        return width, height, fmt, levels
    except (IndexError, struct.error):
        return "unreadable header"


def image_size(path):
    """(width, height) of a PNG/JPEG/BMP/GIF/DDS file, or None."""
    with open(path, "rb") as f:
        head = f.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:2] == b"BM" and len(head) >= 26:
            w, h = struct.unpack_from("<ii", head, 18)
            return abs(w), abs(h)
        if head[:4] in (b"GIF8",):
            return struct.unpack_from("<HH", head, 6)
        if head[:4] == b"DDS " and len(head) >= 20:
            h, w = struct.unpack_from("<II", head, 12)
            return w, h
        if head[:2] == b"\xff\xd8":
            f.seek(2)
            while True:
                marker = f.read(4)
                if len(marker) < 4 or marker[0] != 0xFF:
                    return None
                code, length = marker[1], struct.unpack(">H", marker[2:4])[0]
                if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">xHH", f.read(5))
                    return w, h
                f.seek(length - 2, 1)
    return None


def survey_textures(root, top=20, alpha=False):
    root = os.path.abspath(root)
    images = []
    projected = {"auto16": 0, "16bit": 0, "binary": 0, "near": 0, "soft": 0, "textures": []}
    other = {}
    problems = {}
    for path in _walk(root):
        rel = _rel(root, path)
        lower = rel.lower()
        ext = os.path.splitext(lower)[1]
        size = os.path.getsize(path)
        kind = None
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".dds"):
            try:
                dims = image_size(path)
            except (IOError, OSError, struct.error):
                dims = None
            if dims:
                w, h = dims
                images.append((w * h * 4, w, h, "RGBA (decoded %s)" % ext[1:], rel, size))
                continue
            kind = "unreadable images"
        elif ext == ".xnb":
            info = xnb_texture_info(path)
            if isinstance(info, tuple):
                w, h, fmt, levels = info
                bpp = SURFACE_BPP.get(fmt, 4)
                gpu = int(w * h * bpp * (4.0 / 3 if levels > 1 else 1))
                name = SURFACE_NAMES.get(fmt, "format %d" % fmt)
                if fmt in COMPRESSED_FORMATS:
                    name += " (compressed)"
                images.append((gpu, w, h, name, rel, size))
                if fmt == 0:
                    cls = None
                    if alpha:
                        try:
                            xnb = XnbFile(path)
                            tex = xnb.texture()
                            classes = [alpha_class(xnb.payload[o:o + n]) for o, n in tex["levels"]]
                            cls = classes[0][0] if all(c == classes[0][0] for c, _ in classes) else "soft"
                            projected["textures"].append((rel, cls, max(sh for _c, sh in classes)))
                        except (ValueError, IndexError, struct.error):
                            cls = None
                    projected[cls or "unknown"] = projected.get(cls or "unknown", 0) + 1
                    for mode in ("auto16", "16bit"):
                        target = texture_format_for([cls or "soft"], mode)
                        projected[mode] += gpu // 2 if target else gpu
                else:
                    projected["auto16"] += gpu
                    projected["16bit"] += gpu
                continue
            problems[info] = problems.get(info, 0) + 1
            kind = "xnb: " + info.split(" (")[0]
        elif ext in (".ogg", ".wav", ".mp3", ".bank", ".fsb", ".fev"):
            kind = "audio"
        elif ext in (".ogv", ".webm", ".mp4", ".bik", ".avi"):
            kind = "video"
        else:
            kind = "other"
        n, total = other.get(kind, (0, 0))
        other[kind] = (n + 1, total + size)

    images.sort(reverse=True)
    total_gpu = sum(i[0] for i in images)
    return {
        "root": root,
        "images": len(images),
        "decoded_bytes": total_gpu,
        "file_bytes": sum(i[5] for i in images),
        "largest": images[:top],
        "over_2048": sum(1 for i in images if max(i[1], i[2]) > 2048),
        "over_4096": sum(1 for i in images if max(i[1], i[2]) > 4096),
        "over_8192": sum(1 for i in images if max(i[1], i[2]) > 8192),
        "by_dir": _sum_by_dir(images),
        "formats": _count_by(images, 3),
        "other": other,
        "projected": projected if alpha else None,
    }


def _sum_by_dir(images):
    out = {}
    for gpu, _w, _h, _fmt, rel, _size in images:
        top = rel.split("/")[0] if "/" in rel else "."
        n, total = out.get(top, (0, 0))
        out[top] = (n + 1, total + gpu)
    return out


def _count_by(images, index):
    out = {}
    for item in images:
        out[item[index]] = out.get(item[index], 0) + 1
    return out


def print_textures(r, out=sys.stdout):
    w = out.write
    w("ypport textures - %s\n" % r["root"])
    w("=" * 72 + "\n")
    w("Images: %d files, %s on disk, %s once decoded for the GPU\n" % (
        r["images"], human(r["file_bytes"]), human(r["decoded_bytes"])))
    w("Larger than 2048 px: %d, than 4096 px: %d, than 8192 px: %d\n" % (
        r["over_2048"], r["over_4096"], r["over_8192"]))
    w("\nBy top-level folder (decoded size):\n")
    for d, (n, total) in sorted(r["by_dir"].items(), key=lambda kv: -kv[1][1]):
        w("  %-30s %6d images  %10s\n" % (d, n, human(total)))
    w("\nFormats:\n")
    for fmt, n in sorted(r["formats"].items(), key=lambda kv: -kv[1]):
        w("  %-36s %6d\n" % (fmt, n))
    p = r.get("projected")
    if p:
        w("\nAlpha of the Color textures: %d binary (0/255), %d nearly binary (only within 15 of"
          " 0/255), %d soft\n" % (p["binary"], p["near"], p["soft"]))
        w("Texture memory with --textures auto16: %s, with --textures 16bit: %s\n" % (
            human(p["auto16"]), human(p["16bit"])))
        w("\nPer texture (alpha class, share of semi-transparent pixels, format with 16bit):\n")
        for rel, cls, share in sorted(p["textures"]):
            target = texture_format_for([cls], "16bit")
            w("  %-48s %-7s %6.2f%%  %s\n" % (rel[-48:], cls, share * 100,
                                             {2: "Bgra5551", 3: "Bgra4444"}.get(target, "Color")))
    w("\nLargest images (decoded size, dimensions, file):\n")
    for gpu, width, height, fmt, rel, _size in r["largest"]:
        w("  %10s  %5dx%-5d  %s\n" % (human(gpu), width, height, rel))
    if r["other"]:
        w("\nOther files:\n")
        for kind, (n, total) in sorted(r["other"].items(), key=lambda kv: -kv[1][1]):
            w("  %-36s %6d files  %10s\n" % (kind, n, human(total)))
    w("\n")


# --------------------------------------------------------------------------
# Videos (Ogg Theora cutscenes)
# --------------------------------------------------------------------------

VIDEO_EXTS = (".ogv", ".ogg")
VIDEO_TOOL = "ogvshrink"


def _ogg_pages(data):
    """Complete Ogg pages in `data`: (header_type, granulepos, serial, lacing, body)."""
    p = data.find(b"OggS")
    while 0 <= p and p + 27 <= len(data):
        if data[p + 4] != 0:
            p = data.find(b"OggS", p + 1)
            continue
        htype = data[p + 5]
        gp, serial = struct.unpack_from("<qI", data, p + 6)
        nseg = data[p + 26]
        body = p + 27 + nseg
        lacing = data[p + 27:body]
        end = body + sum(bytearray(lacing))
        if len(lacing) < nseg or end > len(data):
            return
        yield htype, gp, serial, lacing, data[body:end]
        p = data.find(b"OggS", end)


def _first_packet(lacing, body):
    n = 0
    for v in bytearray(lacing):
        n += v
        if v < 255:
            break
    return body[:n]


def ogg_video_info(path):
    """Header facts of an Ogg Theora file (picture size, frame rate, pixel
    format, frames, audio), or None if it has no Theora stream."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(65536)
        f.seek(max(0, size - 262144))
        tail = f.read()
    if head[:4] != b"OggS":
        return None
    theora = None
    audio = []
    for htype, _gp, serial, lacing, body in _ogg_pages(head):
        if not htype & 2:
            break
        pkt = _first_packet(lacing, body)
        if pkt[:7] == b"\x80theora" and len(pkt) >= 42 and theora is None:
            vrev = pkt[9]
            fn, fd = struct.unpack_from(">II", pkt, 22)
            bits = struct.unpack_from(">H", pkt, 40)[0]
            theora = {
                "serial": serial,
                "width": int.from_bytes(pkt[14:17], "big"),
                "height": int.from_bytes(pkt[17:20], "big"),
                "frame_width": struct.unpack_from(">H", pkt, 10)[0] * 16,
                "frame_height": struct.unpack_from(">H", pkt, 12)[0] * 16,
                "fps": float(fn) / fd if fd else 0.0,
                "pixel_format": {0: "420", 2: "422", 3: "444"}.get((bits >> 3) & 3, "?"),
                "bitrate": int.from_bytes(pkt[37:40], "big"),
                "_shift": (bits >> 5) & 31,
                "_vrev": vrev,
            }
        elif pkt[:7] == b"\x01vorbis" and len(pkt) >= 16:
            audio.append({"channels": pkt[11], "rate": struct.unpack_from("<I", pkt, 12)[0]})
    if theora is None:
        return None
    last = -1
    for _htype, gp, serial, _lacing, _body in _ogg_pages(tail):
        if serial == theora["serial"] and gp > last:
            last = gp
    shift = theora.pop("_shift")
    vrev = theora.pop("_vrev")
    del theora["serial"]
    frames = None
    if last >= 0:
        iframe = last >> shift
        frames = iframe + (last - (iframe << shift)) + (1 if vrev == 0 else 0)
    theora["frames"] = frames
    theora["duration"] = frames / theora["fps"] if frames is not None and theora["fps"] else None
    theora["audio"] = audio
    theora["size"] = size
    return theora


def video_fit(width, height, box):
    """Size ogvshrink scales a width x height picture to so that it fits
    box ("WxH"), or None if it already (nearly) does."""
    bw, bh = (int(v) for v in box.split("x"))
    scale = min(float(bw) / width, float(bh) / height, 1.0)
    w = max(16, int(width * scale + 0.5) & ~1)
    h = max(16, int(height * scale + 0.5) & ~1)
    if w >= width * 0.9:
        return None
    return w, h


def find_videos(root):
    root = os.path.abspath(root)
    out = []
    for path in _walk(root):
        if not path.lower().endswith(VIDEO_EXTS):
            continue
        try:
            info = ogg_video_info(path)
        except (OSError, IOError, struct.error, ValueError):
            info = None
        if info is not None:
            info["path"] = _rel(root, path)
            out.append(info)
    return out


def _host_arch():
    machine = os.uname()[4].lower()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)


def find_video_tool(arch, dirs=()):
    here = os.path.dirname(os.path.abspath(__file__))
    for d in list(dirs) + [os.path.join(here, "bin." + arch)]:
        path = os.path.join(d, VIDEO_TOOL)
        if os.path.isfile(path) and sniff(path) == ("elf", arch):
            if not os.access(path, os.X_OK):
                try:
                    os.chmod(path, 0o755)
                except OSError:
                    pass
            return path
    return None


def parse_tool_output(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def run_jobs(commands, jobs, done):
    """Runs commands (lists) with up to `jobs` at a time; calls
    done(index, returncode, stdout, stderr) as each one finishes."""
    pending = list(enumerate(commands))
    running = []
    while pending or running:
        while pending and len(running) < max(1, jobs):
            i, cmd = pending.pop(0)
            running.append((i, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)))
        time.sleep(0.2)
        for item in list(running):
            i, proc = item
            if proc.poll() is not None:
                out, err = proc.communicate()
                running.remove(item)
                done(i, proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _fmt_duration(seconds):
    if seconds is None:
        return "?"
    return "%d:%02d" % (int(seconds) // 60, int(seconds) % 60)


def print_videos(videos, root, bench=None, out=sys.stdout):
    w = out.write
    w("ypport videos - %s\n" % root)
    w("=" * 72 + "\n")
    if not videos:
        w("No Ogg Theora videos found.\n\n")
        return
    for v in videos:
        audio = ", ".join("%dch %d Hz" % (a["channels"], a["rate"]) for a in v["audio"]) or "no audio"
        w("  %s\n    %dx%d %s, %.3f fps, %s frames (%s), %s, %s\n" % (
            v["path"], v["width"], v["height"], v["pixel_format"], v["fps"],
            v["frames"] if v["frames"] is not None else "?", _fmt_duration(v["duration"]),
            human(v["size"]), audio))
        b = (bench or {}).get(v["path"])
        if b:
            if "src_decode_avg_ms" in b:
                w("    decode per frame: avg %s ms, p95 %s ms, max %s ms; %s%% of frames take longer"
                  " than the %.1f ms a frame lasts\n" % (
                      b["src_decode_avg_ms"], b["src_decode_p95_ms"], b["src_decode_max_ms"],
                      b["src_slow_frames_pct"], 1000.0 / v["fps"] if v["fps"] else 0))
            else:
                w("    benchmark failed: %s\n" % (b.get("skip") or b.get("error") or "?"))
    total = sum(v["duration"] or 0 for v in videos)
    w("\n%d videos, %s, %s of video\n\n" % (len(videos), human(sum(v["size"] for v in videos)),
                                           _fmt_duration(total)))


def xnb_video_info(xnb):
    """Video XNB (VideoReader) fields with the payload offsets of width and
    height, or None for other content."""
    data = xnb.payload
    count, p = _read_7bit(data, 0)
    readers = []
    for _ in range(count):
        n, p = _read_7bit(data, p)
        readers.append(data[p:p + n].decode("utf-8", "replace"))
        p += n + 4
    _shared, p = _read_7bit(data, p)
    type_id, p = _read_7bit(data, p)
    if not 0 < type_id <= len(readers) or "VideoReader" not in readers[type_id - 1]:
        return None
    # Each field is written with ReadObject<T>: a type reader index, then the value.
    idx, p = _read_7bit(data, p)
    name = None
    if idx:
        n, p = _read_7bit(data, p)
        name = data[p:p + n].decode("utf-8", "replace")
        p += n
    fields = {"filename": name}
    for key, fmt in (("duration_ms", "<i"), ("width", "<i"), ("height", "<i"), ("fps", "<f"),
                     ("soundtrack", "<i")):
        idx, p = _read_7bit(data, p)
        fields[key + "_offset"] = p
        fields[key] = struct.unpack_from(fmt, data, p)[0]
        p += 4
    return fields


# --------------------------------------------------------------------------
# prepare / restore
# --------------------------------------------------------------------------

class PrepareError(Exception):
    pass


class Preparer(object):
    def __init__(self, gamedir, framework_dir=None, log=print, dry_run=False, native_dirs=(),
                 native_arch="aarch64", textures="full", videos="full", video_tool=None,
                 video_quality=44, jobs=None):
        self.root = os.path.abspath(gamedir)
        self.textures = textures
        self.videos = videos
        self.video_tool = video_tool
        self.video_quality = video_quality
        self.jobs = jobs or min(4, os.cpu_count() or 1)
        self.native_dirs = [d for d in native_dirs if d and os.path.isdir(d)]
        self.native_arch = native_arch
        self.state_dir = os.path.join(self.root, STATE_DIR)
        self.backup_dir = os.path.join(self.state_dir, "backup")
        self.framework_dir = framework_dir
        self.log = log
        self.dry_run = dry_run
        self.state = {"version": VERSION, "extracted": [], "moved": [], "rewritten": [],
                      "created": []}

    def _abs(self, rel):
        return os.path.join(self.root, *rel.split("/")) if rel and rel != "." else self.root

    # -- file operations that can be undone --------------------------------
    def _park(self, rel, reason):
        if rel in self.state["moved"] or not os.path.exists(self._abs(rel)):
            return
        dst = os.path.join(self.backup_dir, *rel.split("/"))
        self.log("  park %-56s (%s)" % (rel, reason))
        if self.dry_run:
            return
        if not os.path.isdir(os.path.dirname(dst)):
            os.makedirs(os.path.dirname(dst))
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(self._abs(rel), dst)
        self.state["moved"].append(rel)
        self._save_state()

    def _rewrite(self, rel, data):
        path = self._abs(rel)
        if self.dry_run:
            self.log("  would rewrite %s" % rel)
            return
        if os.path.exists(path) and rel not in self.state["rewritten"] and rel not in self.state["created"]:
            bak = os.path.join(self.backup_dir, *rel.split("/"))
            if not os.path.isdir(os.path.dirname(bak)):
                os.makedirs(os.path.dirname(bak))
            shutil.copy2(path, bak)
            self.state["rewritten"].append(rel)
        elif not os.path.exists(path):
            self.state["created"].append(rel)
        with open(path + ".yptmp", "w") as f:
            f.write(data)
        os.rename(path + ".yptmp", path)
        self._save_state()
        self.log("  wrote %s" % rel)

    def _save_state(self):
        if self.dry_run:
            return
        if not os.path.isdir(self.state_dir):
            os.makedirs(self.state_dir)
        tmp = os.path.join(self.state_dir, "state.json.yptmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
        os.rename(tmp, os.path.join(self.state_dir, "state.json"))

    def _write_state_file(self, name, text):
        if self.dry_run:
            return
        with open(os.path.join(self.state_dir, name), "w") as f:
            f.write(text)

    # -- main entry --------------------------------------------------------
    def run(self, app_name=None):
        if os.path.exists(os.path.join(self.state_dir, "state.json")):
            raise PrepareError("game folder is already prepared; run `restore` or `reset` first")
        self.log("Inspecting %s ..." % self.root)
        report = inspect_dir(self.root)
        for v in report["verdict"]:
            self.log("  * " + v)

        bundle = self._pick_bundle(report, app_name)
        if bundle is not None:
            self.log("Extracting single-file bundle %s (%d files, %s) ..." % (
                bundle["path"], bundle["files"], human(bundle["content_size"])))
            if self.dry_run:
                self.log("(dry run stops here: the rest depends on the extracted files)")
                return {}
            b = Bundle(self._abs(bundle["path"]))
            self.state["bundle"] = bundle["path"]
            self._save_state()
            # Overwrite: leftovers of an earlier extraction may be stale.
            written = b.extract(self.root, overwrite=True, log=self.log)
            self.state["extracted"].extend(written)
            self._save_state()
            report = inspect_dir(self.root, skip=[bundle["path"]])
            for v in report["verdict"]:
                self.log("  * " + v)

        if not report["portable"]:
            raise PrepareError("this build cannot be run natively:\n  " + "\n  ".join(report["verdict"]))

        main = report["main_assembly"]
        if "!/" in main:
            raise PrepareError("main assembly is still inside a bundle (%s)" % main)
        kind = report["runtime_kind"]
        self.log("Main assembly: %s (%s, %s)" % (main, report["target_framework"], kind))

        # x86_64 .so files next to the game would be found by dlopen() before
        # the aarch64 ones on LD_LIBRARY_PATH, so they go first.
        self._park_foreign_natives(report)
        if bundle is not None:
            self._park(bundle["path"], "single-file host")

        if kind == "dotnet":
            env = self._prepare_dotnet(report, main)
        else:
            env = self._prepare_mono(report, main)
        self._retarget_assemblies(report)
        self._install_replacements()
        self._convert_textures()
        self._shrink_videos()

        engine = report["engine"] or {}
        env.update({
            "YP_ENGINE": engine.get("name") or "",
            "YP_ENGINE_VERSION": engine.get("version") or "",
            "YP_ENGINE_FLAVOR": engine.get("flavor") or "",
            "YP_FMOD_VERSION": report["fmod_header_version"] or "",
            "YP_NATIVE_NEEDS": " ".join(sorted(report["native_needs"])),
            "YP_PREPARED_BY": "ypport %s" % VERSION,
            "YP_TEXTURES": self.textures,
            "YP_VIDEOS": self.videos,
        })
        lines = ["# generated by ypport.py prepare - do not edit"]
        for k in sorted(env):
            lines.append("%s=%s" % (k, shquote(str(env[k]))))
        self.state["main"] = main
        self.state["textures"] = self.textures
        self.state["videos"] = self.videos
        self.state["fingerprint"] = fingerprint(self.root, self.state)
        self._write_state_file("launch.env", "\n".join(lines) + "\n")
        self._write_state_file("inspect.json", json.dumps(report, indent=1, default=_json_default))
        self._save_state()
        self.log("Done. Launch settings written to %s/launch.env" % STATE_DIR)
        return env

    def _pick_bundle(self, report, app_name):
        if not report["bundles"]:
            return None
        if report["runtimeconfigs"] and not app_name:
            return None  # loose files already present, the bundle is extra
        cands = report["bundles"]
        if app_name:
            cands = [b for b in cands if os.path.basename(b["path"]).lower().startswith(app_name.lower())] or cands
        cands = sorted(cands, key=lambda b: (b.get("runtimeconfig") is None, b["arch"] != "x86_64",
                                             -b["content_size"]))
        return cands[0]

    # -- .NET (Core) builds ------------------------------------------------
    def _prepare_dotnet(self, report, main):
        app_dir = os.path.dirname(main)
        prefix = app_dir + "/" if app_dir else ""
        stem = os.path.splitext(os.path.basename(main))[0]
        rc_rel = prefix + stem + ".runtimeconfig.json"
        deps_rel = prefix + stem + ".deps.json"

        deps = None
        runtime_pack = set()
        if os.path.exists(self._abs(deps_rel)):
            with open(self._abs(deps_rel), "rb") as f:
                deps = json.loads(f.read().decode("utf-8-sig"))
            runtime_pack = runtime_pack_files(deps)

        framework_names = set()
        if self.framework_dir and os.path.isdir(self.framework_dir):
            framework_names = set(os.listdir(self.framework_dir))
        app_abs = self._abs(app_dir)
        self_contained = (os.path.exists(os.path.join(app_abs, "System.Private.CoreLib.dll")) or
                          bool(runtime_pack))

        # 1. park the x86_64 runtime that a self-contained build carries
        self.log("Parking the bundled .NET runtime ...")
        for fn in sorted(os.listdir(app_abs)):
            full = os.path.join(app_abs, fn)
            if not os.path.isfile(full):
                continue
            rel = prefix + fn
            reason = None
            if fn in runtime_pack:
                reason = "runtime pack"
            elif fn in RUNTIME_NATIVE_FILES:
                reason = "runtime native"
            elif fn in framework_names and fn.lower().endswith(".dll"):
                reason = "provided by device runtime"
            elif fn in (stem, stem + ".exe") and rel != main and sniff(full)[0] in ("elf", "pe", "macho"):
                reason = "apphost"
            elif self_contained and deps is None and not framework_names and _looks_like_framework(fn):
                reason = "framework assembly"
            if reason:
                self._park(rel, reason)

        # 2. runtimeconfig.json -> framework dependent, roll forward
        rc = {"runtimeOptions": {}}
        if os.path.exists(self._abs(rc_rel)):
            with open(self._abs(rc_rel), "rb") as f:
                rc = json.loads(f.read().decode("utf-8-sig"))
        opts = rc.setdefault("runtimeOptions", {})
        fw_list = opts.pop("includedFrameworks", None) or opts.get("frameworks") or (
            [opts["framework"]] if "framework" in opts else [])
        new_fws = []
        for fw in fw_list:
            name = fw.get("name")
            if name == "Microsoft.WindowsDesktop.App":
                raise PrepareError("the game needs Microsoft.WindowsDesktop.App (WinForms/WPF); "
                                   "use the Linux or macOS build")
            if name == "Microsoft.AspNetCore.App":
                continue
            vm = re.match(r"(\d+)\.(\d+)", fw.get("version") or "")
            new_fws.append({"name": name, "version": ("%s.%s.0" % vm.groups()) if vm else fw.get("version")})
        if not new_fws:
            m = re.search(r"Version=v(\d+)\.(\d+)", report["target_framework"] or "")
            if not m:
                raise PrepareError("cannot determine the .NET version of %s" % main)
            new_fws = [{"name": "Microsoft.NETCore.App", "version": "%s.%s.0" % m.groups()}]
        opts.pop("framework", None)
        opts.pop("frameworks", None)
        if len(new_fws) == 1:
            opts["framework"] = new_fws[0]
        else:
            opts["frameworks"] = new_fws
        opts["rollForward"] = "Major"
        props = opts.setdefault("configProperties", {})
        # No ICU on the handhelds; old BinaryFormatter saves keep working on .NET 8;
        # a background GC thread is a luxury with 1 GB of RAM.
        props["System.Globalization.Invariant"] = True
        props["System.Globalization.PredefinedCulturesOnly"] = False
        props["System.Runtime.Serialization.EnableUnsafeBinaryFormatterSerialization"] = True
        props["System.GC.Concurrent"] = False
        props["System.GC.Server"] = False
        self._rewrite(rc_rel, json.dumps(rc, indent=2) + "\n")

        # 3. deps.json -> drop the runtime pack and every parked file
        if deps is not None:
            parked = set(os.path.basename(p) for p in self.state["moved"])
            self._rewrite(deps_rel, json.dumps(rewrite_deps(deps, parked), indent=2) + "\n")

        return {"YP_MODE": "dotnet", "YP_ENTRY": main, "YP_APPDIR": app_dir or ".",
                "YP_FRAMEWORK": new_fws[0]["name"], "YP_FRAMEWORK_VERSION": new_fws[0]["version"]}

    # -- Mono / .NET Framework builds ---------------------------------------
    def _prepare_mono(self, report, main):
        app_dir = os.path.dirname(main)
        prefix = app_dir + "/" if app_dir else ""
        app_abs = self._abs(app_dir)
        self.log("Parking the bundled Mono runtime ...")
        for item in report["mono_runtime_files"]:
            self._park(item["path"], "mono runtime")
        bcl = re.compile(r"^(mscorlib|System(\..*)?|Mono\..*|Microsoft\.CSharp|Microsoft\.VisualBasic|"
                         r"WindowsBase|netstandard)\.dll$")
        for fn in sorted(os.listdir(app_abs)):
            if bcl.match(fn):
                self._park(prefix + fn, "BCL provided by device Mono")
        # dllmap entries pinned to x86/x86_64 are ignored on aarch64; unpin them.
        for fn in sorted(os.listdir(app_abs)):
            if not fn.endswith(".config"):
                continue
            with open(os.path.join(app_abs, fn), "rb") as f:
                text = f.read().decode("utf-8-sig", "replace")
            if "<dllmap" in text:
                new = unpin_dllmaps(text)
                if new != text:
                    self._rewrite(prefix + fn, new)
        return {"YP_MODE": "mono", "YP_ENTRY": main, "YP_APPDIR": app_dir or "."}

    def _retarget_assemblies(self, report):
        self.log("Retargeting CPU-specific IL assemblies to %s ..." % self.native_arch)
        for m in report["managed"]:
            rel = m["path"]
            if "!/" in rel or rel in self.state["moved"] or not os.path.isfile(self._abs(rel)):
                continue
            with open(self._abs(rel), "rb") as f:
                data = f.read()
            new, what = retarget_assembly(data, self.native_arch)
            if new is None:
                continue
            if load_assembly(new, rel) is None:
                raise PrepareError("retargeting %s produced an unreadable image" % rel)
            self._rewrite_bytes(rel, new, what)

    def _convert_textures(self):
        """Store Color textures in 16-bit formats to halve their GPU memory:
        on a 1 GB device the game's texture atlases alone need ~750 MB."""
        if self.textures == "full":
            return
        self.log("Converting textures (%s) ..." % self.textures)
        saved = 0
        converted = 0
        t0 = time.time()
        for path in _walk(self.root):
            if not path.lower().endswith(".xnb"):
                continue
            rel = _rel(self.root, path)
            try:
                with open(path, "rb") as f:
                    head = f.read(6)
                if len(head) < 6 or head[:3] != b"XNB" or head[5] & 0x80:
                    continue
                xnb = XnbFile(path)
                tex = xnb.texture()
                if tex is None or tex["format"] != 0:
                    continue
                what = convert_texture_xnb(xnb, self.textures)
            except (ValueError, IndexError, struct.error) as e:
                self.log("  skip %s (%s)" % (rel, e))
                continue
            if what is None:
                self.log("  keep  %-55s (soft alpha, stays Color)" % rel)
                continue
            self._rewrite_bytes(rel, xnb.to_bytes(compress=True), what)
            saved += sum(size for _off, size in tex["levels"]) // 2
            converted += 1
        self.log("  %d textures converted, %s less texture memory (%.0fs)" % (
            converted, human(saved), time.time() - t0))

    def _shrink_videos(self):
        """Re-encode cutscenes larger than the screen to fit it: software
        Theora decoding of HD video is too slow for a Cortex-A53."""
        if self.videos == "full":
            return
        todo = []
        for v in find_videos(self.root):
            fit = video_fit(v["width"], v["height"], self.videos)
            if fit is None:
                self.log("  keep  %-55s (%dx%d fits %s)" % (v["path"], v["width"], v["height"], self.videos))
            else:
                todo.append((v, fit))
        if not todo:
            return
        tool = self.video_tool or find_video_tool(self.native_arch, self.native_dirs)
        if tool is None:
            self.log("  cannot shrink %d videos: no %s for %s" % (len(todo), VIDEO_TOOL, self.native_arch))
            return
        todo.sort(key=lambda item: -item[0]["size"])
        seconds = sum(v["duration"] or 0 for v, _fit in todo)
        self.log("Shrinking %d videos (%s of video) to fit %s, %d at a time ..." % (
            len(todo), _fmt_duration(seconds), self.videos, self.jobs))
        if self.dry_run:
            for v, (w, h) in todo:
                self.log("  would shrink %s (%dx%d -> %dx%d)" % (v["path"], v["width"], v["height"], w, h))
            return
        t0 = time.time()
        results = {}
        cmds = []
        for v, _fit in todo:
            src = self._abs(v["path"])
            cmds.append([tool, "--max", self.videos, "--quality", str(self.video_quality),
                         src, src + ".yptmp.ogv"])

        def done(i, rc, out, err):
            v = todo[i][0]
            results[i] = (rc, parse_tool_output(out), err)
            self._video_done(v, rc, results[i][1], err, len(results), len(todo), time.time() - t0)

        run_jobs(cmds, self.jobs, done)
        shrunk = dict((todo[i][0]["path"], todo[i][1]) for i, (rc, _o, _e) in results.items() if rc == 0)
        if shrunk:
            self._patch_video_xnbs(shrunk, dict((v["path"], v) for v, _fit in todo))
        self.log("  %d of %d videos shrunk (%.0fs)" % (len(shrunk), len(todo), time.time() - t0))

    def _video_done(self, v, rc, info, err, n, total, elapsed):
        rel = v["path"]
        tmp = self._abs(rel) + ".yptmp.ogv"
        if rc != 0:
            if os.path.exists(tmp):
                os.remove(tmp)
            why = info.get("skip") or (err.strip().splitlines() or ["exit status %d" % rc])[-1]
            self.log("  [%d/%d] keep  %s (%s)" % (n, total, rel, why))
            return
        new_size = os.path.getsize(tmp)
        budget = 1000.0 / v["fps"] if v["fps"] else 0
        self.log("  [%d/%d] shrink %s  %dx%d -> %s, %s -> %s, decode %s -> %s ms/frame"
                 " (p95 %s -> %s, a frame lasts %.1f ms)  [%.0fs]" % (
                     n, total, rel, v["width"], v["height"], info.get("out_size", "?"), human(v["size"]),
                     human(new_size), info.get("src_decode_avg_ms", "?"), info.get("out_decode_avg_ms", "?"),
                     info.get("src_decode_p95_ms", "?"), info.get("out_decode_p95_ms", "?"), budget, elapsed))
        # The original goes to the backup folder (a rename, not a copy: videos
        # are big) and the new file takes its place.
        dst = os.path.join(self.backup_dir, *rel.split("/"))
        if not os.path.isdir(os.path.dirname(dst)):
            os.makedirs(os.path.dirname(dst))
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(self._abs(rel), dst)
        self.state["moved"].append(rel)
        os.rename(tmp, self._abs(rel))
        self.state["created"].append(rel)
        self._save_state()

    def _patch_video_xnbs(self, shrunk, originals):
        """A video loaded through the content pipeline has an XNB next to it
        that records its size; FNA-derived players refuse a video whose size
        differs from the XNB's."""
        by_name = {}
        for rel in shrunk:
            by_name[os.path.splitext(rel)[0].lower()] = rel
        for path in _walk(self.root):
            if not path.lower().endswith(".xnb"):
                continue
            rel = _rel(self.root, path)
            stem = os.path.splitext(rel)[0].lower()
            try:
                with open(path, "rb") as f:
                    head = f.read(4096)
                if len(head) < 14 or head[:3] != b"XNB":
                    continue
                if head[5] & 0x80:
                    raise ValueError("LZX-compressed")
                # Only the start is decompressed to tell videos from the
                # (big) texture atlases.
                prefix = lz4_block_prefix(head[14:], 1024) if head[5] & 0x40 else head[10:]
                if b"VideoReader" not in prefix:
                    continue
                xnb = XnbFile(path)
                info = xnb_video_info(xnb)
            except (ValueError, IndexError, struct.error) as e:
                if stem in by_name:
                    self.log("  cannot read %s (%s); the game may refuse %s" % (rel, e, by_name[stem]))
                continue
            if info is None:
                continue
            target = by_name.get(stem)
            if target is None and info["filename"]:
                ref = posixpath.normpath(posixpath.join(posixpath.dirname(rel),
                                                       info["filename"].replace("\\", "/")))
                target = by_name.get(os.path.splitext(ref)[0].lower())
            if target is None:
                continue
            orig = originals[target]
            w, h = shrunk[target]
            if (info["width"], info["height"]) != (orig["width"], orig["height"]):
                self.log("  leave %s: it says %dx%d, the video was %dx%d" % (
                    rel, info["width"], info["height"], orig["width"], orig["height"]))
                continue
            data = bytearray(xnb.payload)
            struct.pack_into("<i", data, info["width_offset"], w)
            struct.pack_into("<i", data, info["height_offset"], h)
            xnb.payload = bytes(data)
            self._rewrite_bytes(rel, xnb.to_bytes(compress=bool(xnb.flags & 0x40)),
                                "video size %dx%d -> %dx%d" % (orig["width"], orig["height"], w, h))

    def _rewrite_bytes(self, rel, data, what):
        path = self._abs(rel)
        self.log("  patch %-56s (%s)" % (rel, what))
        if self.dry_run:
            return
        if rel not in self.state["rewritten"] and rel not in self.state["created"]:
            bak = os.path.join(self.backup_dir, *rel.split("/"))
            if not os.path.isdir(os.path.dirname(bak)):
                os.makedirs(os.path.dirname(bak))
            shutil.copy2(path, bak)
            self.state["rewritten"].append(rel)
        with open(path + ".yptmp", "wb") as f:
            f.write(data)
        os.rename(path + ".yptmp", path)
        self._save_state()

    # -- natives -------------------------------------------------------------
    def _install_replacements(self):
        """Put an aarch64 build next to the game wherever an x86_64 library
        with the same file name was parked. Search paths would find them on
        LD_LIBRARY_PATH anyway; this also covers code that dlopen()s a
        library by absolute path inside the game folder."""
        if not self.native_dirs:
            return
        self.log("Installing %s replacements ..." % self.native_arch)
        for rel in list(self.state["moved"]):
            base = os.path.basename(rel)
            if base in RUNTIME_NATIVE_FILES:
                continue
            for d in self.native_dirs:
                src = os.path.join(d, base)
                if not os.path.isfile(src) or sniff(src) != ("elf", self.native_arch):
                    continue
                dst = self._abs(rel)
                if not os.path.exists(dst):
                    self.log("  install %-53s (from %s)" % (rel, d))
                    if not self.dry_run:
                        shutil.copy2(src, dst)
                        self.state["created"].append(rel)
                        self._save_state()
                break

    def _park_foreign_natives(self, report):
        self.log("Parking foreign-architecture native files ...")
        for item in report["native_libs"] + report["runtime_files"] + report["apphosts"]:
            p = item["path"]
            if "!/" in p or item.get("format") != "elf" or item.get("arch") == self.native_arch:
                continue
            self._park(p, "%s ELF" % item.get("arch"))


def runtime_pack_files(deps):
    """File names that a deps.json attributes to the .NET runtime pack."""
    names = set()
    for target in deps.get("targets", {}).values():
        for lib, info in target.items():
            if not lib.lower().startswith("runtimepack."):
                continue
            for section in ("runtime", "native", "resources"):
                for asset in (info.get(section) or {}):
                    names.add(os.path.basename(asset))
    return names


def rewrite_deps(deps, parked):
    """Return a deps.json without runtime packs or entries for parked files."""
    new = json.loads(json.dumps(deps))
    dropped = set()
    for target in new.get("targets", {}).values():
        for lib in list(target):
            if lib.lower().startswith("runtimepack."):
                del target[lib]
                dropped.add(lib)
                continue
            info = target[lib]
            for section in ("runtime", "native", "runtimeTargets"):
                assets = info.get(section)
                if not assets:
                    continue
                for asset in list(assets):
                    if os.path.basename(asset) in parked:
                        del assets[asset]
                if not assets:
                    del info[section]
    libs = new.get("libraries", {})
    for lib in dropped:
        libs.pop(lib, None)
    return new


def unpin_dllmaps(text):
    """Remove cpu="..." restrictions and directory prefixes from Mono dllmaps
    so they apply on aarch64 and resolve through LD_LIBRARY_PATH."""
    def fix(m):
        tag = m.group(0)
        tag = re.sub(r'\s+cpu\s*=\s*"[^"]*"', "", tag)
        tag = re.sub(r'(target\s*=\s*")(?:[^"]*/)?([^"/]+)"', r'\1\2"', tag)
        return tag
    return re.sub(r"<dllmap\b[^>]*>", fix, text)


def fingerprint(root, state):
    """Changes when the user copies a different (or the same) game build over
    a prepared folder: every file prepare touched is part of it."""
    h = hashlib.sha1()
    paths = [state["main"]] + sorted(set(state.get("moved", []) + state.get("created", []) +
                                         state.get("rewritten", [])))
    for rel in paths:
        try:
            st = os.stat(os.path.join(root, *rel.split("/")))
            h.update(("%s:%d:%d\n" % (rel, st.st_size, int(st.st_mtime))).encode())
        except OSError:
            h.update(("%s:-\n" % rel).encode())
    return h.hexdigest()


def _load_state(root):
    with open(os.path.join(root, STATE_DIR, "state.json")) as f:
        return json.load(f)


def restore(gamedir, log=print):
    root = os.path.abspath(gamedir)
    state_dir = os.path.join(root, STATE_DIR)
    if not os.path.exists(os.path.join(state_dir, "state.json")):
        raise PrepareError("nothing to restore (no %s/state.json)" % STATE_DIR)
    state = _load_state(root)
    backup = os.path.join(state_dir, "backup")
    for rel in state.get("rewritten", []):
        src = os.path.join(backup, *rel.split("/"))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(root, *rel.split("/")))
            os.remove(src)
            log("  restored %s" % rel)
    for rel in state.get("created", []):
        p = os.path.join(root, *rel.split("/"))
        if os.path.exists(p):
            os.remove(p)
            log("  removed %s" % rel)
    for rel in reversed(state.get("moved", [])):
        src = os.path.join(backup, *rel.split("/"))
        dst = os.path.join(root, *rel.split("/"))
        if os.path.exists(src):
            if not os.path.isdir(os.path.dirname(dst)):
                os.makedirs(os.path.dirname(dst))
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
            log("  unparked %s" % rel)
    for rel in state.get("extracted", []):
        p = os.path.join(root, *rel.split("/"))
        if os.path.exists(p):
            os.remove(p)
    for rel in sorted(set(os.path.dirname(r) for r in state.get("extracted", []) if "/" in r),
                      key=len, reverse=True):
        d = os.path.join(root, *rel.split("/"))
        while d != root and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            d = os.path.dirname(d)
    shutil.rmtree(state_dir)
    log("Restored original game files.")


def reset(gamedir, log=print):
    """Forget a previous prepare without touching game files (used after the
    user copied a new game build over a prepared folder)."""
    state_dir = os.path.join(os.path.abspath(gamedir), STATE_DIR)
    if os.path.isdir(state_dir):
        shutil.rmtree(state_dir)
        log("Discarded previous preparation state.")


STATUS_OK, STATUS_NOT_PREPARED, STATUS_OUTDATED = 0, 1, 2


def prepared_status(gamedir, textures=None, videos=None):
    """STATUS_OK: prepared by this version and unchanged since.
    STATUS_OUTDATED: prepared by another version of this tool, or with
    another texture or video mode than `textures`/`videos`; `restore` and
    prepare again.
    STATUS_NOT_PREPARED: never prepared, or the game files were copied again
    since (use `reset`)."""
    root = os.path.abspath(gamedir)
    try:
        state = _load_state(root)
        if state.get("fingerprint") != fingerprint(root, state):
            return STATUS_NOT_PREPARED
    except (ValueError, KeyError, OSError, IOError):
        return STATUS_NOT_PREPARED
    if state.get("version") != VERSION:
        return STATUS_OUTDATED
    if textures is not None and state.get("textures", "full") != textures:
        return STATUS_OUTDATED
    if videos is not None and state.get("videos", "full") != videos:
        return STATUS_OUTDATED
    return STATUS_OK


def is_prepared(gamedir):
    return prepared_status(gamedir) == STATUS_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def video_mode(value):
    if value == "full" or re.match(r"^[1-9][0-9]*x[1-9][0-9]*$", value):
        return value
    raise argparse.ArgumentTypeError("expected full or WIDTHxHEIGHT, e.g. 640x480")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ypport.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version="ypport " + VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("inspect", help="describe a game build")
    p.add_argument("dir")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")

    p = sub.add_parser("textures", help="how much memory the game's images need once decoded")
    p.add_argument("dir")
    p.add_argument("--top", type=int, default=20, help="list this many of the largest images")
    p.add_argument("--alpha", action="store_true",
                   help="decode textures to check their alpha and project 16-bit memory use")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("videos", help="list the game's Ogg Theora videos")
    p.add_argument("dir")
    p.add_argument("--bench", action="store_true",
                   help="decode every video with %s and report the time per frame" % VIDEO_TOOL)
    p.add_argument("--tool", help="path of %s (default: bin.<arch>/ next to this script)" % VIDEO_TOOL)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("xnb2png", help="save a texture XNB as PNG, optionally as it looks after conversion")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--as", dest="as_mode", choices=("color", "16bit", "5551", "4444"), default="color",
                   help="16bit: the format --textures 16bit would choose for this texture")

    p = sub.add_parser("xnb-convert", help="convert one Color texture XNB to a 16-bit format")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--mode", choices=TEXTURE_MODES[1:], default="auto16")
    p.add_argument("--no-compress", action="store_true")

    p = sub.add_parser("extract", help="extract a .NET single-file bundle")
    p.add_argument("bundle")
    p.add_argument("outdir")
    p.add_argument("--list", action="store_true", help="only list the entries")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("prepare", help="make a game folder runnable on aarch64")
    p.add_argument("dir")
    p.add_argument("--framework-dir", help="shared/Microsoft.NETCore.App/<ver> of the target runtime")
    p.add_argument("--app", help="name of the game executable when several are present")
    p.add_argument("--native-dir", action="append", default=[],
                   help="folder with aarch64 libraries to install in place of parked x86_64 ones "
                        "(repeatable, first match wins)")
    p.add_argument("--native-arch", default="aarch64",
                   help="architecture of the --native-dir libraries (tests on a PC use x86_64)")
    p.add_argument("--textures", choices=TEXTURE_MODES, default="full",
                   help="auto16: Color textures with binary alpha -> Bgra5551; 16bit: also the "
                        "rest -> Bgra4444; full: leave textures alone")
    p.add_argument("--videos", type=video_mode, default="full",
                   help="WxH: re-encode videos larger than WxH to fit it; full: leave them alone")
    p.add_argument("--video-tool", help="path of %s (default: searched in --native-dir and "
                                        "bin.<arch>/ next to this script)" % VIDEO_TOOL)
    p.add_argument("--video-quality", type=int, default=44, help="Theora quality 0-63 (default 44)")
    p.add_argument("--jobs", type=int, help="videos encoded at a time (default: CPU cores, at most 4)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="restore first if already prepared")

    p = sub.add_parser("restore", help="undo prepare")
    p.add_argument("dir")

    p = sub.add_parser("reset", help="forget a previous prepare, keep game files as they are")
    p.add_argument("dir")

    p = sub.add_parser("status", help="exit 0 if prepared and unchanged, 2 if prepared by another "
                                      "version of this tool or texture/video mode, 1 otherwise")
    p.add_argument("dir")
    p.add_argument("--textures", choices=TEXTURE_MODES)
    p.add_argument("--videos", type=video_mode)

    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        r = inspect_dir(args.dir)
        if args.json:
            json.dump(r, sys.stdout, indent=1, default=_json_default)
            sys.stdout.write("\n")
        else:
            print_report(r)
        return 0 if r["portable"] else 2
    if args.cmd == "textures":
        r = survey_textures(args.dir, top=args.top, alpha=args.alpha)
        if args.json:
            json.dump(r, sys.stdout, indent=1, default=_json_default)
            sys.stdout.write("\n")
        else:
            print_textures(r)
        return 0
    if args.cmd == "videos":
        root = os.path.abspath(args.dir)
        videos = find_videos(root)
        bench = None
        if args.bench:
            tool = args.tool or find_video_tool(_host_arch())
            if tool is None:
                print("no %s for this machine; pass --tool" % VIDEO_TOOL)
                return 1
            bench = {}
            for v in videos:
                sys.stderr.write("decoding %s ...\n" % v["path"])
                proc = subprocess.Popen([tool, "--bench", os.path.join(root, v["path"])],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = proc.communicate()
                b = parse_tool_output(out.decode("utf-8", "replace"))
                if proc.returncode not in (0, 3):
                    b["error"] = err.decode("utf-8", "replace").strip() or "exit %d" % proc.returncode
                bench[v["path"]] = b
                v["bench"] = b
        if args.json:
            json.dump(videos, sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            print_videos(videos, root, bench)
        return 0
    if args.cmd == "xnb2png":
        xnb = XnbFile(args.src)
        tex = xnb.texture()
        if tex is None or tex["format"] != 0:
            print("not a Color texture")
            return 1
        off, size = tex["levels"][0]
        rgba = xnb.payload[off:off + size]
        cls, share = alpha_class(rgba)
        fmt = {"color": None, "5551": 2, "4444": 3}.get(args.as_mode)
        if args.as_mode == "16bit":
            fmt = texture_format_for([cls], "16bit")
        if fmt:
            rgba = preview_16bit(rgba, fmt)
        write_png(args.dst, tex["width"], tex["height"], rgba)
        print("%dx%d, alpha %s (%.2f%% semi-transparent), shown as %s" % (
            tex["width"], tex["height"], cls, share * 100, {2: "Bgra5551", 3: "Bgra4444"}.get(fmt, "Color")))
        return 0
    if args.cmd == "xnb-convert":
        xnb = XnbFile(args.src)
        what = convert_texture_xnb(xnb, args.mode)
        with open(args.dst, "wb") as f:
            f.write(xnb.to_bytes(compress=not args.no_compress))
        print(what or "unchanged (not a Color texture, or alpha not binary in auto16 mode)")
        return 0
    if args.cmd == "extract":
        b = Bundle(args.bundle)
        print("bundle v%d.%d, id %s, %d files%s" % (b.major, b.minor, b.bundle_id, len(b.entries),
                                                     ", compressed" if b.compressed else ""))
        if args.list:
            for e in b.entries:
                print("  %-10s %10d %10d  %s" % (e.type_name, e.size, e.compressed_size, e.path))
            return 0
        if not os.path.isdir(args.outdir):
            os.makedirs(args.outdir)
        b.extract(args.outdir, overwrite=args.overwrite, log=print)
        return 0
    if args.cmd == "prepare":
        if args.force and os.path.exists(os.path.join(args.dir, STATE_DIR, "state.json")):
            restore(args.dir)
        t0 = time.time()
        try:
            Preparer(args.dir, framework_dir=args.framework_dir, dry_run=args.dry_run,
                     native_dirs=args.native_dir, native_arch=args.native_arch,
                     textures=args.textures, videos=args.videos, video_tool=args.video_tool,
                     video_quality=args.video_quality, jobs=args.jobs).run(app_name=args.app)
        except (PrepareError, BundleError) as e:
            print("ERROR: %s" % e)
            return 3
        print("prepare took %.1fs" % (time.time() - t0))
        return 0
    if args.cmd == "restore":
        try:
            restore(args.dir)
        except PrepareError as e:
            print("ERROR: %s" % e)
            return 3
        return 0
    if args.cmd == "reset":
        reset(args.dir)
        return 0
    if args.cmd == "status":
        return prepared_status(args.dir, textures=args.textures, videos=args.videos)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
