"""Chord recognition: template scoring plus Viterbi decoding.

Frame-independent template matching flickers badly -- real music has passing
tones and momentary voicings that outvote the governing harmony. So scoring is
only half the job; a hidden Markov model with a strong self-transition bias
turns noisy per-beat guesses into the sustained chords a chart actually shows.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .theory import NO_CHORD, build_templates, parse_chord, transpose_label


@dataclass
class ChordSpan:
    """One chord, held over a time range."""

    label: str
    start: float
    end: float
    confidence: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def transposed(self, semitones: int, *, prefer_flats: bool = False) -> ChordSpan:
        return ChordSpan(
            label=transpose_label(self.label, semitones, prefer_flats=prefer_flats),
            start=self.start,
            end=self.end,
            confidence=self.confidence,
        )


@dataclass
class ChordRecognizerConfig:
    # Probability of holding the current chord into the next beat. High by
    # design: at ~120 BPM most chords last two beats or more, so changing is
    # the exception. Lower it for dense jazz, raise it for slow hymns.
    self_transition: float = 0.92
    # Sharpens or softens the template scores before decoding. Higher values
    # trust the acoustic evidence more and the smoothing prior less.
    emission_sharpness: float = 28.0
    # Below this dB level relative to peak, emit no-chord instead of guessing.
    silence_db: float = -42.0
    # How strongly to penalize chroma energy that falls outside the template.
    # Without this, dense templates win by covering more pitch classes.
    off_template_penalty: float = 0.55
    # How much to trust the corpus chord-quality priors. 1.0 applies them as
    # true log-probabilities; 0 ignores them and lets the audio decide alone,
    # which in practice floods the output with sus and power chords.
    prior_weight: float = 1.0
    # Prior probability that a given moment has no analyzable chord.
    no_chord_probability: float = 0.02
    # Restrict the vocabulary to these quality names. None means everything;
    # theory.SIMPLE_QUALITY_NAMES is a good default for pop and hymn charts.
    quality_names: tuple[str, ...] | None = None
    # Extra log-odds of changing chord on a downbeat. A single global
    # self-transition can't express *where* a change is plausible, so it either
    # holds through real changes or invents ones mid-bar. Harmony overwhelmingly
    # moves on the downbeat, so making that specific moment cheaper lets the
    # decoder stay sticky everywhere else.
    downbeat_change_bonus: float = 2.0


class ChordRecognizer:
    def __init__(self, config: ChordRecognizerConfig | None = None) -> None:
        self.config = config or ChordRecognizerConfig()
        vocabulary = build_templates(self.config.quality_names)
        self.templates = vocabulary.templates
        self.support = vocabulary.support
        self.log_priors = vocabulary.log_priors
        # No-chord is a real state, appended after the pitched vocabulary.
        self.labels = [*vocabulary.labels, NO_CHORD]
        self._n_states = len(self.labels)

    # -- scoring ----------------------------------------------------------

    def score(self, chroma: np.ndarray, energy_db: np.ndarray | None = None) -> np.ndarray:
        """Log-emission scores, shape (n_states, n_frames).

        `chroma` is (12, n_frames) and L1-normalized.
        """
        cfg = self.config
        # Unit-normalize columns so the dot product is a cosine similarity and
        # loud passages don't score higher than quiet ones.
        norms = np.linalg.norm(chroma, axis=0, keepdims=True)
        unit = np.divide(chroma, norms, out=np.zeros_like(chroma), where=norms > 1e-9)

        match = self.templates @ unit  # (n_chords, n_frames)

        # Energy on pitch classes that aren't chord tones at all. This uses the
        # binary support, not the harmonic template -- the latter has some
        # energy in nearly every bin, which would make the penalty uniform and
        # therefore useless.
        off_template = (1.0 - self.support) @ unit
        match = match - cfg.off_template_penalty * off_template

        # No-chord scores at zero similarity, so it only wins where nothing
        # harmonic fits or the frame is silent.
        no_chord = np.zeros((1, chroma.shape[1]), dtype=np.float32)
        scores = np.vstack([match, no_chord])

        # Scale the acoustic evidence first, then add log-priors. Keeping the
        # two separate is what makes the priors interpretable: they stay genuine
        # log probabilities instead of offsets that silently rescale whenever
        # emission_sharpness changes.
        log_emission = cfg.emission_sharpness * scores
        log_emission[:-1] += cfg.prior_weight * self.log_priors[:, None]
        log_emission[-1] += cfg.prior_weight * float(
            np.log(cfg.no_chord_probability)
        )

        if energy_db is not None:
            silent = energy_db < cfg.silence_db
            if silent.any():
                log_emission[:-1, silent] = -1e6  # force no-chord

        return log_emission

    # -- decoding ---------------------------------------------------------

    def decode(
        self,
        log_emission: np.ndarray,
        downbeats: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Viterbi decode. Returns (state_indices, per_frame_confidence).

        The transition matrix is uniform off the diagonal, so this is
        implemented in O(n_states * n_frames) rather than materializing an
        n_states^2 matrix: the best predecessor is either the same state or the
        globally best state from the previous step.

        `downbeats` is a per-frame boolean marking frames that begin a measure.
        Those frames get `downbeat_change_bonus` added to the cost of switching,
        which is what lets the decoder be sticky mid-bar without holding through
        real changes at bar lines.
        """
        cfg = self.config
        n_states, n_frames = log_emission.shape

        stay = np.log(cfg.self_transition)
        switch = np.log((1.0 - cfg.self_transition) / (n_states - 1))

        if downbeats is None:
            bonus = np.zeros(n_frames)
        else:
            bonus = np.where(
                np.asarray(downbeats[:n_frames], dtype=bool),
                cfg.downbeat_change_bonus,
                0.0,
            )
            if bonus.size < n_frames:  # pad if the grid ran short
                bonus = np.pad(bonus, (0, n_frames - bonus.size))

        delta = log_emission[:, 0] - np.log(n_states)
        backpointers = np.zeros((n_states, n_frames), dtype=np.int32)

        for t in range(1, n_frames):
            best_prev = int(np.argmax(delta))
            from_other = delta[best_prev] + switch + bonus[t]
            from_self = delta + stay

            take_self = from_self >= from_other
            backpointers[:, t] = np.where(take_self, np.arange(n_states), best_prev)
            delta = np.where(take_self, from_self, from_other) + log_emission[:, t]

            # Rescale to keep the running scores out of underflow.
            delta -= delta.max()

        path = np.empty(n_frames, dtype=np.int32)
        path[-1] = int(np.argmax(delta))
        for t in range(n_frames - 1, 0, -1):
            path[t - 1] = backpointers[path[t], t]

        confidence = _softmax_confidence(log_emission, path)
        return path, confidence

    # -- top level --------------------------------------------------------

    def recognize(
        self,
        chroma: np.ndarray,
        boundaries: np.ndarray,
        energy_db: np.ndarray | None = None,
        downbeats: np.ndarray | None = None,
    ) -> list[ChordSpan]:
        """Full recognition over pre-segmented frames.

        `boundaries` holds n_frames + 1 times marking each frame's extent, so
        beat-synchronous and fixed-hop input are handled identically.
        """
        if chroma.shape[1] == 0:
            return []
        log_emission = self.score(chroma, energy_db)
        path, confidence = self.decode(log_emission, downbeats)
        return self._to_spans(path, confidence, boundaries)

    def _to_spans(
        self, path: np.ndarray, confidence: np.ndarray, boundaries: np.ndarray
    ) -> list[ChordSpan]:
        """Collapse a per-frame label sequence into contiguous spans."""
        spans: list[ChordSpan] = []
        n = len(path)
        start_idx = 0

        for i in range(1, n + 1):
            if i < n and path[i] == path[start_idx]:
                continue
            label = self.labels[path[start_idx]]
            spans.append(
                ChordSpan(
                    label=label,
                    start=float(boundaries[start_idx]),
                    end=float(boundaries[min(i, len(boundaries) - 1)]),
                    confidence=float(np.mean(confidence[start_idx:i])),
                )
            )
            start_idx = i

        return spans


