"""Chord-CNN-LSTM: large-vocabulary chord recognition with inversions.

"Large-Vocabulary Chord Transcription via Chord Structure Decomposition"
(Jiang et al., ISMIR 2019), MIT licensed. An ensemble of five CNN-LSTM models
whose frame probabilities are averaged and decoded by an HMM over a chord
dictionary.

Why this over BTC: it recognises **inversions**. Real chord charts are full of
`D/F#`, `G/D`, `G/B`, and neither the chroma templates nor BTC can express them
at all. It also picks up sus chords that BTC systematically omits.

Upstream ships its own mini-framework (`mir/`, `extractors/`, `io_new/`) and its
model weights, all committed in the repo -- so `ensure_model()` is a clone plus
the same numpy-alias patches the 2019 code needs everywhere.

One wrinkle: upstream resolves its chord dictionaries by *relative* path, so
inference has to run with the repo as the working directory. `_in_repo()`
handles that and restores the previous directory afterwards.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from .chords import ChordSpan, merge_short_spans
from .theory import FLAT_NAMES, SHARP_NAMES

REPO_URL = "https://github.com/music-x-lab/ISMIR2019-Large-Vocabulary-Chord-Recognition.git"
HOME = Path.home() / ".cache" / "chord-detect" / "cnnlstm"

# Upstream quality names -> this project's spelling.
_QUALITY_MAP = {
    "maj": "", "min": "m", "dim": "dim", "aug": "aug",
    "min6": "m6", "maj6": "6", "min7": "m7", "maj7": "maj7",
    "7": "7", "dim7": "dim7", "hdim7": "m7b5",
    "sus2": "sus2", "sus4": "sus4", "minmaj7": "m",
    "maj9": "maj7", "min9": "m7", "9": "7", "11": "7", "13": "7",
    "sus4(b7)": "7sus4", "sus4(b7,9)": "7sus4",
}

# Harte writes the bass as a scale degree relative to the root ("/3", "/5",
# "/b7"), not as a note name -- so it has to be resolved against the root.
_DEGREE_SEMITONES = {
    "1": 0, "b2": 1, "2": 2, "b3": 3, "3": 4, "4": 5, "#4": 6,
    "b5": 6, "5": 7, "#5": 8, "b6": 8, "6": 9, "bb7": 9, "b7": 10,
    "7": 11, "9": 2, "b9": 1, "#9": 3, "11": 5, "#11": 6, "13": 9, "b13": 8,
}


def is_installed() -> bool:
    return (HOME / "chord_recognition.py").exists() and any(
        (HOME / "cache_data").glob("*.sdict")
    )


def ensure_model(quiet: bool = False) -> Path:
    """Clone upstream and patch it for modern numpy."""
    if is_installed():
        return HOME

    HOME.parent.mkdir(parents=True, exist_ok=True)
    if not quiet:
        print(f"Fetching Chord-CNN-LSTM into {HOME} (~56 MB, one time)…")
    subprocess.run(
        ["git", "clone", "--depth", "1", "-q", REPO_URL, str(HOME)], check=True
    )
    _patch_for_modern_deps()
    return HOME


def _patch_for_modern_deps() -> None:
    """`np.float` and friends were removed in numpy 1.24."""
    import re

    aliases = [
        (re.compile(r"\bnp\.float\b(?!\d)"), "float"),
        (re.compile(r"\bnp\.int\b(?!\d)"), "int"),
        (re.compile(r"\bnp\.bool\b(?!_)"), "bool"),
        (re.compile(r"\bnp\.object\b"), "object"),
    ]
    for path in HOME.rglob("*.py"):
        text = original = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, replacement in aliases:
            text = pattern.sub(replacement, text)
        if text != original:
            path.write_text(text)


@contextlib.contextmanager
def _in_repo():
    """Run with the repo as cwd; upstream resolves data files relatively."""
    ensure_model()
    previous = os.getcwd()
    if str(HOME) not in sys.path:
        sys.path.insert(0, str(HOME))
    os.chdir(HOME)
    try:
        yield
    finally:
        os.chdir(previous)


def harte_to_label(chord: str) -> str:
    """'Eb:maj/3' -> 'Eb/G'; 'C:min7' -> 'Cm7'; 'N'/'X' -> 'N'."""
    if chord in ("N", "X", ""):
        return "N"

    body, _, degree = chord.partition("/")
    if ":" in body:
        root, quality = body.split(":", 1)
        suffix = _QUALITY_MAP.get(quality, "")
    else:
        root, quality, suffix = body, "maj", ""

    label = f"{root}{suffix}"
    if not degree:
        return label

    offset = _DEGREE_SEMITONES.get(degree)
    if offset is None:
        return label

    table = FLAT_NAMES if "b" in root else SHARP_NAMES
    if root not in SHARP_NAMES and root not in FLAT_NAMES:
        return label
    root_pc = (SHARP_NAMES.index(root) if root in SHARP_NAMES
               else FLAT_NAMES.index(root))
    bass = table[(root_pc + offset) % 12]
    # A bass that equals the root is root position, not an inversion.
    return label if bass == root else f"{label}/{bass}"


def recognize(
    audio_path: str | Path,
    *,
    chord_dict: str = "submission",
    min_duration: float = 0.28,
    inversions: bool = True,
) -> list[ChordSpan]:
    """Run the ensemble over a file and return chord spans."""
    with _in_repo():
        from chord_recognition import chord_recognition  # type: ignore

        with tempfile.NamedTemporaryFile(suffix=".lab", delete=False) as handle:
            out_path = Path(handle.name)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                chord_recognition(
                    str(Path(audio_path).resolve()), str(out_path), chord_dict
                )
            raw = out_path.read_text()
        finally:
            out_path.unlink(missing_ok=True)

    spans: list[ChordSpan] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        start, end, harte = float(parts[0]), float(parts[1]), parts[2]
        label = harte_to_label(harte)
        if not inversions:
            label = label.split("/")[0]
        spans.append(
            ChordSpan(
                label=label,
                start=start,
                end=end,
                # The HMM emits a decoded path, not a calibrated per-chord
                # probability, so a flat nominal value is more honest than a
                # number dressed up to look like the template model's posterior.
                confidence=0.75,
            )
        )

    return merge_short_spans(spans, min_duration=min_duration)
