"""Build a double-clickable macOS app that launches chord-detect.

Produces `Chord Detect.app` — a real bundle with an icon, so it shows up in
Spotlight, Launchpad and the Dock. The bundle is a thin launcher, not a frozen
copy of the code: it runs `uv run chord-detect-app` from this repo, so edits to
the project take effect without rebuilding.

The launcher is idempotent. If the server is already up it just opens the
browser instead of starting a second one on a port that's already taken.

    uv run python scripts/make_app.py            # build into ~/Applications
    uv run python scripts/make_app.py --dest .   # build here instead
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_NAME = "Chord Detect"
BUNDLE_ID = "local.chorddetect.app"
PORT = 8765

LAUNCHER = """#!/bin/bash
# Launcher for {app_name}. Starts the local server if it isn't already up,
# then opens the browser. Keeps its own log so failures are diagnosable.
REPO="{repo}"
PORT={port}
LOG="$HOME/Library/Logs/chord-detect.log"
mkdir -p "$(dirname "$LOG")"

open_ui() {{ open "http://127.0.0.1:$PORT/"; }}

# Already running? Just show it.
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
  open_ui
  exit 0
fi

cd "$REPO" || {{
  osascript -e 'display alert "Chord Detect" message "Project folder not found at {repo}."'
  exit 1
}}

# `uv` lives in a few different places depending on how it was installed, and
# a GUI launch does not inherit the shell's PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  osascript -e 'display alert "Chord Detect" message "Could not find uv. Install it, or launch from a terminal instead."'
  exit 1
fi

echo "--- launch $(date) ---" >> "$LOG"
nohup uv run chord-detect-app >> "$LOG" 2>&1 &

# Wait for the port, then open. Analysis models load lazily, so this is quick.
for _ in $(seq 1 60); do
  if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then
    open_ui
    exit 0
  fi
  sleep 0.5
done

osascript -e 'display alert "Chord Detect" message "The server did not start. See ~/Library/Logs/chord-detect.log."'
exit 1
"""


def _png_bytes(rgba) -> bytes:
    """Encode an (H, W, 4) uint8 array as PNG.

    Hand-rolled rather than pulling in Pillow or matplotlib: the icon is a few
    rectangles, and a whole imaging stack is a lot of dependency for that.
    """
    import struct
    import zlib

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


def draw_icon(png_path: Path, size: int = 1024) -> None:
    """Render the app icon: a beat grid with one measure playing."""
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

    # Rounded background, inset the way macOS icons are.
    inset, radius = 0.06, 0.20
    box(inset, inset, 1 - inset, 1 - inset, bg)
    r = radius * size
    lo, hi = int(inset * size), int((1 - inset) * size)
    ys, xs = np.mgrid[0:size, 0:size]
    for cx, cy in ((lo + r, lo + r), (hi - r, lo + r), (lo + r, hi - r), (hi - r, hi - r)):
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

    png_path.write_bytes(_png_bytes(img))


def build_icns(png_path: Path, out_dir: Path) -> Path | None:
    """Convert a PNG into a multi-resolution .icns via macOS tooling."""
    if not shutil.which("iconutil") or not shutil.which("sips"):
        return None
    iconset = out_dir / "icon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 64, 128, 256, 512, 1024):
        for name, px in ((f"icon_{size}x{size}.png", size),
                         (f"icon_{size // 2}x{size // 2}@2x.png", size)):
            if size == 16 and "@2x" in name:
                continue
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(png_path), "--out",
                 str(iconset / name)],
                check=True, capture_output=True,
            )
    icns = out_dir / "icon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True, capture_output=True)
    return icns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=Path.home() / "Applications")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This builds a macOS .app bundle; nothing to do on this platform.")
        return 1

    dest = args.dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    bundle = dest / f"{APP_NAME}.app"
    if bundle.exists():
        shutil.rmtree(bundle)

    macos = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    launcher = macos / APP_NAME.replace(" ", "")
    launcher.write_text(LAUNCHER.format(app_name=APP_NAME, repo=REPO, port=PORT))
    launcher.chmod(0o755)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": launcher.name,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "icon",
        "LSMinimumSystemVersion": "11.0",
        # No dock icon or menu bar: this is a launcher for a browser UI, and a
        # bouncing dock icon that never becomes a window is just confusing.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "icon.png"
        try:
            draw_icon(png)
            icns = build_icns(png, Path(tmp))
            if icns:
                shutil.copy(icns, resources / "icon.icns")
            else:
                print("  (sips/iconutil unavailable — bundle will use a generic icon)")
        except Exception as exc:  # noqa: BLE001 - an icon is not worth failing over
            print(f"  (icon generation skipped: {type(exc).__name__}: {exc})")

    # Nudge Launch Services so it shows up in Spotlight without a re-login.
    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks"
        "/LaunchServices.framework/Support/lsregister"
    )
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", str(bundle)], capture_output=True)

    print(f"Built {bundle}")
    print("  Double-click it, or find it in Spotlight as “Chord Detect”.")
    print("  Logs: ~/Library/Logs/chord-detect.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
