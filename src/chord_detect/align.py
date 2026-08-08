"""Align lyrics to the recording, so chords can be placed over the right words.

This is the piece Ledgr can't do: it has no audio. Overtone knows when every
chord sounds, and once it also knows when every word is sung, chord-over-word
placement is a join rather than a manual task.

Forced alignment, not transcription. The words are known, so the model only has
to find *where* they were sung, never guess *what* -- far more reliable than
running ASR at a worship recording.

Three things were measured on a real recording before this was written, and each
one shapes the code (see NEXT_STEPS.md for the numbers):

  * A single pass over a whole song drifts badly. One region of mismatch drags
    everything after it, producing sung "lines" 32 seconds long. Aligning inside
    bounded windows fixed the same song completely: 0 of 17 lines implausible,
    median confidence 0.410 against 0.034 globally.
  * Instrumental passages have no words to align to, so without an escape the
    aligner parks real words on top of them. A `*` star token -- native to
    MMS_FA -- absorbs them, and correctly found a 14s intro and a 5s outro tail.
  * Lyric sheets are arrangements, not transcripts. "Bridge x2" is written once
    and sung twice; align against the written form and the aligner runs out of
    words halfway through.

Confidence is only meaningful inside a window. Globally it collapses to noise
(0.034 median) even where timings are right, so plausibility is judged on line
*duration* first and confidence second.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Sequence

# A sung line shorter than this is a collapse -- the aligner ran out of audio.
MIN_LINE = 0.6
# ...and one longer than this is drift. Slow worship lines run 3-7s; 12s is
# generous enough not to fire on a held final note.
MAX_LINE = 12.0

SECTION_RE = re.compile(r"^\\?\[(.+?)\\?\]$")
REPEAT_RE = re.compile(r"\s*[x×]\s*(\d+)\s*$", re.I)
STAR = "*"


@dataclass
class Word:
    text: str
    start: float
    end: float
    score: float


@dataclass
class Line:
    """One sung line, with its words placed in time."""

    label: str          # section label it belongs to
    section: int        # index into the expanded section list
    text: str           # the line as written
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def score(self) -> float:
        if not self.words:
            return 0.0
        return sum(w.score for w in self.words) / len(self.words)

    @property
    def flags(self) -> list[str]:
        """Why this line looks wrong. Empty means it looks fine.

        Duration leads because it needs no calibration -- a sung line lasting
        32 seconds is impossible regardless of model or mix, whereas a
        confidence threshold has to be tuned per material.
        """
        out = []
        if self.duration > MAX_LINE:
            out.append("long")
        elif self.duration < MIN_LINE:
            out.append("collapsed")
        if self.score < 0.15:
            out.append("unsure")
        return out


@dataclass
class Section:
    label: str
    lines: list[str]


@dataclass
class Alignment:
    """Word timings for a whole song, plus what to distrust."""

    lines: list[Line]
    stars: list[tuple[float, float]]     # audio the aligner found wordless
    lyrics_hash: str
    duration: float
    anchors: list[tuple[int, float]] = field(default_factory=list)

    @property
    def suspect(self) -> list[int]:
        """Indices of lines worth a human look, worst first."""
        flagged = [i for i, ln in enumerate(self.lines) if ln.flags]
        return sorted(flagged, key=lambda i: -self.lines[i].duration)

    def to_json(self) -> str:
        return json.dumps(
            {
                "lyrics_hash": self.lyrics_hash,
                "duration": self.duration,
                "anchors": self.anchors,
                "stars": self.stars,
                "lines": [
                    {
                        "label": ln.label,
                        "section": ln.section,
                        "text": ln.text,
                        "start": round(ln.start, 3),
                        "end": round(ln.end, 3),
                        "score": round(ln.score, 4),
                        "flags": ln.flags,
                        "words": [
                            {
                                "t": round(w.start, 3),
                                "e": round(w.end, 3),
                                "w": w.text,
                                "s": round(w.score, 4),
                            }
                            for w in ln.words
                        ],
                    }
                    for ln in self.lines
                ],
            },
            indent=None,
        )


# --------------------------------------------------------------------------
# Lyrics -> a sung word sequence
# --------------------------------------------------------------------------


def parse_lyrics(text: str) -> list[Section]:
    """Split a lyric sheet into sections.

    Handles markdown-escaped brackets (`\\[Verse 1\\]`), since that's what a
    sheet written in a markdown editor actually contains.
    """
    sections: list[Section] = []
    label, lines = "Intro", []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = s.replace("\\[", "[").replace("\\]", "]")
        match = SECTION_RE.match(s)
        if match:
            if lines:
                sections.append(Section(label, lines))
            label, lines = match.group(1).replace("\\-", "-").strip(), []
        else:
            lines.append(s)
    if lines:
        sections.append(Section(label, lines))
    return sections


def expand_repeats(sections: Sequence[Section]) -> list[Section]:
    """`Bridge x2` -> two consecutive Bridge sections.

    Alignment needs the sequence as *sung*. A repeat marker is shorthand for a
    reader; leaving it unexpanded makes the aligner run out of words mid-section
    and drag everything after it forward.
    """
    out: list[Section] = []
    for section in sections:
        match = REPEAT_RE.search(section.label)
        times = int(match.group(1)) if match else 1
        base = REPEAT_RE.sub("", section.label).strip() or section.label
        for i in range(times):
            label = base if times == 1 else f"{base} ({i + 1}/{times})"
            out.append(Section(label, list(section.lines)))
    return out


def normalize(word: str) -> str:
    """Reduce a word to what MMS_FA's dictionary accepts: a-z and apostrophe."""
    w = word.lower().replace("’", "'")
    w = w.replace("“", "").replace("”", "")
    return re.sub(r"[^a-z']", "", w).strip("'")