def _softmax_confidence(log_emission: np.ndarray, path: np.ndarray) -> np.ndarray:
    """Posterior of the chosen state under the emission scores alone.

    Deliberately ignores the transition prior: this answers "how well does the
    audio support this label", which is the useful signal for flagging shaky
    regions in the UI. Smoothing already had its say during decoding.
    """
    shifted = log_emission - log_emission.max(axis=0, keepdims=True)
    probs = np.exp(shifted)
    probs /= probs.sum(axis=0, keepdims=True)
    return probs[path, np.arange(len(path))]


def merge_short_spans(
    spans: list[ChordSpan], min_duration: float = 0.28
) -> list[ChordSpan]:
    """Absorb blips into whichever neighbour is more confident.

    Viterbi still emits the occasional one-beat chord at a transition. Below
    roughly a third of a second nothing reads as a chord change to a player.
    """
    if not spans:
        return []

    result = [spans[0]]
    for span in spans[1:]:
        previous = result[-1]
        if span.duration >= min_duration:
            if span.label == previous.label:
                result[-1] = _extend(previous, span)
            else:
                result.append(span)
            continue

        # Too short to stand on its own -- fold it into the neighbour.
        result[-1] = _extend(previous, span)

    return [s for s in result if s.duration > 1e-6]


def _extend(span: ChordSpan, following: ChordSpan) -> ChordSpan:
    total = span.duration + following.duration
    weight = span.duration / total if total > 0 else 1.0
    return ChordSpan(
        label=span.label,
        start=span.start,
        end=following.end,
        confidence=span.confidence * weight + following.confidence * (1 - weight),
    )


def spans_to_pitch_classes(spans: list[ChordSpan]) -> list[tuple[float, list[int]]]:
    """Expand each span into its constituent pitch classes, for MIDI export."""
    from .theory import QUALITY_BY_NAME

    out: list[tuple[float, list[int]]] = []
    for span in spans:
        parsed = parse_chord(span.label)
        if parsed is None:
            continue
        root, quality = parsed
        intervals = QUALITY_BY_NAME[quality].intervals
        out.append((span.start, [(root + i) % 12 for i in intervals]))
    return out
