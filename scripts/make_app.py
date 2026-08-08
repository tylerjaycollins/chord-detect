"""Build a double-clickable macOS app that launches chord-detect.

Produces `Overtone.app` — a real bundle with an icon, so it shows up in
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
sys.path.insert(0, str(REPO / "src"))

from chord_detect.icon import draw_icon  # noqa: E402  (needs sys.path above)
APP_NAME = "Overtone"
BUNDLE_ID = "local.overtone.app"
PORT = 8765

LAUNCHER = """#!/bin/bash
# Launcher for {app_name}. Starts the local server if it isn't already up,
# then opens the browser. Keeps its own log so failures are diagnosable.
REPO="{repo}"
PORT={port}
LOG="$HOME/Library/Logs/chord-detect.log"
mkdir -p "$(dirname "$LOG")"

# Open in a standalone window rather than a browser tab. Chrome's --app drops
# the address bar, tabs and bookmarks, so Overtone gets a window of its own that
# cmd-tabs and sits in Mission Control like an app. Falls back to the default
# browser when Chrome isn't installed -- a tab still works, it's just a tab.
open_ui() {{
  if [ -d "/Applications/Google Chrome.app" ]; then
    open -na "Google Chrome" --args \
      --app="http://127.0.0.1:$PORT/" \
      --user-data-dir="$HOME/Library/Application Support/Overtone/window" \
      >/dev/null 2>&1 || open "http://127.0.0.1:$PORT/"
  else
    open "http://127.0.0.1:$PORT/"
  fi
}}

# Already running? Just show it.
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
  open_ui
  exit 0
fi

cd "$REPO" || {{
  osascript -e 'display alert "Overtone" message "Project folder not found at {repo}."'
  exit 1
}}

# `uv` lives in a few different places depending on how it was installed, and
# a GUI launch does not inherit the shell's PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  osascript -e 'display alert "Overtone" message "Could not find uv. Install it, or launch from a terminal instead."'
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

osascript -e 'display alert "Overtone" message "The server did not start. See ~/Library/Logs/chord-detect.log."'
exit 1
"""


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
    print("  Double-click it, or find it in Spotlight as “Overtone”.")
    print("  Logs: ~/Library/Logs/chord-detect.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
