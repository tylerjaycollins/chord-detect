"""Music theory primitives: pitch classes, chord vocabulary, chroma templates.

Everything downstream (recognition, transposition, capo, export) agrees on the
conventions here: pitch class 0 == C, and a chord is a (root, quality) pair.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Pitch classes of the keys conventionally written with flats, so chords get
# spelled the way a chart would write them: Bb, not A#.
FLAT_MAJOR_KEYS = {5, 10, 3, 8, 1, 6}  # F Bb Eb Ab Db Gb
FLAT_MINOR_KEYS = {2, 7, 0, 5, 10, 3}  # d g c f bb eb


def prefers_flats(root: int, minor: bool = False) -> bool:
    """Whether a key on `root` is conventionally spelled with flats."""
    table = FLAT_MINOR_KEYS if minor else FLAT_MAJOR_KEYS
    return (root % 12) in table

NO_CHORD = "N"


@dataclass(frozen=True)
class Quality:
    """A chord quality: how it's spelled, and which intervals it contains."""

    name: str  # suffix appended to the root, e.g. "m7"
    intervals: tuple[int, ...]  # semitones above the root
    # Roughly how often this quality occurs in popular/congregational music,
    # as a probability over all chords. Used as a genuine Bayesian prior rather
    # than a hand-tuned nudge -- see `log_prior`.
    probability: float

    @property
    def log_prior(self) -> float:
        return float(np.log(self.probability))


# Frequencies approximate published corpus statistics for popular music (major
# and minor triads dominate overwhelmingly; sus and power chords are rare).
#
# These exist because template matching has a strong bias toward rare, exotic
# qualities. Two mechanisms drive it: a superset template always explains more
# observed energy than its parent triad, and `sus4` in particular is built from
# the I/IV/V degrees that are also the most common *roots* in a song, so energy
# bleeding between adjacent chords reads as sus4. Worse, sus4's fourth has the
# root as its own 3rd harmonic, so the template reinforces the root and matches
# root-heavy chroma even better.
#
# An earlier hand-guessed version of this table produced 36% sus4 and 23% power
# chords on a real worship recording. Do not "simplify" these back to uniform.
QUALITIES: tuple[Quality, ...] = (
    Quality("", (0, 4, 7), 0.600),  # major
    Quality("m", (0, 3, 7), 0.200),  # minor
    Quality("7", (0, 4, 7, 10), 0.060),  # dominant 7
    Quality("m7", (0, 3, 7, 10), 0.045),
    Quality("maj7", (0, 4, 7, 11), 0.015),
    Quality("sus4", (0, 5, 7), 0.015),
    Quality("5", (0, 7), 0.010),  # power chord / open fifth
    Quality("sus2", (0, 2, 7), 0.008),
    Quality("6", (0, 4, 7, 9), 0.008),
    Quality("dim", (0, 3, 6), 0.005),
    Quality("m6", (0, 3, 7, 9), 0.004),
    Quality("m7b5", (0, 3, 6, 10), 0.004),
    Quality("dim7", (0, 3, 6, 9), 0.003),
    Quality("aug", (0, 4, 8), 0.002),
)

QUALITY_BY_NAME = {q.name: q for q in QUALITIES}

# A deliberately small vocabulary that covers the overwhelming majority of
# congregational and pop charts. Fewer confusable templates means fewer exotic
# false positives, which matters more in practice than covering rare chords.
SIMPLE_QUALITY_NAMES = ("", "m", "7", "m7", "sus4")


def chord_label(root: int, quality: str, *, prefer_flats: bool = False) -> str:
    """Render a (root, quality) pair as a chart symbol, e.g. (9, 'm7') -> 'Am7'."""
    names = FLAT_NAMES if prefer_flats else SHARP_NAMES
    return f"{names[root % 12]}{quality}"


def split_slash(label: str) -> tuple[str, str | None]:
    """Split 'D/F#' into ('D', 'F#'). Returns (label, None) when there's no bass.

    Slash chords are carried as plain strings rather than a richer type so that
    every existing call site keeps working: `parse_chord` reads the part before
    the slash, and only code that cares about the bass has to ask for it.
    """
    if "/" not in label:
        return label, None
    base, _, bass = label.partition("/")
    return base, bass or None


