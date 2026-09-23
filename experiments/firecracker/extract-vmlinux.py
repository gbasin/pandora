#!/usr/bin/env python3
"""Extract an uncompressed ELF vmlinux from an x86 bzImage.

Firecracker on x86_64 boots only an uncompressed ELF kernel; every distro ships
a compressed bzImage. Rather than building a kernel (slow, and then it is not
the distro's Docker-ready config), we carve the payload out of the distro image.
Scans for each supported compressor's magic and keeps the first result that is
a valid ELF.
"""
import lzma
import gzip
import io
import subprocess
import sys
import zlib

CANDIDATES = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x5d\x00\x00\x00", "lzma"),
    (b"\x89\x4c\x5a\x4f", "lzop"),
    (b"\x02\x21\x4c\x18", "lz4"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
]


def decompress(kind, buf):
    if kind == "gzip":
        return gzip.GzipFile(fileobj=io.BytesIO(buf)).read()
    if kind == "xz":
        return lzma.LZMADecompressor(lzma.FORMAT_XZ).decompress(buf)
    if kind == "lzma":
        return lzma.LZMADecompressor(lzma.FORMAT_ALONE).decompress(buf)
    cmd = {"zstd": ["zstd", "-d", "-c"], "lz4": ["lz4", "-d", "-c"], "lzop": ["lzop", "-d", "-c"]}[kind]
    p = subprocess.run(cmd, input=buf, capture_output=True)
    return p.stdout


def main(src, dst):
    data = open(src, "rb").read()
    for magic, kind in CANDIDATES:
        start = 0
        while True:
            pos = data.find(magic, start)
            if pos < 0:
                break
            start = pos + 1
            try:
                out = decompress(kind, data[pos:])
            except Exception:
                continue
            if len(out) > 1_000_000 and out[:4] == b"\x7fELF":
                open(dst, "wb").write(out)
                print(f"{kind} payload at 0x{pos:x} -> {dst} ({len(out)} bytes)")
                return 0
    print("no ELF payload found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