def lyrics_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------


def _bundle():
    import torchaudio

    return torchaudio.pipelines.MMS_FA


def _emission(model, audio, sample_rate: int, device: str):
    """Run the acoustic model over a stretch of audio.

    Deliberately re-run per window rather than computed once and sliced.
    Slicing a global emission was tried and is measurably worse: wav2vec2's
    self-attention spans its whole input, so five minutes of context yields
    different representations than seventy seconds of it. The gain from
    windowing comes from bounding the model's attention, not merely from
    bounding the alignment search -- slicing keeps the search narrow but throws
    the actual benefit away. Cost is unchanged overall, since the windows
    partition the song.
    """
    import torch

    waveform = torch.from_numpy(audio).unsqueeze(0).to(device)
    with torch.inference_mode():
        emission, _ = model(waveform)
    return emission.cpu(), waveform.shape[1] / emission.shape[1] / sample_rate


def _align_tokens(emission, words: Sequence[str]):
    """Forced-align a word sequence against an emission slice.

    Returns per-word token spans, or None when the segment can't hold the words
    (more tokens than frames), which forced_align rejects outright.
    """
    import torch
    import torchaudio

    dictionary = _bundle().get_dict(star=STAR)
    tokens = [[dictionary[c] for c in w] for w in words if w]
    flat = [t for tok in tokens for t in tok]
    if not flat or emission.shape[1] <= len(flat):
        return None

    targets = torch.tensor(flat, dtype=torch.int32).unsqueeze(0)
    try:
        aligned, scores = torchaudio.functional.forced_align(
            emission, targets, blank=0
        )
    except Exception:
        return None

    spans = torchaudio.functional.merge_tokens(aligned[0], scores[0].exp())
    out, at = [], 0
    for tok in tokens:
        out.append(spans[at : at + len(tok)])
        at += len(tok)
    return out


def _span_times(span, ratio: float, offset: float) -> tuple[float, float, float]:
    start = span[0].start * ratio + offset
    end = span[-1].end * ratio + offset
    total = sum(len(s) for s in span)
    score = sum(s.score * len(s) for s in span) / total if total else 0.0
    return start, end, score


def derive_anchors(
    alignment: Alignment, lines_per_section: Sequence[int]
) -> list[tuple[int, float]]:
    """Turn a rough pass into anchors for a sharper second pass.

    Only section starts sitting in *plausible* neighbourhoods are trusted. A
    first pass is reliable in some stretches and badly drifted in others, so
    taking every boundary would lock the second pass onto confident nonsense;
    taking only the clean ones leaves the drifted stretch spanned by one larger
    window, where it can still be rescued.
    """
    starts = set()
    at = 0
    for count in lines_per_section:
        starts.add(at)
        at += count

    anchors: list[tuple[int, float]] = []
    last = -1.0
    for index in sorted(starts):
        if index == 0 or index >= len(alignment.lines):
            continue
        window = alignment.lines[max(0, index - 1) : index + 2]
        if any(line.flags for line in window):
            continue
        start = alignment.lines[index].start
        if start <= last:          # non-monotonic means the pass lost its place
            continue
        anchors.append((index, start))
        last = start
    return anchors


