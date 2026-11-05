"""End-to-end analysis: audio file in, structured chord chart out.

This is the single entry point every front end uses -- the CLI today, the
desktop UI next. Keeping the whole pipeline behind one function with one result
type means swapping in a better chord model later touches nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

import numpy as np

from .beats import BeatGrid, track_beats
from .chords import ChordRecognizer, ChordRecognizerConfig, ChordSpan, merge_short_spans
from .features import (
    AudioClip,
    beat_synchronous,
    chroma_features,
    frame_energy,
    frames_to_time,
    load_audio,
    time_to_frames,
)
from .key import KeyEstimate, estimate_key, estimate_key_from_chords
from .meter import SIGNATURES, lock_grid, snap_changes
from .theory import transpose_label

ProgressFn = Callable[[str, float], None]


@dataclass
class Measure:
    """One measure's worth of chords, for chart layout."""

    index: int
    start: float
    end: float
    chords: list[str] = field(default_factory=list)


@dataclass
class BeatOptions:
    """Alternative chords that fit one beat, best first."""

    time: float
    options: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class Analysis:
    path: str
    duration: float
    tempo: float
    meter: int | None
    key: str
    key_confidence: float
    beat_source: str
    chords: list[ChordSpan]
    beats: list[float]
    downbeats: list[float]
    measures: list[Measure] = field(default_factory=list)
    # Per-beat alternatives, precomputed so an offline page can answer "what
    # else fits here?" instantly without a Python round trip.
    beat_options: list[BeatOptions] = field(default_factory=list)
    # Set when the whole song was locked onto one steady grid (see meter.py):
    # the signature as musicians write it ("6/8"), every subdivision tick, and
    # which tick starts the first bar. Kept so bars can be redrawn in another
    # signature or phase without re-analysing the audio.
    time_signature: str | None = None
    ticks: list[float] = field(default_factory=list)
    bar_phase: int = 0

    @property
    def signature(self) -> str | None:
        """Time signature for display: "6/8" when locked, else beats-per-bar/4."""
        if self.time_signature:
            return self.time_signature
        return f"{self.meter}/4" if self.meter else None

    # -- views ------------------------------------------------------------

    def transposed(self, semitones: int) -> "Analysis":
        """Return a copy with every chord shifted. Used for capo/transpose."""
        prefer_flats = _wants_flats(self.key, semitones)
        # asdict() flattens nested dataclasses to plain dicts, so anything that
        # must stay typed has to be reattached explicitly. beat_options is
        # deliberately *not* transposed: it stays in sounding pitch so the page
        # can re-spell it at any capo.
        clone = Analysis(
            **{**asdict(self), "chords": [], "measures": [], "beat_options": []}
        )
        clone.beat_options = self.beat_options
        clone.chords = [
            c.transposed(semitones, prefer_flats=prefer_flats) for c in self.chords
        ]
        clone.key = transpose_label(self.key, semitones, prefer_flats=prefer_flats)
        clone.measures = [
            Measure(
                index=m.index,
                start=m.start,
                end=m.end,
                chords=[
                    transpose_label(c, semitones, prefer_flats=prefer_flats)
                    for c in m.chords
                ],
            )
            for m in self.measures
        ]
        return clone

    def chord_at(self, time: float) -> ChordSpan | None:
        """The chord sounding at `time`, for playback cursor sync."""
        for span in self.chords:
            if span.start <= time < span.end:
                return span
        return None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "Analysis":
        """Rebuild an analysis from its stored JSON.

        The inverse of `to_dict`, which is a plain `asdict` -- so this only has
        to reattach the nested dataclasses that flattening turned into dicts.

        Worth having because the app stores rendered HTML rather than rendering
        on demand: without a reader, fixing the renderer can't reach a song
        that's already been analyzed, and recovering it costs a full re-analysis
        of audio whose answers are already on disk.
        """
        return cls(
            path=data["path"],
            duration=data["duration"],
            tempo=data["tempo"],
            meter=data.get("meter"),
            key=data["key"],
            key_confidence=data.get("key_confidence", 0.0),
            beat_source=data.get("beat_source", ""),
            chords=[ChordSpan(**c) for c in data.get("chords", [])],
            beats=list(data.get("beats", [])),
            downbeats=list(data.get("downbeats", [])),
            measures=[Measure(**m) for m in data.get("measures", [])],
            beat_options=[
                BeatOptions(
                    time=b["time"],
                    # JSON has no tuples; the page expects (label, score) pairs.
                    options=[tuple(o) for o in b.get("options", [])],
                )
                for b in data.get("beat_options", [])
            ],
            time_signature=data.get("time_signature"),
            ticks=list(data.get("ticks", [])),
            bar_phase=int(data.get("bar_phase", 0)),
        )

    @classmethod
    def from_json(cls, text: str) -> "Analysis":
        return cls.from_dict(json.loads(text))


