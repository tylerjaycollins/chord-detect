"""Batch-evaluate chord detection over a folder of audio.

Two modes, and the first needs no work from you:

  Sanity report (no annotations required)
      Runs every file and reports things that are checkable without ground
      truth: what fraction of the track is 7th chords vs triads, how many
      chords fall outside the detected key, mean confidence, how much time
      sits below the confidence floor. The quality histogram is the direct
      probe for the failure mode that was hardest to kill -- triads being
      inflated to 7th chords. If a hymn comes back 60% maj7, that's the bug
      returning, and it shows up without anyone labeling a thing.

  Scored (needs a .lab file next to the audio)
      Frame-level accuracy against timed labels, reported both as exact
      chord-symbol match and root-only match. The gap between those two
      numbers localizes the problem: high root accuracy with low exact
      accuracy means roots are fine and quality is wrong, which is a
      template/prior issue, not a chroma or timing issue.

Usage:
    uv run python scripts/eval.py testdata/
    uv run python scripts/eval.py testdata/ --sensitivity 0.95
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chord_detect.chords import ChordRecognizerConfig  # noqa: E402
from chord_detect.pipeline import Analysis, analyze  # noqa: E402
from chord_detect.theory import QUALITY_BY_NAME, parse_chord  # noqa: E402

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aiff", ".aif"}

# Scale degrees present in a major and a natural-minor key, used only to flag
# chords worth a second look -- borrowed chords are normal, so this is a smell
# test rather than a correctness check.
MAJOR_SCALE = {0, 2, 4, 5, 7, 9, 11}
MINOR_SCALE = {0, 2, 3, 5, 7, 8, 10}


def find_audio(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)


# ---------------------------------------------------------------- annotations


def load_lab(path: Path) -> list[tuple[float, float, str]]:
    """Read `start end label` triples, one per line (Isophonics .lab format)."""
    rows: list[tuple[float, float, str]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        rows.append((float(parts[0]), float(parts[1]), parts[2]))
    return rows


def label_at(rows: list[tuple[float, float, str]], t: float) -> str | None:
    for start, end, label in rows:
        if start <= t < end:
            return label
    return None


def score(analysis: Analysis, truth: list[tuple[float, float, str]]) -> dict:
    """Frame-level accuracy at 10 ms resolution, exact and root-only."""
    exact = roots = total = 0
    quality_confusion: Counter[tuple[str, str]] = Counter()

    for t in np.arange(0.0, analysis.duration, 0.01):
        expected = label_at(truth, float(t))
        if expected is None:
            continue
        got_span = analysis.chord_at(float(t))
        got = got_span.label if got_span else "N"
        total += 1

        if got == expected:
            exact += 1
            roots += 1
            continue

        want = _safe_parse(expected)
        have = _safe_parse(got)
        if want and have and want[0] == have[0]:
            roots += 1
            quality_confusion[(want[1] or "maj", have[1] or "maj")] += 1
        elif want and have:
            quality_confusion[(expected, got)] += 1

    return {
        "exact": exact / total if total else 0.0,
        "root": roots / total if total else 0.0,
        "frames": total,
        "confusion": quality_confusion.most_common(5),
    }


def _safe_parse(label: str) -> tuple[int, str] | None:
    try:
        return parse_chord(label)
    except ValueError:
        return None


# --------------------------------------------------------------- sanity report


def sanity(analysis: Analysis) -> dict:
    """Checks that need no ground truth."""
    total_time = sum(s.duration for s in analysis.chords) or 1.0

    seventh_time = 0.0
    triad_time = 0.0
    quality_time: Counter[str] = Counter()
    for span in analysis.chords:
        parsed = _safe_parse(span.label)
        if parsed is None:
            quality_time["N"] += span.duration
            continue
        _, quality = parsed
        quality_time[quality or "maj"] += span.duration
        n_tones = len(QUALITY_BY_NAME[quality].intervals)
        if n_tones >= 4:
            seventh_time += span.duration
        else:
            triad_time += span.duration

    key_parsed = _safe_parse(analysis.key) or (0, "")
    tonic = key_parsed[0]
    scale = MINOR_SCALE if key_parsed[1] == "m" else MAJOR_SCALE
    outside = 0.0
    outside_labels: Counter[str] = Counter()
    for span in analysis.chords:
        parsed = _safe_parse(span.label)
        if parsed is None:
            continue
        if (parsed[0] - tonic) % 12 not in scale:
            outside += span.duration
            outside_labels[span.label] += span.duration

    weighted_conf = (
        sum(s.confidence * s.duration for s in analysis.chords) / total_time
    )
    shaky = sum(s.duration for s in analysis.chords if s.confidence < 0.35)

    return {
        "seventh_pct": 100 * seventh_time / total_time,
        "triad_pct": 100 * triad_time / total_time,
        "outside_key_pct": 100 * outside / total_time,
        "outside_labels": outside_labels.most_common(4),
        "confidence": weighted_conf,
        "shaky_pct": 100 * shaky / total_time,
        "n_distinct": len({s.label for s in analysis.chords}),
        "quality_time": quality_time.most_common(6),
        "changes_per_min": 60 * len(analysis.chords) / max(analysis.duration, 1e-6),
    }


# ---------------------------------------------------------------------- report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Audio file or folder")
    parser.add_argument("--sensitivity", type=float, default=0.92)
    parser.add_argument("--no-beat-sync", action="store_true")
    args = parser.parse_args()

    files = find_audio(args.path)
    if not files:
        print(f"No audio found in {args.path}")
        print(f"Recognized extensions: {' '.join(sorted(AUDIO_SUFFIXES))}")
        return 1

    config = ChordRecognizerConfig(self_transition=args.sensitivity)
    scored: list[float] = []

    for path in files:
        print(f"\n{'=' * 72}\n{path.name}\n{'=' * 72}")
        try:
            analysis = analyze(path, config=config, beat_sync=not args.no_beat_sync)
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue

        s = sanity(analysis)
        print(
            f"  key {analysis.key:<4} tempo {analysis.tempo:6.1f}  "
            f"meter {str(analysis.meter or '?'):>3}  "
            f"{analysis.duration/60:.1f}min  via {analysis.beat_source}"
        )
        print(
            f"  chords: {len(analysis.chords):>4} spans, {s['n_distinct']} distinct, "
            f"{s['changes_per_min']:.1f} changes/min"
        )
        print(
            f"  quality: {s['triad_pct']:.0f}% triads / {s['seventh_pct']:.0f}% 4-note"
            f"    confidence {s['confidence']:.2f}"
            f"    shaky {s['shaky_pct']:.0f}%"
        )
        print(
            "  mix:     "
            + ", ".join(f"{q} {100*t/max(analysis.duration,1e-9):.0f}%"
                        for q, t in s["quality_time"])
        )
        if s["outside_key_pct"] > 1:
            labels = ", ".join(f"{l}" for l, _ in s["outside_labels"])
            print(f"  outside {analysis.key}: {s['outside_key_pct']:.0f}%  ({labels})")

        # Flag the failure mode that was hardest to kill.
        if s["seventh_pct"] > 45:
            print(
                "  >> WARNING: mostly 4-note chords. If this track is really "
                "triad-based,\n     the 7th-inflation bug is back -- see "
                "features._subtract_noise_floor."
            )

        lab = path.with_suffix(".lab")
        if lab.exists():
            result = score(analysis, load_lab(lab))
            scored.append(result["exact"])
            print(
                f"  SCORED: exact {result['exact']:.1%}   "
                f"root-only {result['root']:.1%}   ({result['frames']} frames)"
            )
            if result["confusion"]:
                print("  top confusions (want -> got):")
                for (want, got), n in result["confusion"]:
                    print(f"      {want:>8} -> {got:<8} {n/100:.1f}s")
            if result["root"] - result["exact"] > 0.15:
                print(
                    "  >> Roots are much better than exact match: this is a "
                    "quality\n     problem (templates/priors), not chroma or timing."
                )
        else:
            print(f"  (no {lab.name} -- sanity report only)")

    if scored:
        print(f"\n{'=' * 72}")
        print(f"mean exact accuracy across {len(scored)} annotated files: "
              f"{sum(scored)/len(scored):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