def align(
    audio_path: Path | str,
    lyrics: str,
    *,
    anchors: Sequence[tuple[int, float]] | None = None,
    refine: bool = True,
    device: str = "cpu",
    progress: Callable[[str, float], None] | None = None,
) -> Alignment:
    """Align `lyrics` to the recording.

    `anchors` are `(line_index, time)` pairs -- "this line starts here" -- and
    are the reliable path: bounded windows are what turned a mangled global pass
    into 0 implausible lines on the one song measured so far. Without them the
    whole song is one window, which works on some material and drifts on other.
    """
    import librosa

    def report(stage: str, fraction: float) -> None:
        if progress:
            progress(stage, fraction)

    sections = expand_repeats(parse_lyrics(lyrics))

    # Flatten to lines, dropping any that normalize away to nothing (a stray
    # punctuation-only line would otherwise claim a time range).
    lines: list[Line] = []
    line_words: list[list[str]] = []
    for si, section in enumerate(sections):
        for text in section.lines:
            toks = [t for t in (normalize(w) for w in text.split()) if t]
            if not toks:
                continue
            lines.append(Line(label=section.label, section=si, text=text))
            line_words.append(toks)

    report("Loading audio", 0.05)
    bundle = _bundle()
    audio, sr = librosa.load(str(audio_path), sr=bundle.sample_rate, mono=True)
    duration = len(audio) / sr

    report("Loading the alignment model", 0.12)
    model = bundle.get_model().to(device)

    # Windows are [first line, last line) paired with an audio range. Anchors
    # cut the song into independent problems, so a mismatch in one can't drag
    # the rest -- the failure mode measured on the global pass.
    marks = sorted(set(anchors or []), key=lambda a: a[0])
    bounds: list[tuple[int, int, float, float]] = []
    prev_line, prev_time = 0, 0.0
    for line_index, at in marks:
        if 0 < line_index <= len(lines):
            bounds.append((prev_line, line_index, prev_time, at))
            prev_line, prev_time = line_index, at
    bounds.append((prev_line, len(lines), prev_time, duration))

    stars: list[tuple[float, float]] = []
    for n, (lo, hi, t0, t1) in enumerate(bounds):
        report("Aligning words", 0.3 + 0.6 * n / max(1, len(bounds)))
        if lo >= hi:
            continue

        clip = audio[int(t0 * sr) : int(t1 * sr)]
        if clip.size < sr:
            continue
        window, ratio = _emission(model, clip, sr, device)

        # A star at each end absorbs intros, turnarounds and outros. Without it
        # that audio has to be assigned to a real word, which is exactly how a
        # sung line ends up 32 seconds long.
        words: list[str] = [STAR]
        owners: list[int | None] = [None]
        for li in range(lo, hi):
            for token in line_words[li]:
                words.append(token)
                owners.append(li)
        words.append(STAR)
        owners.append(None)

        spans = _align_tokens(window, words)
        if spans is None:
            continue

        offset = t0
        for span, owner in zip(spans, owners):
            if not span:
                continue
            start, end, score = _span_times(span, ratio, offset)
            if owner is None:
                if end - start > 1.0:
                    stars.append((round(start, 3), round(end, 3)))
                continue
            lines[owner].words.append(
                Word(text="", start=start, end=end, score=score)
            )

    # Re-attach the written words to their timings. Kept separate so the
    # alignment loop deals only in normalized tokens.
    for line, toks in zip(lines, line_words):
        for word, token in zip(line.words, toks):
            word.text = token

    result = Alignment(
        lines=lines,
        stars=stars,
        lyrics_hash=lyrics_hash(lyrics),
        duration=duration,
        anchors=[(int(i), float(t)) for i, t in marks],
    )

    # Second pass: use the clean parts of the first to cut the song into smaller
    # windows, then align again. Window length is the dominant lever -- a 210s
    # window drifts where a 70s one doesn't -- and section boundaries are where
    # the cuts belong. User anchors always win over derived ones.
    if refine:
        per_section = [
            sum(1 for line in lines if line.section == si)
            for si in range(len(sections))
        ]
        derived = derive_anchors(result, per_section)
        combined = {index: at for index, at in derived}
        combined.update({index: at for index, at in marks})
        if len(combined) > len(marks):
            report("Refining", 0.9)
            sharper = align(
                audio_path,
                lyrics,
                anchors=sorted(combined.items()),
                refine=False,
                device=device,
            )
            # Only keep it if it actually helped. A refinement that locked onto
            # a bad first pass should not silently replace a better answer.
            if len(sharper.suspect) <= len(result.suspect):
                sharper.anchors = [(int(i), float(t)) for i, t in marks]
                result = sharper

    report("Done", 1.0)
    return result