def analyze(
    path: str | Path,
    *,
    config: ChordRecognizerConfig | None = None,
    device: str | None = None,
    beat_sync: bool = True,
    suggestions: bool = False,
    model: str = "cnnlstm",
    signature: str | None = None,
    progress: ProgressFn | None = None,
) -> Analysis:
    """Analyze one audio file.

    `beat_sync` aggregates chroma per beat before recognition. It's a large
    accuracy win on anything with a steady pulse, and it snaps chord changes
    onto beats where players expect them. Disable it for free-time material
    (solo piano rubato, ambient) where a wrong beat grid would hurt more than
    the coarse time resolution.
    """

    def report(stage: str, fraction: float) -> None:
        if progress is not None:
            progress(stage, fraction)

    report("Loading audio", 0.0)
    clip = load_audio(path)

    report("Tracking beats", 0.15)
    grid = track_beats(clip, device=device)

    # The learned model works from its own 144-bin CQT, so the chroma front end
    # is only needed for the template recognizer and for per-beat suggestions.
    needs_chroma = model not in ("btc", "cnnlstm") or suggestions
    chroma = None
    if needs_chroma:
        report("Extracting chroma", 0.45)
        chroma = chroma_features(clip)
        energy = frame_energy(clip)

    report("Recognizing chords", 0.75)

    if model in ("btc", "cnnlstm"):
        if model == "cnnlstm":
            from . import cnnlstm

            spans = cnnlstm.recognize(path)
        else:
            from . import btc

            spans = btc.recognize(path, device=device)
        # Suggestions still come from chroma templates -- BTC emits a single
        # label per frame with no usable ranking behind it.
        chroma_seq, boundaries = (None, None)
        if suggestions and chroma is not None:
            chroma_seq, boundaries, _ = _beat_aligned(clip, chroma, energy, grid)
    else:
        recognizer = ChordRecognizer(config)
        use_beat_sync = beat_sync and grid.beats.size >= 4
        if use_beat_sync:
            chroma_seq, boundaries, energy_seq = _beat_aligned(
                clip, chroma, energy, grid
            )
        else:
            chroma_seq = chroma
            boundaries = np.append(
                frames_to_time(np.arange(chroma.shape[1]), clip.sr),
                clip.duration,
            )
            energy_seq = energy

        # Mark which segments begin a measure, so the decoder can make chord
        # changes cheap at bar lines and expensive in between.
        downbeat_flags = _downbeat_flags(boundaries, grid, chroma_seq.shape[1])
        spans = recognizer.recognize(
            chroma_seq, boundaries, energy_seq, downbeat_flags
        )
        spans = merge_short_spans(spans)

    beat_options: list[BeatOptions] = []
    if suggestions and chroma_seq is not None and boundaries is not None:
        beat_options = _beat_options(chroma_seq, boundaries)

    report("Estimating key", 0.92)
    # Without a chromagram, infer the key from the chords themselves -- which
    # is often steadier anyway, since it reasons over committed harmony rather
    # than raw spectral energy.
    key = estimate_key(chroma) if chroma is not None else estimate_key_from_chords(spans)

    # Lock the whole song onto one steady grid in one signature; chord changes
    # decide where bar lines fall. Falls back to the tracker's own grid (phase
    # corrected by the same chord evidence) when the song isn't steady.
    report("Locking the beat grid", 0.95)
    changes = [s.start for s in spans[1:] if s.label != "N"]
    locked = lock_grid(clip, changes, signature=signature)
    if locked is not None:
        per_beat, _ = SIGNATURES[locked.signature]
        spans = _snap_spans(spans, locked)
        grid = BeatGrid(
            beats=locked.beats,
            downbeats=locked.downbeats,
            tempo=60.0 / (locked.tick_period * per_beat),
            source=f"{grid.source}+locked",
            meter=locked.beats_per_bar,
        )
    else:
        grid = align_downbeat_phase(grid, spans)

    analysis = Analysis(
        path=str(Path(path).resolve()),
        duration=clip.duration,
        tempo=round(grid.tempo, 2),
        meter=grid.meter,
        key=key.display_name,
        key_confidence=round(key.confidence, 3),
        beat_source=grid.source,
        chords=spans,
        beats=[float(b) for b in grid.beats],
        downbeats=[float(b) for b in grid.downbeats],
    )
    analysis.measures = build_measures(analysis, grid)
    analysis.beat_options = beat_options
    if locked is not None:
        analysis.time_signature = locked.signature
        analysis.ticks = [float(t) for t in locked.ticks]
        analysis.bar_phase = locked.phase

    report("Done", 1.0)
    return analysis


