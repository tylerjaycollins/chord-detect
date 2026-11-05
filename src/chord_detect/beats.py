"""Beat, downbeat, and tempo tracking.

Primary path is Beat This! (Foscarin et al., ISMIR 2024), a transformer tracker
that reports beats *and* downbeats. Downbeats are what let us group chords into
measures, which is the difference between a wall of symbols and a readable
chart.

librosa's onset-autocorrelation tracker is kept as a fallback so the pipeline
degrades instead of failing when the model weights can't be fetched.
"""

from __future__ import annotations

import contextlib
import io
import logging
import warnings
from dataclasses import dataclass, field

import librosa
import numpy as np

from .features import AudioClip

log = logging.getLogger(__name__)


@dataclass
class BeatGrid:
    beats: np.ndarray  # beat times in seconds
    downbeats: np.ndarray  # subset of `beats` that begin a measure
    tempo: float  # median BPM
    source: str  # which tracker produced this
    meter: int | None = field(default=None)  # beats per measure, if inferable

    @property
    def has_downbeats(self) -> bool:
        return self.downbeats.size > 0


def track_beats(clip: AudioClip, *, device: str | None = None) -> BeatGrid:
    """Detect beats and downbeats, preferring the neural tracker."""
    try:
        return _track_beat_this(clip, device=device)
    except Exception as exc:  # noqa: BLE001 - any failure should fall back
        log.warning("Beat This! unavailable (%s); falling back to librosa", exc)
        return _track_librosa(clip)


def _resolve_device(requested: str | None) -> str:
    if requested:
        return requested
    import torch

    # Apple Silicon: MPS is roughly 3-4x CPU for this model.
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _track_beat_this(clip: AudioClip, *, device: str | None) -> BeatGrid:
    from beat_this.inference import Audio2Beats

    resolved = _resolve_device(requested=device)
    # torch.hub prints a raw percentage stream while fetching weights on first
    # run, which mangles the CLI progress bar. Errors still surface as
    # exceptions, so swallowing stdout here loses nothing.
    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
        warnings.simplefilter("ignore")
        # Hand it the samples we already decoded rather than the file path: its
        # own loader can't read .m4a, which silently dropped those songs to the
        # librosa fallback with no bar lines at all.
        tracker = Audio2Beats(checkpoint_path="final0", device=resolved, dbn=False)
        beats, downbeats = tracker(clip.samples, clip.sr)

    beats = np.asarray(beats, dtype=np.float64)
    downbeats = np.asarray(downbeats, dtype=np.float64)
    return BeatGrid(
        beats=beats,
        downbeats=downbeats,
        tempo=_median_tempo(beats),
        source=f"beat_this[{resolved}]",
        meter=_infer_meter(beats, downbeats),
    )


def _track_librosa(clip: AudioClip) -> BeatGrid:
    from .features import HOP_LENGTH

    tempo, beat_frames = librosa.beat.beat_track(
        y=clip.samples, sr=clip.sr, hop_length=HOP_LENGTH, units="frames"
    )
    beats = librosa.frames_to_time(beat_frames, sr=clip.sr, hop_length=HOP_LENGTH)
    # No downbeat model here. Assuming 4/4 from the first beat is wrong often
    # enough that we'd rather report nothing and let the caller decide.
    return BeatGrid(
        beats=np.asarray(beats, dtype=np.float64),
        downbeats=np.array([], dtype=np.float64),
        tempo=float(np.atleast_1d(tempo)[0]),
        source="librosa",
        meter=None,
    )


def _median_tempo(beats: np.ndarray) -> float:
    """Median-of-intervals BPM, which shrugs off isolated missed beats."""
    if beats.size < 2:
        return 0.0
    intervals = np.diff(beats)
    intervals = intervals[intervals > 1e-6]
    if intervals.size == 0:
        return 0.0
    return float(60.0 / np.median(intervals))


def _infer_meter(beats: np.ndarray, downbeats: np.ndarray) -> int | None:
    """Beats per measure, from the spacing of detected downbeats."""
    if beats.size == 0 or downbeats.size < 2:
        return None
    # Count how many beats fall inside each measure and take the mode.
    counts: list[int] = []
    for start, end in zip(downbeats[:-1], downbeats[1:]):
        counts.append(int(np.sum((beats >= start - 1e-6) & (beats < end - 1e-6))))
    counts = [c for c in counts if 1 <= c <= 12]
    if not counts:
        return None
    values, frequency = np.unique(counts, return_counts=True)
    return int(values[np.argmax(frequency)])