def parse_chord(label: str) -> tuple[int, str] | None:
    """Root and quality of a chord symbol, ignoring any slash bass.

    Returns None for the no-chord symbol.
    """
    if label in (NO_CHORD, "", None):
        return None
    label, _ = split_slash(label)
    # Longest root spelling first so "C#" isn't read as "C".
    for length in (2, 1):
        head, tail = label[:length], label[length:]
        for table in (SHARP_NAMES, FLAT_NAMES):
            if head in table and tail in QUALITY_BY_NAME:
                return table.index(head), tail
    raise ValueError(f"unrecognized chord symbol: {label!r}")


def transpose_label(label: str, semitones: int, *, prefer_flats: bool = False) -> str:
    """Shift a chord symbol by `semitones`, preserving quality and any bass."""
    parsed = parse_chord(label)
    if parsed is None:
        return NO_CHORD
    root, quality = parsed
    shifted = chord_label(root + semitones, quality, prefer_flats=prefer_flats)

    # A slash bass has to move with the chord, or a capo'd chart silently
    # reports the wrong bass note.
    _, bass = split_slash(label)
    if bass:
        names = FLAT_NAMES if prefer_flats else SHARP_NAMES
        for table in (SHARP_NAMES, FLAT_NAMES):
            if bass in table:
                moved = (table.index(bass) + semitones) % 12
                return f"{shifted}/{names[moved]}"
    return shifted


# --------------------------------------------------------------------------
# Capo / shape families
# --------------------------------------------------------------------------

# The open-position keys a guitarist actually has shapes for. Each covers its
# relative minor too -- "G shapes" means G, C, D, Em, Am, i.e. the whole open-G
# vocabulary, so a song in Em is served by the same family as one in G.
SHAPE_KEYS: tuple[str, ...] = ("G", "C", "D", "A", "E")

# Above this the capo crowds the body and intonation suffers, so a different
# shape family is usually the better answer.
DEFAULT_MAX_CAPO = 7


def capo_for_shape(key_label: str, shape: str) -> int | None:
    """Fret needed to play `shape` family and have it sound in `key_label`.

    Minor keys are matched against the family's relative minor, so a song in Am
    resolves to Em shapes within the G family rather than to something unplayable.
    """
    key = parse_chord(key_label)
    shape_parsed = parse_chord(shape)
    if key is None or shape_parsed is None:
        return None

    tonic, quality = key
    shape_root = shape_parsed[0]
    if quality == "m":
        # Relative minor of the shape family sits a minor third below it.
        shape_root = (shape_root - 3) % 12

    return (tonic - shape_root) % 12


def choose_shapes(
    key_label: str,
    preferences: list[str],
    max_capo: int = DEFAULT_MAX_CAPO,
) -> tuple[int, str] | None:
    """Pick the first preferred shape family reachable within `max_capo`.

    Falls back to whichever preference needs the lowest capo, so a request is
    always honoured with *something* rather than silently ignored.
    """
    scored: list[tuple[int, str]] = []
    for shape in preferences:
        capo = capo_for_shape(key_label, shape)
        if capo is None:
            continue
        scored.append((capo, shape))

    if not scored:
        return None

    for capo, shape in scored:  # preference order wins when it fits
        if capo <= max_capo:
            return capo, shape

    return min(scored)


def shape_key_label(key_label: str, capo: int) -> str:
    """How the key is spelled once a capo is applied, e.g. C + capo 5 -> G."""
    key = parse_chord(key_label)
    if key is None:
        return key_label
    root, quality = key
    shifted = (root - capo) % 12
    return chord_label(
        shifted, quality, prefer_flats=prefers_flats(shifted, minor=quality == "m")
    )


# --------------------------------------------------------------------------
# Scale degrees
# --------------------------------------------------------------------------

# Numeral for each semitone above the tonic. Non-diatonic degrees get the
# spelling a chart would use (bIII rather than #II, and so on).
_MAJOR_DEGREES = {
    0: "I", 1: "bII", 2: "II", 3: "bIII", 4: "III", 5: "IV",
    6: "#IV", 7: "V", 8: "bVI", 9: "VI", 10: "bVII", 11: "VII",
}
_MINOR_DEGREES = {
    0: "I", 1: "bII", 2: "II", 3: "III", 4: "#III", 5: "IV",
    6: "#IV", 7: "V", 8: "VI", 9: "#VI", 10: "VII", 11: "#VII",
}