def _beat_options(
    chroma_seq: np.ndarray, boundaries: np.ndarray, top_n: int = 6
) -> list[BeatOptions]:
    """Rank alternative chords for every beat in one pass.

    The recognizer already scores the whole vocabulary at every beat and keeps
    only the winner. Retaining the runners-up costs one extra matrix multiply
    over the full vocabulary and turns "what else could go here?" into a lookup
    the exported page can answer with no Python running.
    """
    if chroma_seq.shape[1] == 0:
        return []

    # Full vocabulary: suggestions should offer colours the conservative
    # recognizer would never commit to on its own.
    recognizer = ChordRecognizer(ChordRecognizerConfig(quality_names=None))
    scores = recognizer.score(chroma_seq)[:-1]  # drop the no-chord row

    shifted = scores - scores.max(axis=0, keepdims=True)
    weights = np.exp(shifted)
    weights /= weights.sum(axis=0, keepdims=True)

    ranked = np.argsort(weights, axis=0)[::-1][:top_n]

    out: list[BeatOptions] = []
    for column in range(weights.shape[1]):
        options = [
            (recognizer.labels[int(row)], round(float(weights[row, column]), 4))
            for row in ranked[:, column]
        ]
        out.append(
            BeatOptions(time=float(boundaries[column]), options=options)
        )
    return out


