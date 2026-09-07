"""Generate `axis/desktop/assets/axis_icon.ico` from scratch.

Pure standard library (zlib + struct) — deliberately no Pillow/new
dependency just to rasterize a placeholder mark. Produces a small
multi-resolution .ico (16/32/48/256) with a simple dark rounded-square
background and a blue "A" glyph, matching `axis_icon.svg`'s palette.

Re-run this any time to regenerate the icon (e.g. after swapping in a real
brand mark by hand — this script only needs to run once and its output is
checked in like any other asset).
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

BACKGROUND = (0x1B, 0x1D, 0x22, 255)
ACCENT = (0x3B, 0x82, 0xF6, 255)
TRANSPARENT = (0, 0, 0, 0)

# 5x7 bitmap glyph for "A".
_GLYPH = [
    "01110",
    "10001",
    "10001",
    "11111",
    "10001",
    "10001",
    "10001",
]


def _rounded_square_mask(x: int, y: int, size: int, radius: float) -> bool:
    cx = min(max(x, radius), size - 1 - radius)
    cy = min(max(y, radius), size - 1 - radius)
    dx, dy = x - cx, y - cy
    return (dx * dx + dy * dy) <= radius * radius


def _glyph_mask(x: int, y: int, size: int) -> bool:
    margin = size * 0.28
    inner = size - 2 * margin
    gw, gh = len(_GLYPH[0]), len(_GLYPH)
    cell = inner / max(gw, gh)
    gx = int((x - margin) / cell)
    gy = int((y - margin) / cell)
    if 0 <= gy < gh and 0 <= gx < gw:
        return _GLYPH[gy][gx] == "1"
    return False


def render(size: int) -> bytes:
    """Raw RGBA bytes, row-major, top-to-bottom."""
    radius = size * 0.22
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            idx = (y * size + x) * 4
            if not _rounded_square_mask(x, y, size, radius):
                color = TRANSPARENT
            elif _glyph_mask(x, y, size):
                color = ACCENT
            else:
                color = BACKGROUND
            pixels[idx : idx + 4] = bytes(color)
    return bytes(pixels)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(size: int, rgba: bytes) -> bytes:
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type: None
        raw.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def build_ico(sizes: list[int]) -> bytes:
    images = [encode_png(size, render(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    entries = bytearray()
    offset = 6 + 16 * len(sizes)
    for size, png in zip(sizes, images):
        wh = size if size < 256 else 0
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return header + bytes(entries) + b"".join(images)


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "axis" / "desktop" / "assets" / "axis_icon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_ico([16, 32, 48, 256]))
    print(f"Wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
