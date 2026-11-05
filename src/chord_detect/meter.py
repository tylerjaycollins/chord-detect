"""Lock the whole song onto one steady grid in one time signature.

Why this exists: the neural beat tracker picks its pulse frame by frame, and on
compound meters it wanders. On "I'm Waiting for Heaven" (6/8, eighth = 0.406 s)
it flipped between 0.6 s and 0.8 s beats -- the cross-rhythms -- so bars came
out different lengths, rows had 2 cells next to rows with 4, and chord changes
landed mid-bar. 6/8 also had no representation at all: meter was just "beats
per bar" and always printed as N/4.

What we do instead, in three steps:

1. Find the *tick* -- the fastest steady pulse (usually the eighth note) --
   from the onset envelope's autocorrelation. This is the most reliable level:
   on every song checked so far it's the single strongest periodicity.
2. Decide how ticks group. Compound (6/8) puts strong energy at 3 ticks, simple
   meters at 2. Then pick the bar: 6 ticks for 6/8, 8 for 4/4, 6 for 3/4.
3. Track ticks with librosa's dynamic-programming tracker held to that tempo,
   so the grid stays steady but can still follow slight drift, then rotate bar
   lines to where chord changes actually fall.

If the tracked ticks aren't steady (rubato, live tempo swings) we return None
and the caller keeps the neural tracker's grid -- a locked grid on a song that
isn't locked would be worse than the old behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np
from scipy.signal import find_peaks

from .features import AudioClip

ONSET_HOP = 256

# (ticks per counted beat, ticks per bar) for each signature we can lock to.
# 6/8 counts every eighth: six boxes a bar, the way Tyler counts it (2026-11-02),
# not the two dotted-quarter pulses it's felt in.
SIGNATURES: dict[str, tuple[int, int]] = {
    "6/8": (1, 6),
    "4/4": (2, 8),
    "3/4": (2, 6),
}


@dataclass
class LockedGrid:
    signature: str
    ticks: np.ndarray  # every subdivision (usually eighth notes)
    beats: np.ndarray  # the beats a musician counts (all six eighths in 6/8)
    downbeats: np.ndarray
    tick_period: float
    phase: int  # index of the first downbeat within `ticks`

    @property
    def beats_per_bar(self) -> int:
        per_beat, per_bar = SIGNATURES[self.signature]
        return per_bar // per_beat


def lock_grid(
    clip: AudioClip,
    chord_starts: list[float],
    *,
    signature: str | None = None,
) -> LockedGrid | None:
    """Build a steady grid for the whole song, or None if the song isn't steady."""
    oenv = librosa.onset.onset_strength(
        y=clip.samples, sr=clip.sr, hop_length=ONSET_HOP
    )
    if oenv.size < 100:
        return None
    fps = clip.sr / ONSET_HOP
    ac = librosa.autocorrelate(oenv - oenv.mean())
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]

    period = _tick_period(ac, fps)
    if period is None:
        return None

    frames = librosa.beat.beat_track(
        onset_envelope=oenv,
        sr=clip.sr,
        hop_length=ONSET_HOP,
        bpm=60.0 / period,
        tightness=400,
        trim=False,
        units="frames",
    )[1]
    ticks = librosa.frames_to_time(frames, sr=clip.sr, hop_length=ONSET_HOP)
    if ticks.size < 16:
        return None

    intervals = np.diff(ticks)
    p5, p95 = np.percentile(intervals, [5, 95])
    if p5 <= 0 or p95 / p5 > 1.25:
        return None
    period = float(np.median(intervals))

    if signature is None:
        signature = guess_signature(ac, fps, period)
    return place_bars(ticks, signature, chord_starts)


