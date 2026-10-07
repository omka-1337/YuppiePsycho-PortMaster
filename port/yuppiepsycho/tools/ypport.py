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

Only the Python standard library is used, so the same file runs on a PC and
on the handheld (Python 3.6+).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time
import zlib

try:
    from shlex import quote as shquote
except ImportError:  # pragma: no cover
    from pipes import quote as shquote

VERSION = "0.1.0"
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
        mnh_rva = struct.unpack_from("<I", data, cli + 64)[0]
        self.ready_to_run = False
        if mnh_rva:
            off = self._off(mnh_rva)
            self.ready_to_run = off is not None and data[off:off + 4] == b"RTR\0"
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
    app = [m for m in r["managed"] if not m["framework_assembly"]]
    fw = [m for m in r["managed"] if m["framework_assembly"]]
    w("  app assemblies (%d):\n" % len(app))
    for m in sorted(app, key=lambda m: m["path"]):
        flags = []
        if m.get("ready_to_run"):
            flags.append("R2R")
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
        w("  %-60s %-6s %-8s %s\n" % (n["path"][-60:], n["format"], n["arch"], key or ""))
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
# prepare / restore
# --------------------------------------------------------------------------

class PrepareError(Exception):
    pass


class Preparer(object):
    def __init__(self, gamedir, framework_dir=None, log=print, dry_run=False):
        self.root = os.path.abspath(gamedir)
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

        engine = report["engine"] or {}
        env.update({
            "YP_ENGINE": engine.get("name") or "",
            "YP_ENGINE_VERSION": engine.get("version") or "",
            "YP_ENGINE_FLAVOR": engine.get("flavor") or "",
            "YP_FMOD_VERSION": report["fmod_header_version"] or "",
            "YP_NATIVE_NEEDS": " ".join(sorted(report["native_needs"])),
            "YP_PREPARED_BY": "ypport %s" % VERSION,
        })
        lines = ["# generated by ypport.py prepare - do not edit"]
        for k in sorted(env):
            lines.append("%s=%s" % (k, shquote(str(env[k]))))
        self.state["main"] = main
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

    # -- natives -------------------------------------------------------------
    def _park_foreign_natives(self, report):
        self.log("Parking foreign-architecture native files ...")
        for item in report["native_libs"] + report["runtime_files"] + report["apphosts"]:
            p = item["path"]
            if "!/" in p or item.get("format") != "elf" or item.get("arch") == "aarch64":
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
    """Changes when the user copies a different game build over the folder."""
    h = hashlib.sha1()
    main = state["main"]
    st = os.stat(os.path.join(root, *main.split("/")))
    h.update(("%s:%d:%d\n" % (main, st.st_size, int(st.st_mtime))).encode())
    for rel in sorted(state.get("moved", [])):
        # A parked file showing up again means the game was copied again.
        h.update(("%s:%d\n" % (rel, os.path.exists(os.path.join(root, *rel.split("/"))))).encode())
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


def is_prepared(gamedir):
    root = os.path.abspath(gamedir)
    try:
        state = _load_state(root)
        return state.get("fingerprint") == fingerprint(root, state)
    except (ValueError, KeyError, OSError, IOError):
        return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="ypport.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version="ypport " + VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("inspect", help="describe a game build")
    p.add_argument("dir")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")

    p = sub.add_parser("extract", help="extract a .NET single-file bundle")
    p.add_argument("bundle")
    p.add_argument("outdir")
    p.add_argument("--list", action="store_true", help="only list the entries")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("prepare", help="make a game folder runnable on aarch64")
    p.add_argument("dir")
    p.add_argument("--framework-dir", help="shared/Microsoft.NETCore.App/<ver> of the target runtime")
    p.add_argument("--app", help="name of the game executable when several are present")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="restore first if already prepared")

    p = sub.add_parser("restore", help="undo prepare")
    p.add_argument("dir")

    p = sub.add_parser("reset", help="forget a previous prepare, keep game files as they are")
    p.add_argument("dir")

    p = sub.add_parser("status", help="exit 0 if the folder is prepared and unchanged")
    p.add_argument("dir")

    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        r = inspect_dir(args.dir)
        if args.json:
            json.dump(r, sys.stdout, indent=1, default=_json_default)
            sys.stdout.write("\n")
        else:
            print_report(r)
        return 0 if r["portable"] else 2
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
            Preparer(args.dir, framework_dir=args.framework_dir, dry_run=args.dry_run).run(app_name=args.app)
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
        return 0 if is_prepared(args.dir) else 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