def roman_numeral(
    root: int, quality: str, key_root: int, key_minor: bool = False
) -> str:
    """Scale degree of a chord within a key, e.g. (9, 'm', 0) -> 'vi'.

    Case carries the quality, as is conventional: uppercase for major,
    lowercase for minor, with a trailing marker for diminished and augmented.
    """
    table = _MINOR_DEGREES if key_minor else _MAJOR_DEGREES
    numeral = table[(root - key_root) % 12]

    intervals = QUALITY_BY_NAME.get(quality, QUALITY_BY_NAME[""]).intervals
    is_minor = 3 in intervals and 4 not in intervals

    if is_minor:
        numeral = numeral.lower()
    if 6 in intervals and 7 not in intervals:  # diminished fifth
        numeral += "°"
    elif 8 in intervals:  # augmented fifth
        numeral += "+"

    return numeral


def label_roman(label: str, key_label: str) -> str:
    """Roman numeral for a chord symbol within a key symbol. '' if unknown."""
    chord = parse_chord(label)
    key = parse_chord(key_label)
    if chord is None or key is None:
        return ""
    return roman_numeral(chord[0], chord[1], key[0], key_minor=key[1] == "m")


# --------------------------------------------------------------------------
# Chroma templates
# --------------------------------------------------------------------------

# Relative weight of each chord tone in the template. The root and fifth
# dominate the low harmonics of real instruments, the third is what
# distinguishes quality, and extensions are comparatively faint.
_TONE_WEIGHTS = {
    0: 1.00,  # root
    7: 0.80,  # fifth
    6: 0.75,  # diminished fifth
    8: 0.75,  # augmented fifth
    3: 0.90,  # minor third
    4: 0.90,  # major third
    2: 0.65,  # ninth (sus2)
    5: 0.70,  # fourth (sus4)
    9: 0.60,  # sixth / diminished seventh
    10: 0.65,  # minor seventh
    11: 0.60,  # major seventh
}


# Number of harmonics to model per chord tone, and how fast they decay.
# Real instruments aren't sine waves: a played C also radiates G (3rd
# harmonic), E (5th), and Bb (7th). Ignoring this makes every major triad look
# like a maj7, because the third's own 3rd harmonic *is* the major seventh
# (E -> B in a C chord). Templates that predict the leakage stop being fooled
# by it -- a real maj7 then has to show *more* seventh than physics accounts for.
N_HARMONICS = 8
HARMONIC_DECAY = 0.6


def _harmonic_pitch_classes(n: int = N_HARMONICS) -> list[tuple[int, float]]:
    """(pitch-class offset, amplitude) for the first `n` harmonics of a note."""
    out: list[tuple[int, float]] = []
    for k in range(1, n + 1):
        semitones = int(round(12 * np.log2(k))) % 12
        out.append((semitones, HARMONIC_DECAY ** (k - 1)))
    return out


@dataclass(frozen=True)
class Vocabulary:
    """The chord vocabulary, precomputed for scoring."""

    templates: np.ndarray  # (n_chords, 12) harmonic-aware, L2-normalized
    support: np.ndarray  # (n_chords, 12) binary: actual chord tones only
    labels: list[str]
    log_priors: np.ndarray  # natural log of each chord's prior probability


def build_templates(quality_names: tuple[str, ...] | None = None) -> Vocabulary:
    """Build harmonic-aware chroma templates.

    `quality_names` restricts the vocabulary; None uses everything in
    :data:`QUALITIES`.
    """
    if quality_names is None:
        qualities = QUALITIES
    else:
        unknown = set(quality_names) - set(QUALITY_BY_NAME)
        if unknown:
            raise ValueError(f"unknown qualities: {sorted(unknown)}")
        qualities = tuple(QUALITY_BY_NAME[n] for n in quality_names)

    harmonics = _harmonic_pitch_classes()

    templates: list[np.ndarray] = []
    supports: list[np.ndarray] = []
    labels: list[str] = []
    log_priors: list[float] = []

    for root in range(12):
        for quality in qualities:
            vec = np.zeros(12, dtype=np.float32)
            support = np.zeros(12, dtype=np.float32)

            for interval in quality.intervals:
                pitch_class = (root + interval) % 12
                weight = _TONE_WEIGHTS[interval]
                support[pitch_class] = 1.0
                for offset, amplitude in harmonics:
                    vec[(pitch_class + offset) % 12] += weight * amplitude

            # Unit-normalize so a dot product against a chroma frame is a
            # cosine similarity.
            vec /= np.linalg.norm(vec)
            templates.append(vec)
            supports.append(support)
            labels.append(chord_label(root, quality.name))
            log_priors.append(quality.log_prior)

    return Vocabulary(
        templates=np.stack(templates),
        support=np.stack(supports),
        labels=labels,
        log_priors=np.asarray(log_priors, dtype=np.float32),
    )
