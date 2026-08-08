"""Key estimation via Krumhansl-Schmuckler profile correlation.

Knowing the key is what turns a list of chord symbols into something readable:
it decides sharps vs flats, enables Roman-numeral/Nashville numbering, and
tells you which capo positions are worth suggesting.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .chords import ChordSpan
from .theory import FLAT_NAMES, SHARP_NAMES, parse_chord
from .theory import prefers_flats as _prefers_flats

# Krumhansl & Kessler (1982) key profiles: perceived stability of each scale
# degree, measured from listener ratings.
MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)


@dataclass
class KeyEstimate:
    tonic: int  # pitch class
    mode: str  # "major" or "minor"
    confidence: float  # gap between best and runner-up correlation

    @property
    def name(self) -> str:
        suffix = "" if self.mode == "major" else "m"
        return f"{SHARP_NAMES[self.tonic]}{suffix}"

    @property
    def prefers_flats(self) -> bool:
        """Whether charts in this key are conventionally spelled with flats."""
        return _prefers_flats(self.tonic, minor=self.mode == "minor")

    @property
    def display_name(self) -> str:
        """Key name spelled the way a chart would write it."""
        names = FLAT_NAMES if self.prefers_flats else SHARP_NAMES
        suffix = "" if self.mode == "major" else "m"
        return f"{names[self.tonic]}{suffix}"


def estimate_key(chroma: np.ndarray, weights: np.ndarray | None = None) -> KeyEstimate:
    """Correlate the aggregate chroma against all 24 rotated profiles.

    `weights` optionally scales each frame's contribution -- passing chord
    durations weights sustained harmony above passing chords.
    """
    if chroma.shape[1] == 0:
        return KeyEstimate(tonic=0, mode="major", confidence=0.0)

    if weights is not None and weights.size == chroma.shape[1]:
        profile = (chroma * weights[None, :]).sum(axis=1)
    else:
        profile = chroma.sum(axis=1)

    if profile.sum() <= 1e-9:
        return KeyEstimate(tonic=0, mode="major", confidence=0.0)

    profile = profile / profile.sum()

    scores: list[tuple[float, int, str]] = []
    for tonic in range(12):
        for mode, reference in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = np.roll(reference, tonic)
            scores.append((_correlate(profile, rotated), tonic, mode))

    scores.sort(reverse=True)
    best, tonic, mode = scores[0]
    runner_up = scores[1][0]
    # Normalize the margin into roughly 0..1 so it reads as a confidence.
    confidence = float(np.clip((best - runner_up) / max(abs(best), 1e-6), 0.0, 1.0))

    return KeyEstimate(tonic=tonic, mode=mode, confidence=confidence)


def estimate_key_from_chords(spans: list[ChordSpan]) -> KeyEstimate:
    """Alternative estimate built from recognized chords rather than raw chroma.

    Often steadier than the chroma estimate, because it reasons over harmony
    the recognizer already committed to instead of raw spectral energy.
    """
    histogram = np.zeros(12)
    for span in spans:
        parsed = parse_chord(span.label)
        if parsed is None:
            continue
        root, quality = parsed
        from .theory import QUALITY_BY_NAME

        for interval in QUALITY_BY_NAME[quality].intervals:
            # Weight by duration, and give the root extra pull.
            weight = span.duration * (1.6 if interval == 0 else 1.0)
            histogram[(root + interval) % 12] += weight

    if histogram.sum() <= 0:
        return KeyEstimate(tonic=0, mode="major", confidence=0.0)

    return estimate_key(histogram[:, None])


def _correlate(a: np.ndarray, b: np.ndarray) -> float:
    a_centered = a - a.mean()
    b_centered = b - b.mean()
    denominator = np.linalg.norm(a_centered) * np.linalg.norm(b_centered)
    if denominator < 1e-12:
        return 0.0
    return float(np.dot(a_centered, b_centered) / denominator)
