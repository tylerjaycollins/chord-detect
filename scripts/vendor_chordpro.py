"""Vendor Ledgr's ChordPro logic into Overtone as a single JS bundle.

Why vendor rather than reimplement: Ledgr's chart code is import-pure by
design -- `lib/chordpro/*.ts` and `chord-editor/chordpro-edit.ts` have no npm
imports and no React, which its own `types.ts` calls out as deliberate so the
same parser runs in the client editor, the server print path, and a plain-node
script. Overtone becomes a fourth consumer. The payoff is that a chart cannot
mean one thing here and another in Ledgr: they share the parser, the
serializer, the transposer and every editing operation.

The cost is a cross-repo coupling, which is why provenance is recorded rather
than assumed. The bundle header and `chordpro.json` both carry the Ledgr commit
it was built from, so "is this stale?" is a question with an answer.

    uv run python scripts/vendor_chordpro.py
    uv run python scripts/vendor_chordpro.py --check   # stale? (exit 1 if so)

Needs Ledgr checked out with its node_modules installed; esbuild comes from
there rather than being a dependency here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "src" / "chord_detect" / "static"
BUNDLE = OUT_DIR / "chordpro.js"
STAMP = OUT_DIR / "chordpro.json"

DEFAULT_LEDGR = Path.home() / "Development" / "Ledgr"

# Only pure modules. The .tsx components are React and deliberately excluded --
# Overtone's UI is vanilla JS on a generated page, so the components are the one
# part that genuinely has to be rewritten rather than reused.
ENTRY = """\
export { parseChordPro, serializeChordChart } from "@/lib/chordpro/parse";
export * from "@/lib/chordpro/transpose";
export * from "@/lib/chordpro/lyrics";
export * from "@/lib/chordpro/types";
export * from "@/components/chord-editor/chordpro-edit";
"""


def ledgr_root() -> Path:
    """Where Ledgr lives. LEDGR_REPO overrides for a non-standard checkout."""
    return Path(os.environ.get("LEDGR_REPO") or DEFAULT_LEDGR).expanduser()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def build(root: Path) -> dict:
    """Bundle Ledgr's chordpro modules into one IIFE. Returns the stamp."""
    esbuild = root / "node_modules" / ".bin" / "esbuild"
    if not esbuild.exists():
        raise SystemExit(
            f"esbuild not found at {esbuild}.\n"
            f"Run `npm install` in {root} first — Overtone borrows Ledgr's "
            "toolchain rather than adding a JS build of its own."
        )

    commit = _git(root, "rev-parse", "HEAD")
    committed = _git(root, "log", "-1", "--format=%cI")
    # A dirty Ledgr tree means the commit alone doesn't identify the source.
    dirty = bool(_git(root, "status", "--porcelain", "src/lib/chordpro",
                      "src/components/chord-editor"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "entry.ts"
        entry.write_text(ENTRY)
        subprocess.run(
            [
                str(esbuild),
                str(entry),
                "--bundle",
                "--format=iife",
                "--global-name=ChordPro",
                "--platform=browser",
                f"--alias:@={root / 'src'}",
                f"--outfile={BUNDLE}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    stamp = {
        "source": str(root),
        "commit": commit,
        "committed": committed,
        "dirty": dirty,
        "vendored": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": BUNDLE.stat().st_size,
    }
    STAMP.write_text(json.dumps(stamp, indent=2) + "\n")

    # Header in the bundle too: the JSON is easy to miss when reading the JS.
    header = (
        "// Vendored from Ledgr — DO NOT EDIT HERE.\n"
        f"// Ledgr commit {commit[:12]} ({committed})"
        f"{'  [DIRTY WORKING TREE]' if dirty else ''}\n"
        "// Rebuild: uv run python scripts/vendor_chordpro.py\n"
    )
    BUNDLE.write_text(header + BUNDLE.read_text())
    stamp["bytes"] = BUNDLE.stat().st_size
    STAMP.write_text(json.dumps(stamp, indent=2) + "\n")
    return stamp


def check(root: Path) -> int:
    """Report whether the vendored bundle is behind Ledgr. Exit 1 if stale."""
    if not STAMP.exists():
        print("No vendored bundle yet. Run without --check to build one.")
        return 1

    stamp = json.loads(STAMP.read_text())
    head = _git(root, "rev-parse", "HEAD")
    if head == stamp["commit"] and not stamp.get("dirty"):
        print(f"Up to date with Ledgr {head[:12]}.")
        return 0

    # Count only commits that actually touched the vendored sources; Ledgr is a
    # busy repo and "200 commits behind" is noise if none of them were ours.
    behind = _git(
        root,
        "rev-list",
        "--count",
        f"{stamp['commit']}..HEAD",
        "--",
        "src/lib/chordpro",
        "src/components/chord-editor",
    )
    if behind == "0" and not stamp.get("dirty"):
        print(f"Ledgr has moved on, but nothing under chordpro/ changed. Fine.")
        return 0

    print(
        f"STALE: {behind} commit(s) touched Ledgr's chordpro sources since "
        f"{stamp['commit'][:12]}."
        + ("  (built from a dirty tree)" if stamp.get("dirty") else "")
        + "\nRebuild: uv run python scripts/vendor_chordpro.py"
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report staleness instead of rebuilding. Exits 1 when stale.",
    )
    args = parser.parse_args()

    root = ledgr_root()
    if not (root / "src" / "lib" / "chordpro").is_dir():
        raise SystemExit(
            f"Ledgr's chordpro sources aren't at {root}.\n"
            "Set LEDGR_REPO to the checkout."
        )

    if args.check:
        sys.exit(check(root))

    stamp = build(root)
    print(f"Vendored {stamp['bytes']:,} bytes from Ledgr {stamp['commit'][:12]}")
    if stamp["dirty"]:
        print("  WARNING: built from a dirty Ledgr tree — the commit doesn't "
              "fully identify this bundle.")
    print(f"  {BUNDLE.relative_to(REPO)}")


if __name__ == "__main__":
    main()
