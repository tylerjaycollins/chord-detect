"""The Overtone icon, drawn rather than stored.

Lives in the package rather than in `scripts/` because two things need it: the
macOS bundle build, and the web app itself -- Safari's "Add to Dock" takes its
icon from the page, so the server has to serve one or the Dock gets a generic
placeholder.

Hand-rolled PNG encoding, no Pillow or matplotlib: the icon is a few
rectangles, and a whole imaging stack is a lot of dependency for that.
"""

from __future__ import annotations

import struct
import zlib
from functools import lru_cache
from pathlib import Path


def png_bytes(rgba) -> bytes:
    """Encode an (H, W, 4) uint8 array as PNG."""
    height, width = rgba.shape[:2]
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # per-scanline filter type: none
        raw.extend(rgba[row].tobytes())

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


@lru_cache(maxsize=4)
def icon_png(size: int = 1024, rounded: bool = True) -> bytes:
    """A beat grid with one measure playing.

    `rounded` masks the corners the way a macOS app icon is drawn. Safari's
    web-app icons get their own rounding applied on top, so the served version
    passes False and keeps the full square -- rounding twice leaves a pale
    fringe where the first mask cut the background away.
    """
    import numpy as np

    def rgb(value: str) -> tuple[int, int, int]:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))

    bg, cell, cell_on, gold, white = (
        rgb("#16202e"), rgb("#3a4657"), rgb("#5d6c86"),
        rgb("#e8c98a"), (255, 255, 255),
    )

    img = np.zeros((size, size, 4), dtype=np.uint8)

    def box(x0, y0, x1, y1, colour):
        a, b = int(y0 * size), int(y1 * size)
        c, d = int(x0 * size), int(x1 * size)
        img[a:b, c:d, :3] = colour
        img[a:b, c:d, 3] = 255

    inset, radius = (0.06, 0.20) if rounded else (0.0, 0.0)
    box(inset, inset, 1 - inset, 1 - inset, bg)

    if rounded:
        r = radius * size
        lo, hi = int(inset * size), int((1 - inset) * size)
        ys, xs = np.mgrid[0:size, 0:size]
        for cx, cy in (
            (lo + r, lo + r), (hi - r, lo + r), (lo + r, hi - r), (hi - r, hi - r)
        ):
            corner = (
                ((xs - cx) ** 2 + (ys - cy) ** 2 > r * r)
                & ((xs < cx) if cx < size / 2 else (xs > cx))
                & ((ys < cy) if cy < size / 2 else (ys > cy))
            )
            img[corner] = 0

    # Three measures of four beats; the middle one is playing.
    left, width, gap = 0.155, 0.69, 0.018
    cw = (width - 3 * gap) / 4
    for y, active in ((0.24, False), (0.425, True), (0.61, False)):
        for i in range(4):
            x = left + i * (cw + gap)
            box(x, y, x + cw, y + 0.145, cell_on if active else cell)
        # Gold marker on beat one, where a chord change lands.
        box(left, y, left + 0.022, y + 0.145, gold)

    # Playhead riding across the measure that's playing.
    head = left + 1.6 * (cw + gap)
    box(head, 0.41, head + 0.013, 0.595, white)

    return png_bytes(img)


def draw_icon(png_path: Path, size: int = 1024) -> None:
    """Write the bundle icon to disk."""
    png_path.write_bytes(icon_png(size, rounded=True))
