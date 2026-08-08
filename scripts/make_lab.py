"""Turn a human-written chord sheet into a timed .lab annotation.

Writing timestamps by hand is the tedious part of building ground truth, and
it's unnecessary: the analyzer already detects downbeats, so if you write the
chords *in measures* and say roughly where a section starts, bar lines can be
placed automatically.

Spec format -- a plain text file, one directive or bar-line per line:

    # Comments start with #.
    @ 0:12            anchor: the next bar starts at ~0:12 (snaps to a downbeat)
    D | G | A | D     four bars, one chord each
    D | G | A D | D   bar 3 holds two chords, split evenly
    x3                repeat the previous line 3 more times
    @ 1:45            jump to another section
    Bm | G | D | A

Usage:
    uv run python scripts/make_lab.py song.mp3 song.chords
    uv run python scripts/make_lab.py song.mp3 song.chords -o testdata/song.lab

Caveat worth knowing: bar lines come from *detected* downbeats, so a .lab built
this way can score chord identity but not timing -- it inherits whatever timing
the tracker chose. That's the right trade here, because chord quality is the
open question and timing was already verified to ~20 ms against synthetic
audio. If the beat grid is visibly wrong, fix that before trusting the output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chord_detect.pipeline import analyze  # noqa: E402
from chord_detect.theory import NO_CHORD, parse_chord  # noqa: E402


def parse_timestamp(text: str) -> float:
    """Accept 83, 1:23, or 1:23.5."""
    text = text.strip()
    if ":" not in text:
        return float(text)
    minutes, seconds = text.rsplit(":", 1)
    return int(minutes) * 60 + float(seconds)


def parse_spec(path: Path) -> list[tuple[float | None, list[list[str]]]]:
    """Parse the spec into (anchor_time, bars) sections.

    Each bar is a list of chord labels to split evenly across it.
    """
    sections: list[tuple[float | None, list[list[str]]]] = []
    anchor: float | None = None
    bars: list[list[str]] = []
    previous_line: str | None = None

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        if line.startswith("@"):
            if bars:
                sections.append((anchor, bars))
                bars = []
            anchor = parse_timestamp(line[1:])
            continue

        repeat = re.fullmatch(r"[xX](\d+)", line)
        if repeat:
            if previous_line is None:
                raise ValueError(f"line {lineno}: 'x{repeat.group(1)}' with no preceding bars")
            for _ in range(int(repeat.group(1))):
                bars.extend(_parse_bars(previous_line, lineno))
            continue

        bars.extend(_parse_bars(line, lineno))
        previous_line = line

    if bars:
        sections.append((anchor, bars))
    if not sections:
        raise ValueError("spec contained no chords")
    return sections


def _parse_bars(line: str, lineno: int) -> list[list[str]]:
    out: list[list[str]] = []
    for cell in line.split("|"):
        chords = cell.split()
        if not chords:
            continue
        for chord in chords:
            if chord == NO_CHORD:
                continue
            try:
                parse_chord(chord)
            except ValueError as exc:
                raise ValueError(f"line {lineno}: {exc}") from exc
        out.append(chords)
    return out


def build(
    downbeats: np.ndarray, duration: float, sections: list[tuple[float | None, list[list[str]]]]
) -> list[tuple[float, float, str]]:
    """Place each bar between consecutive downbeats."""
    if downbeats.size < 2:
        raise SystemExit(
            "No downbeats were detected, so bars can't be placed automatically.\n"
            "Write the .lab by hand, or try --vocabulary/--sensitivity settings "
            "that give a usable beat grid first."
        )

    rows: list[tuple[float, float, str]] = []
    cursor = 0  # index into downbeats

    for anchor, bars in sections:
        if anchor is not None:
            cursor = int(np.argmin(np.abs(downbeats - anchor)))
            snapped = downbeats[cursor]
            if abs(snapped - anchor) > 1.5:
                print(
                    f"  note: anchor {anchor:.1f}s snapped to downbeat "
                    f"{snapped:.1f}s ({snapped - anchor:+.1f}s)",
                    file=sys.stderr,
                )

        for bar in bars:
            if cursor + 1 >= len(downbeats):
                bar_start = downbeats[cursor] if cursor < len(downbeats) else duration
                bar_end = duration
            else:
                bar_start = downbeats[cursor]
                bar_end = downbeats[cursor + 1]
            cursor += 1

            if bar_end <= bar_start:
                continue

            # Split the bar evenly among however many chords it holds.
            edges = np.linspace(bar_start, bar_end, len(bar) + 1)
            for i, chord in enumerate(bar):
                rows.append((float(edges[i]), float(edges[i + 1]), chord))

    return _merge_adjacent(rows)


def _merge_adjacent(rows: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Collapse consecutive identical labels into single spans."""
    merged: list[tuple[float, float, str]] = []
    for start, end, label in rows:
        if merged and merged[-1][2] == label and abs(merged[-1][1] - start) < 1e-6:
            merged[-1] = (merged[-1][0], end, label)
        else:
            merged.append((start, end, label))
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("spec", type=Path, help="Chord sheet (see module docstring)")
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help="Reuse a saved analysis JSON instead of re-analyzing (much faster).",
    )
    args = parser.parse_args()

    if args.from_json:
        data = json.loads(args.from_json.read_text())
        downbeats = np.asarray(data["downbeats"], dtype=float)
        duration = float(data["duration"])
    else:
        analysis = analyze(args.audio)
        downbeats = np.asarray(analysis.downbeats, dtype=float)
        duration = analysis.duration

    sections = parse_spec(args.spec)
    rows = build(downbeats, duration, sections)

    output = args.output or args.audio.with_suffix(".lab")
    output.write_text("".join(f"{s:.3f} {e:.3f} {c}\n" for s, e, c in rows))

    covered = sum(e - s for s, e, _ in rows)
    print(f"Wrote {output}")
    print(
        f"  {len(rows)} spans covering {covered:.0f}s of {duration:.0f}s "
        f"({100 * covered / duration:.0f}% of the track)"
    )
    print(f"  {len(downbeats)} downbeats available")
    print("\nNow score it:")
    print(f"  uv run python scripts/eval.py {args.audio}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