def _beat_aligned(
    clip: AudioClip, chroma: np.ndarray, energy: np.ndarray, grid: BeatGrid
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Aggregate chroma and energy into one column per beat interval."""
    beat_frames = time_to_frames(grid.beats, clip.sr)
    beat_frames = np.clip(beat_frames, 0, chroma.shape[1] - 1)
    beat_frames = np.unique(beat_frames)

    synced = beat_synchronous(chroma, beat_frames)

    # librosa.util.sync prepends a segment for the audio *before* the first
    # beat -- but only when there is any, i.e. when beat one isn't at frame 0.
    # Assuming it's always there shifts every chord one beat early.
    leading = [0.0] if beat_frames[0] > 0 else []
    beat_times = frames_to_time(beat_frames, clip.sr).tolist()
    edges = [*leading, *beat_times, clip.duration]

    segment_starts = np.array([*leading, *beat_times], dtype=np.float64)
    if len(edges) != synced.shape[1] + 1:
        # Guard against librosa changing its padding convention: trust the
        # column count and interpolate rather than emitting skewed timings.
        edges = np.linspace(0.0, clip.duration, synced.shape[1] + 1).tolist()
        segment_starts = np.asarray(edges[:-1], dtype=np.float64)

    boundaries = np.asarray(edges, dtype=np.float64)

    # Peak energy per segment, so a segment is only "silent" if all of it is.
    frame_starts = np.clip(time_to_frames(segment_starts, clip.sr), 0, len(energy) - 1)
    frame_ends = np.r_[frame_starts[1:], len(energy)]
    energy_seq = np.array(
        [energy[a : max(a + 1, b)].max() for a, b in zip(frame_starts, frame_ends)]
    )

    return synced, boundaries, energy_seq


def _downbeat_flags(
    boundaries: np.ndarray, grid: BeatGrid, n_segments: int
) -> np.ndarray | None:
    """Per-segment mask: does this segment start a measure?

    Returns None when there are no downbeats, so the decoder falls back to a
    uniform transition cost rather than acting on an invented bar line.
    """
    if not grid.has_downbeats or n_segments == 0:
        return None

    starts = np.asarray(boundaries[:n_segments], dtype=float)
    downbeats = np.asarray(grid.downbeats, dtype=float)
    if starts.size == 0 or downbeats.size == 0:
        return None

    # Half a beat of slack: segment starts are frame-quantized while downbeats
    # are raw floats, the same mismatch that skewed the grid view.
    if grid.beats.size > 1:
        tolerance = float(np.median(np.diff(grid.beats))) * 0.5
    else:
        tolerance = 0.1

    distance = np.abs(starts[:, None] - downbeats[None, :]).min(axis=1)
    return distance <= tolerance


def _snap_spans(spans: list[ChordSpan], locked) -> list[ChordSpan]:
    """Move chord changes onto the locked grid, keeping spans contiguous."""
    if len(spans) < 2:
        return spans
    snapped = snap_changes(
        [s.start for s in spans[1:]], locked.beats, locked.downbeats, locked.tick_period,
        bar_reach=1.5 if locked.signature == "6/8" else 1.05,
    )
    out = [ChordSpan(spans[0].label, spans[0].start, spans[0].end, spans[0].confidence)]
    for span, start in zip(spans[1:], snapped):
        start = max(start, out[-1].start)
        if start >= span.end - 1e-6:
            # Snapped past its own end: too short to keep on this grid, so the
            # chord before it holds on instead.
            out[-1].end = max(out[-1].end, span.end)
            continue
        out[-1].end = start
        if out[-1].end - out[-1].start <= 1e-6:
            out.pop()
        if out and out[-1].label == span.label:
            out[-1].end = span.end
            continue
        out.append(ChordSpan(span.label, start, span.end, span.confidence))
    return out


def align_downbeat_phase(grid: BeatGrid, spans: list[ChordSpan]) -> BeatGrid:
    """Rotate bar lines so they agree with where chords actually change.

    Downbeat trackers get the *period* right far more reliably than the
    *phase* -- it's common to end up a beat or two off, which puts every chord
    change on the last beat of a bar instead of the first. Harmony is the best
    available evidence for the true phase, because chord changes land on
    downbeats much more often than anywhere else.

    Only overrides the tracker when the winning phase is clearly better, so a
    genuinely syncopated song isn't forced into a wrong grid.
    """
    if not grid.has_downbeats or not grid.meter or grid.meter < 2:
        return grid
    if grid.beats.size < grid.meter * 2 or len(spans) < 4:
        return grid

    beats = grid.beats
    meter = grid.meter
    tolerance = float(np.median(np.diff(beats))) * 0.5 if beats.size > 1 else 0.1

    # Index of the tracker's first downbeat within the beat array.
    first = int(np.argmin(np.abs(beats - grid.downbeats[0])))
    changes = np.array([s.start for s in spans[1:]])  # skip the opening chord
    if changes.size == 0:
        return grid

    scores: list[tuple[int, int]] = []
    for phase in range(meter):
        candidate = beats[(first + phase) % meter :: meter]
        if candidate.size == 0:
            scores.append((0, phase))
            continue
        distance = np.abs(changes[:, None] - candidate[None, :]).min(axis=1)
        scores.append((int(np.sum(distance <= tolerance)), phase))

    scores.sort(reverse=True)
    best_hits, best_phase = scores[0]
    current_hits = next(hits for hits, phase in scores if phase == 0)

    # Require a real margin: 25% more chord changes explained.
    if best_phase == 0 or best_hits < current_hits * 1.25:
        return grid

    aligned = beats[(first + best_phase) % meter :: meter]
    return BeatGrid(
        beats=beats,
        downbeats=aligned,
        tempo=grid.tempo,
        source=f"{grid.source}+phase{best_phase:+d}",
        meter=meter,
    )


def build_measures(analysis: Analysis, grid: BeatGrid) -> list[Measure]:
    """Group chords into measures using detected downbeats.

    Without downbeats there's no reliable bar line, so we return an empty list
    rather than inventing one -- a chart with wrong bar lines is worse than a
    flat chord list.
    """
    if not grid.has_downbeats or len(grid.downbeats) < 2:
        return []

    edges = [*grid.downbeats.tolist(), analysis.duration]
    measures: list[Measure] = []

    for index, (start, end) in enumerate(zip(edges[:-1], edges[1:])):
        labels: list[str] = []
        for span in analysis.chords:
            # Any chord overlapping this measure appears in it.
            if span.end > start + 1e-6 and span.start < end - 1e-6:
                if not labels or labels[-1] != span.label:
                    labels.append(span.label)
        measures.append(Measure(index=index + 1, start=start, end=end, chords=labels))

    return measures


def _wants_flats(key_name: str, semitones: int) -> bool:
    """Pick sharp or flat spelling for the transposed key."""
    from .theory import parse_chord, prefers_flats

    parsed = parse_chord(key_name)
    if parsed is None:
        return False
    root, quality = parsed
    return prefers_flats(root + semitones, minor=quality == "m")
