"""Use an isolated stem to build ground truth, then score the full mix against it.

Chord detection on a clean harmonic stem (guitar, piano, keys) is far easier
than on a full mix: no drums smearing broadband energy across all twelve pitch
classes, no vocal melody adding non-chord tones. So the stem's output makes a
usable stand-in for hand annotation -- which is the bottleneck this project has
never gotten past.

What it gives us:

  1. A timed .lab for the full mix, free, without anyone annotating by ear.
  2. A precise list of *where* the mix disagrees with the stem, with timestamps
     to listen to, so degradation can be diagnosed rather than guessed at.

Important caveat: this is pseudo ground truth, not ground truth. Errors on the
stem propagate silently into the "answer". Two guards are worth using --
spot-check the disagreement timestamps by ear, and where a published chord chart
exists, check the stem's chord vocabulary against it first.

Usage:
    uv run python scripts/compare_stems.py mix.mp3 --stem guitar.wav
    uv run python scripts/compare_stems.py mix.mp3 --stem gtr.wav --stem keys.wav \
        --lab testdata/song.lab
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chord_detect.chords import ChordRecognizerConfig  # noqa: E402
from chord_detect.pipeline import Analysis, analyze  # noqa: E402
from chord_detect.theory import QUALITY_BY_NAME, parse_chord  # noqa: E402

STEP = 0.05  # seconds between comparison samples


def label_at(analysis: Analysis, t: float) -> str:
    span = analysis.chord_at(t)
    return span.label if span else "N"


def same_root(a: str, b: str) -> bool:
    pa, pb = parse_chord(a) if a != "N" else None, parse_chord(b) if b != "N" else None
    return bool(pa and pb and pa[0] == pb[0])


def same_triad_quality(a: str, b: str) -> bool:
    """Whether both are minor-ish or both major-ish, ignoring extensions."""
    pa, pb = parse_chord(a) if a != "N" else None, parse_chord(b) if b != "N" else None
    if not (pa and pb):
        return False
    ma = 3 in QUALITY_BY_NAME[pa[1]].intervals and 4 not in QUALITY_BY_NAME[pa[1]].intervals
    mb = 3 in QUALITY_BY_NAME[pb[1]].intervals and 4 not in QUALITY_BY_NAME[pb[1]].intervals
    return ma == mb


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mix", type=Path, help="The full mix")
    parser.add_argument("--stem", type=Path, action="append", required=True,
                        help="Isolated harmonic stem (repeatable)")
    parser.add_argument("--lab", type=Path, default=None,
                        help="Write the stem-derived annotation here")
    parser.add_argument("--sensitivity", type=float, default=0.92)
    parser.add_argument("--show", type=int, default=12,
                        help="How many disagreement ranges to print")
    args = parser.parse_args()

    config = ChordRecognizerConfig(self_transition=args.sensitivity)

    print(f"Analyzing mix: {args.mix.name}")
    mix = analyze(args.mix, config=config)
    print(f"  key {mix.key}  tempo {mix.tempo:.0f}  {len(mix.chords)} spans")

    stems: list[tuple[Path, Analysis]] = []
    for path in args.stem:
        print(f"Analyzing stem: {path.name}")
        stem = analyze(path, config=config)
        print(f"  key {stem.key}  tempo {stem.tempo:.0f}  {len(stem.chords)} spans")
        stems.append((path, stem))

    # The stem with the most confident chords is the reference. A stem that's
    # mostly silence or single notes would otherwise poison the comparison.
    reference_path, reference = max(
        stems,
        key=lambda pair: float(
            np.mean([s.confidence for s in pair[1].chords]) if pair[1].chords else 0
        ),
    )
    print(f"\nUsing {reference_path.name} as the reference.")

    if reference.key != mix.key:
        print(
            f"  ⚠ stem key ({reference.key}) disagrees with mix key ({mix.key}) — "
            "check the stem is the same recording and not transposed."
        )

    duration = min(mix.duration, reference.duration)
    samples = np.arange(0.0, duration, STEP)

    exact = roots = quality = counted = 0
    disagreements: list[tuple[float, str, str]] = []
    for t in samples:
        want = label_at(reference, float(t))
        got = label_at(mix, float(t))
        if want == "N":
            continue  # stem silent here: it can't adjudicate
        counted += 1
        if want == got:
            exact += 1
            roots += 1
            quality += 1
            continue
        if same_root(want, got):
            roots += 1
        if same_triad_quality(want, got):
            quality += 1
        disagreements.append((float(t), want, got))

    if not counted:
        print("\nThe stem produced no chords to compare against.")
        return 1

    print(f"\n{'=' * 64}")
    print(f"Full mix vs {reference_path.name}   ({counted} samples)")
    print(f"{'=' * 64}")
    print(f"  exact agreement        {exact / counted:6.1%}")
    print(f"  root agreement         {roots / counted:6.1%}")
    print(f"  major/minor agreement  {quality / counted:6.1%}")
    print(f"  disagreeing time       {STEP * len(disagreements):6.1f}s")

    if roots / counted - exact / counted > 0.12:
        print(
            "\n  → Roots track the stem but qualities don't: the mix is losing "
            "thirds\n    and extensions, not the harmony itself."
        )

    # Collapse consecutive disagreements into ranges worth listening to.
    ranges: list[tuple[float, float, str, str]] = []
    for t, want, got in disagreements:
        if ranges and abs(t - ranges[-1][1]) < STEP * 1.5 and \
           ranges[-1][2] == want and ranges[-1][3] == got:
            ranges[-1] = (ranges[-1][0], t, want, got)
        else:
            ranges.append((t, t, want, got))
    ranges = [r for r in ranges if r[1] - r[0] >= 0.3]
    ranges.sort(key=lambda r: r[1] - r[0], reverse=True)

    if ranges:
        print(f"\n  Longest disagreements (listen to these):")
        for start, end, want, got in ranges[: args.show]:
            print(
                f"    {int(start // 60)}:{start % 60:05.2f} – "
                f"{int(end // 60)}:{end % 60:05.2f}  "
                f"stem says {want:<7} mix says {got}"
            )

    if args.lab:
        args.lab.write_text(
            "".join(
                f"{s.start:.3f} {s.end:.3f} {s.label}\n"
                for s in reference.chords
                if s.label != "N"
            )
        )
        print(f"\nWrote stem-derived annotation to {args.lab}")
        print("  Spot-check a few of the ranges above before trusting it.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