def place_bars(
    ticks: np.ndarray, signature: str, chord_starts: list[float], phase: int | None = None
) -> LockedGrid:
    """Group ticks into bars of one signature, starting bars where chords change.

    `phase` forces which tick starts the first bar (the "start a measure here"
    correction); otherwise the phase that puts the most chord changes on bar
    lines wins.
    """
    per_beat, per_bar = SIGNATURES[signature]
    period = float(np.median(np.diff(ticks))) if ticks.size > 1 else 0.5
    if phase is None:
        phase = _best_phase(ticks, per_bar, per_beat, chord_starts, period)
    phase %= per_bar
    # Beats are counted from the bar line, so they share its phase.
    return LockedGrid(
        signature=signature,
        ticks=ticks,
        beats=ticks[phase % per_beat :: per_beat],
        downbeats=ticks[phase::per_bar],
        tick_period=period,
        phase=phase,
    )


def guess_signature(ac: np.ndarray, fps: float, period: float) -> str:
    """Compound vs simple from where the ticks group, then 3/4 vs 4/4."""
    r = lambda n: _ac_at(ac, fps, n * period)  # noqa: E731
    if r(3) > r(2):
        return "6/8"
    # Simple meter: a bar of 6 ticks (3/4) only when it clearly beats 8 (4/4).
    if r(6) > r(8) * 1.15 and r(12) > r(16):
        return "3/4"
    return "4/4"


def snap_changes(starts: list[float], beats: np.ndarray, downbeats: np.ndarray,
                 tick_period: float, bar_reach: float = 1.05) -> list[float]:
    """Move each chord change onto the grid.

    A change within one tick of a bar line goes on the bar line -- harmony
    changes on beat 1 far more than a tick either side of it, and that tick is
    the usual size of the recognizer's timing error. `bar_reach` widens that in
    6/8 to half a dotted quarter (1.5 eighths): there the recognizer often calls
    a change an eighth and a bit early, which left it on beat 6. Beat 5 stays out
    of reach, since quick "Bb then Eb" moves really do land there. Anything else
    goes to its nearest counted beat.
    """
    if beats.size == 0:
        return list(starts)
    out: list[float] = []
    for t in starts:
        if downbeats.size:
            d = downbeats[np.argmin(np.abs(downbeats - t))]
            if abs(d - t) <= tick_period * bar_reach:
                out.append(float(d))
                continue
        out.append(float(beats[np.argmin(np.abs(beats - t))]))
    return out


# -- internals -------------------------------------------------------------


def _tick_period(ac: np.ndarray, fps: float) -> float | None:
    """Strongest periodicity between 0.18 s and 0.55 s, sub-frame refined."""
    lo, hi = int(0.18 * fps), int(0.55 * fps)
    if hi + 1 >= ac.size:
        return None
    peaks, _ = find_peaks(ac[lo:hi])
    if peaks.size == 0:
        return None
    peaks = peaks + lo
    i = int(peaks[np.argmax(ac[peaks])])
    a0, a1, a2 = ac[i - 1], ac[i], ac[i + 1]
    denom = a0 - 2 * a1 + a2
    offset = 0.5 * (a0 - a2) / denom if denom != 0 else 0.0
    return float((i + offset) / fps)


def _ac_at(ac: np.ndarray, fps: float, lag_s: float) -> float:
    """Autocorrelation near a lag, allowing ~3% period error."""
    centre = int(round(lag_s * fps))
    span = max(1, int(centre * 0.03))
    seg = ac[max(centre - span, 0) : centre + span + 1]
    return float(seg.max()) if seg.size else 0.0


def _best_phase(
    ticks: np.ndarray,
    per_bar: int,
    per_beat: int,
    chord_starts: list[float],
    period: float,
) -> int:
    changes = np.asarray(chord_starts, dtype=float)
    if changes.size == 0:
        return 0
    best = (-1.0, 0)
    for phase in range(per_bar):
        bars = ticks[phase::per_bar]
        beats = ticks[phase % per_beat :: per_beat]
        if bars.size == 0:
            continue
        on_bar = np.abs(changes[:, None] - bars[None, :]).min(axis=1) <= 0.5 * period
        on_beat = np.abs(changes[:, None] - beats[None, :]).min(axis=1) <= 0.5 * period
        # Bar lines dominate; beats break ties between phases a beat apart.
        score = float(on_bar.sum()) + 0.1 * float(on_beat.sum())
        if score > best[0]:
            best = (score, phase)
    return best[1]
