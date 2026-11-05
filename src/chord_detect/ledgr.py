"""Send a finished chart to Ledgr on this Mac, as a `.cho` file it opens.

Tyler runs the Rust Ledgr as a local app now (2026-11-05), so there's no web
API to post to and no token to keep. The hand-off is a file instead: Overtone
writes the chart to `~/Music/Overtone/<title>.cho` and asks macOS to open it
in Ledgr. Ledgr (D-377 in ledgr-rs) makes a Song from it, and opening the same
file again replaces that song's chart. So each song keeps one file here, and
sending again overwrites it.

Overtone's ChordPro is already Ledgr's own song shape (`{section:}` lines,
`| F / / / |` bars, `[G]words`), so the file goes as it is.

The old Ledgr 1 upload (`POST /api/machine/items` with an api-scoped token) is
gone; `~/.config/overtone/config.json` is no longer read.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

SEND_DIR = Path.home() / "Music" / "Overtone"
APP_PLACES = (
    Path.home() / "Applications" / "Ledgr.app",
    Path("/Applications/Ledgr.app"),
)


class LedgrError(RuntimeError):
    """Sending didn't work; the message is written for a person to act on."""


def ledgr_app() -> Path | None:
    """The installed Ledgr that can open `.cho` files, if there is one.

    An older Ledgr opens only Markdown; handing it a `.cho` would do nothing
    useful, so it counts as not there.
    """
    for app in APP_PLACES:
        try:
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        for doc in info.get("CFBundleDocumentTypes", []):
            if "cho" in (e.lower() for e in doc.get("CFBundleTypeExtensions", [])):
                return app
    return None


def file_name(title: str) -> str:
    """A safe file name for a song title: no slashes, colons or leading dots."""
    name = re.sub(r'[/:\\\x00-\x1f]+', " ", title).strip().lstrip(".").strip()
    name = re.sub(r"\s+", " ", name)[:120].strip()
    return (name or "Song") + ".cho"


def song_file(title: str, remembered: Path | None = None) -> Path:
    """Where this song's file lives: the one it was sent to before, else a new
    name from its title that no other song's file has (`Title (2).cho`)."""
    if remembered is not None and remembered.parent == SEND_DIR:
        return remembered
    first = SEND_DIR / file_name(title)
    if not first.exists():
        return first
    stem = first.stem
    n = 2
    while (SEND_DIR / f"{stem} ({n}).cho").exists():
        n += 1
    return SEND_DIR / f"{stem} ({n}).cho"


def send(text: str, path: Path) -> Path:
    """Write the chart to `path` (replacing what was there) and open it in Ledgr."""
    app = ledgr_app()
    if app is None:
        raise LedgrError(
            "This Mac's Ledgr can't open song files yet. Update Ledgr "
            "(in ledgr-rs: sh tools/mac-app.sh), then send again."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    try:
        subprocess.run(
            ["/usr/bin/open", "-a", str(app), str(path)],
            check=True, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise LedgrError(
            f"The chart is saved at {path}, but Ledgr didn't open it ({err}). "
            "Open that file in Ledgr yourself."
        ) from err
    return path
