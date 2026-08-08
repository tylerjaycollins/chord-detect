"""Chord and progression suggestion.

Two related tools, both built on machinery that already exists:

`suggest_at` answers "what chord could go here?" for a point in a song. The
recognizer already scores all 168 chords against the audio at every moment and
throws away everything but the winner -- this keeps the runners-up. Because the
scores come from the actual audio, suggestions fit what's really sounding rather
than only what's theoretically available in the key.

`suggest_progressions` answers "what progressions would fit this song?" It
renders a library of common progressions into the detected key and ranks them by
overlap with the chords the song actually uses, so familiar-sounding options
come first.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .chords import ChordRecognizer, ChordRecognizerConfig
from .features import AudioClip, chroma_features, time_to_frames
from .theory import (
    QUALITY_BY_NAME,
    SHARP_NAMES,
    chord_label,
    label_roman,
    parse_chord,
    prefers_flats,
)

# Scale degrees available in a key, as (semitones above tonic, quality).
_MAJOR_DEGREES: dict[str, tuple[int, str]] = {
    "I": (0, ""), "ii": (2, "m"), "iii": (4, "m"), "IV": (5, ""),
    "V": (7, ""), "vi": (9, "m"), "vii": (11, "dim"),
    "ii7": (2, "m7"), "V7": (7, "7"), "IV7": (5, "maj7"), "I7": (0, "maj7"),
    "bVII": (10, ""), "bIII": (3, ""), "bVI": (8, ""), "IVsus": (5, "sus4"),
    "Isus": (0, "sus4"), "Vsus": (7, "sus4"), "vi7": (9, "m7"), "iii7": (4, "m7"),
}
_MINOR_DEGREES: dict[str, tuple[int, str]] = {
    "i": (0, "m"), "ii": (2, "dim"), "III": (3, ""), "iv": (5, "m"),
    "v": (7, "m"), "V": (7, ""), "VI": (8, ""), "VII": (10, ""),
    "i7": (0, "m7"), "iv7": (5, "m7"), "V7": (7, "7"), "isus": (0, "sus4"),
}

# Progressions worth offering. Names are how a musician would refer to them.
PROGRESSIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("major", "Pop / axis", ("I", "V", "vi", "IV")),
    ("major", "Sensitive (vi first)", ("vi", "IV", "I", "V")),
    ("major", "Classic three-chord", ("I", "IV", "V", "I")),
    ("major", "Doo-wop / 50s", ("I", "vi", "IV", "V")),
    ("major", "Worship standard", ("I", "IV", "vi", "V")),
    ("major", "Descending anthem", ("IV", "I", "V", "vi")),
    ("major", "Jazz turnaround", ("ii7", "V7", "I", "I")),
    ("major", "Mixolydian rock", ("I", "bVII", "IV", "I")),
    ("major", "Pachelbel", ("I", "V", "vi", "iii", "IV", "I", "IV", "V")),
    ("major", "Suspended build", ("I", "Isus", "IV", "V")),
    ("major", "Gospel walk-up", ("I", "iii7", "IV", "V7")),
    ("major", "Plagal / amen", ("IV", "I", "IV", "I")),
    ("minor", "Natural minor loop", ("i", "VI", "III", "VII")),
    ("minor", "Minor plagal", ("i", "iv", "i", "v")),
    ("minor", "Harmonic minor pull", ("i", "iv", "V7", "i")),
    ("minor", "Lament", ("i", "VII", "VI", "V")),
)


@dataclass
class Suggestion:
    """One candidate chord for a point in the song."""

    label: str
    fit: float  # 0-1, how well the audio supports it
    roman: str  # scale degree in the detected key, "" if outside
    diatonic: bool
    is_current: bool  # already the detected chord here

    @property
    def note(self) -> str:
        if self.is_current:
            return "currently detected"
        if not self.roman:
            return "outside the key"
        return "diatonic" if self.diatonic else "borrowed"


@dataclass
class ProgressionSuggestion:
    name: str
    numerals: tuple[str, ...]
    chords: list[str]
    overlap: float  # fraction of its chords the song already uses


def suggest_at(
    clip: AudioClip,
    time: float,
    key: str,
    *,
    current: str | None = None,
    top_n: int = 6,
    window: float = 1.0,
    chroma: np.ndarray | None = None,
) -> list[Suggestion]:
    """Rank chords by how well they fit the audio around `time`.

    `window` is the number of seconds of audio to consider, centered on the
    point. About a beat's worth works well: too narrow and a single passing tone
    dominates, too wide and neighbouring chords blur in.
    """
    if chroma is None:
        chroma = chroma_features(clip)

    half = max(window, 0.1) / 2.0
    lo, hi = time_to_frames(
        np.array([max(time - half, 0.0), time + half]), clip.sr
    )
    lo = int(np.clip(lo, 0, chroma.shape[1] - 1))
    hi = int(np.clip(hi, lo + 1, chroma.shape[1]))

    # Median over the window, matching how the recognizer aggregates per beat.
    local = np.median(chroma[:, lo:hi], axis=1, keepdims=True)
    if local.sum() <= 1e-9:
        return []

    # Full vocabulary here: suggestions should be able to offer colours the
    # conservative recognizer would never commit to on its own.
    recognizer = ChordRecognizer(ChordRecognizerConfig(quality_names=None))
    scores = recognizer.score(local)[:, 0]

    # Softmax over the pitched states gives a comparable 0-1 fit.
    pitched = scores[:-1]
    shifted = pitched - pitched.max()
    weights = np.exp(shifted)
    weights /= weights.sum()

    scale = _scale_pitch_classes(parse_chord(key))

    order = np.argsort(weights)[::-1][:top_n]
    out: list[Suggestion] = []
    for index in order:
        label = recognizer.labels[index]
        out.append(
            Suggestion(
                label=label,
                fit=float(weights[index]),
                roman=label_roman(label, key),
                diatonic=_is_diatonic(label, scale),
                is_current=current is not None and label == current,
            )
        )
    return out


def _scale_pitch_classes(key_parsed: tuple[int, str] | None) -> set[int]:
    if key_parsed is None:
        return set()
    tonic, quality = key_parsed
    steps = (0, 2, 3, 5, 7, 8, 10) if quality == "m" else (0, 2, 4, 5, 7, 9, 11)
    return {(tonic + step) % 12 for step in steps}


def _is_diatonic(label: str, scale: set[int]) -> bool:
    """True only if *every* chord tone belongs to the key.

    Testing the root alone is not enough: Dm shares its root with D major but
    contains F where the key of D has F#, which makes it a borrowed chord. That
    distinction is the whole point of the label.
    """
    parsed = parse_chord(label)
    if parsed is None or not scale:
        return False
    root, quality = parsed
    intervals = QUALITY_BY_NAME[quality].intervals
    return all((root + interval) % 12 in scale for interval in intervals)


def suggest_progressions(
    key: str, used: list[str] | None = None, top_n: int = 8
) -> list[ProgressionSuggestion]:
    """Common progressions rendered into `key`, ranked by fit to the song."""
    parsed = parse_chord(key)
    if parsed is None:
        return []
    tonic, key_quality = parsed
    minor = key_quality == "m"
    table = _MINOR_DEGREES if minor else _MAJOR_DEGREES
    flats = prefers_flats(tonic, minor=minor)

    used_roots: set[tuple[int, bool]] = set()
    for label in used or []:
        chord = parse_chord(label)
        if chord is None:
            continue
        intervals = QUALITY_BY_NAME[chord[1]].intervals
        used_roots.add((chord[0], 3 in intervals and 4 not in intervals))

    out: list[ProgressionSuggestion] = []
    for mode, name, numerals in PROGRESSIONS:
        if (mode == "minor") != minor:
            continue
        chords: list[str] = []
        matched = 0
        for numeral in numerals:
            degree = table.get(numeral)
            if degree is None:
                chords.append("?")
                continue
            offset, quality = degree
            root = (tonic + offset) % 12
            chords.append(chord_label(root, quality, prefer_flats=flats))
            is_minor = 3 in QUALITY_BY_NAME[quality].intervals and 4 not in QUALITY_BY_NAME[quality].intervals
            if (root, is_minor) in used_roots:
                matched += 1
        out.append(
            ProgressionSuggestion(
                name=name,
                numerals=numerals,
                chords=chords,
                overlap=matched / len(numerals) if numerals else 0.0,
            )
        )

    out.sort(key=lambda p: (-p.overlap, p.name))
    return out[:top_n]
