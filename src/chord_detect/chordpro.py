"""Export an analysis as ChordPro, for import into Ledgr.

Division of labour: this project does the part that needs audio — chords,
beats, downbeats, key, tempo, meter. Ledgr already has the chart editor, lyric
alignment, transposition, printing and sharing. So rather than rebuild an
editor here, emit what Ledgr's parser already reads.

The natural bridge is Ledgr's `BarsLine` (`{ kind: "bars", bars: string[][] }`,
rendered `| C / / / | F / / / |`). It maps one-to-one onto the beat grid: one bar
per measure, one token per beat, a chord name where a chord starts and the strum
slash `/` where the previous chord is still sounding.

Deliberately emits no lyrics — this module has no idea what the words are. The
workflow is: analyze here, import to Ledgr, type the words in, and drag chords
onto the right syllables there.

One convention worth stating, because getting it backwards produces a chart that
looks right and transposes wrong: to Ledgr, the chords written down are the
*shapes* you play and `{key}` is how the song *sounds*, with the shapes derived
as key minus capo (`keyOfCapo` in its transpose module, and its capo control
rewrites the chords by the capo delta to hold the sounding key). So a Bb song
played in G shapes is `{key: Bb}{capo: 3}` over G-family chords -- which is why
`sounding_key` exists here: the analysis has already been transposed into the
shapes by then, so its own key is the wrong one to write.
"""

from __future__ import annotations

from pathlib import Path

from .pipeline import Analysis
from .render import build_grid

HELD = "/"


def to_chordpro(
    analysis: Analysis,
    *,
    title: str | None = None,
    capo: int = 0,
    sounding_key: str | None = None,
    bars_per_line: int = 4,
    section_label: str = "Chart",
    measures_per_section: int | None = None,
) -> str:
    """Render the analysis as ChordPro source.

    `measures_per_section` splits the output into numbered blocks, which makes a
    long song easier to navigate in an editor. None keeps it as one section.
    """
    out: list[str] = []

    name = title or Path(analysis.path).stem
    out.append(f"{{title: {name}}}")
    out.append(f"{{key: {sounding_key or analysis.key}}}")
    if capo:
        out.append(f"{{capo: {capo}}}")
    out.append(f"{{tempo: {analysis.tempo:.0f}}}")
    if analysis.signature:
        out.append(f"{{time: {analysis.signature}}}")

    bars = _bars(analysis)
    if not bars:
        out.append("")
        out.append(f"{{section: {section_label}}}")
        out.append(f"{{c: no bar lines detected — chords in order}}")
        sequence: list[str] = []
        for span in analysis.chords:
            if span.label != "N" and (not sequence or sequence[-1] != span.label):
                sequence.append(span.label)
        for start in range(0, len(sequence), bars_per_line):
            chunk = sequence[start : start + bars_per_line]
            out.append("| " + " | ".join(chunk) + " |")
        return "\n".join(out) + "\n"

    # Group bars into sections, then into lines within each section.
    if measures_per_section and measures_per_section > 0:
        blocks = [
            bars[i : i + measures_per_section]
            for i in range(0, len(bars), measures_per_section)
        ]
    else:
        blocks = [bars]

    for index, block in enumerate(blocks, start=1):
        label = section_label if len(blocks) == 1 else f"{section_label} {index}"
        out.append("")
        out.append(f"{{section: {label}}}")
        for start in range(0, len(block), bars_per_line):
            line = [(list(tokens), sounding) for tokens, sounding in
                    block[start : start + bars_per_line]]
            # Name the chord at the head of every line, even when it's held over
            # from the previous line.
            if line:
                tokens, sounding = line[0]
                if tokens[0] == HELD and sounding:
                    tokens[0] = sounding
            out.append(
                "| " + " | ".join(" ".join(tokens) for tokens, _ in line) + " |"
            )

    return "\n".join(out) + "\n"


def _bars(analysis: Analysis) -> list[tuple[list[str], str]]:
    """One bar per measure. Returns (beat tokens, chord sounding at bar start).

    The sounding chord is carried separately because a held chord is written as
    a slash — but the *first* bar of a line still has to name it, or a reader
    picking up mid-page has no idea what to play.
    """
    rows = build_grid(analysis)
    bars: list[tuple[list[str], str]] = []
    for row in rows:
        tokens: list[str] = []
        sounding = ""
        for cell in row.cells:
            # A continuation on beat 1 becomes a held slash: the chord was
            # already named in an earlier bar, and repeating it would read as a
            # re-strike that isn't there.
            if cell.label and not cell.continuation:
                tokens.append(cell.label)
            else:
                tokens.append(HELD)
            if cell.beat == 1 and cell.label:
                sounding = cell.label
        bars.append((tokens or [HELD], sounding))
    return bars


def to_chordpro_with_lyrics(
    analysis: Analysis,
    alignment,
    *,
    title: str | None = None,
    capo: int = 0,
    sounding_key: str | None = None,
) -> str:
    """Render chords positioned over words, as `[G]word` inline brackets.

    The payoff of alignment: chord start times are known to the millisecond and
    word times come from `align.py`, so placement is a join rather than a manual
    task. The output is Ledgr's `LyricLine`/`ChordPair` shape, so it lands in its
    editor with the placement already done.

    Chords sounding between sung lines -- intros, turnarounds -- are emitted as
    a comment rather than silently dropped, since they're real and a player
    needs them, but they belong to no word.
    """
    out: list[str] = []
    name = title or Path(analysis.path).stem
    out.append(f"{{title: {name}}}")
    out.append(f"{{key: {sounding_key or analysis.key}}}")
    if capo:
        out.append(f"{{capo: {capo}}}")
    out.append(f"{{tempo: {analysis.tempo:.0f}}}")
    if analysis.signature:
        out.append(f"{{time: {analysis.signature}}}")

    spans = [s for s in analysis.chords if s.label != "N"]
    used = set()
    section = None

    for line in alignment.lines:
        if not line.words:
            continue
        if line.label != section:
            section = line.label
            out.append("")
            out.append(f"{{section: {section}}}")

        # Chords that start anywhere inside this line, placed on the word that
        # was sounding when the change happened.
        placed: dict[int, str] = {}
        for si, span in enumerate(spans):
            if span.start < line.start - 0.25 or span.start > line.end:
                continue
            used.add(si)
            index = 0
            for wi, word in enumerate(line.words):
                if word.start <= span.start <= word.end:
                    index = wi
                    break
                if word.start > span.start:
                    index = wi
                    break
                index = wi
            # First chord in, don't overwrite a change already on that word.
            placed.setdefault(index, span.label)

        written = line.text.split()
        parts: list[str] = []
        for wi, word in enumerate(written):
            if wi in placed:
                parts.append(f"[{placed[wi]}]{word}")
            else:
                parts.append(word)
        out.append(" ".join(parts))

    leftover = [s for i, s in enumerate(spans) if i not in used]
    if leftover:
        out.append("")
        out.append("{section: Instrumental}")
        out.append(
            "{c: chords with no words under them — "
            + f"{len(leftover)} changes, see the beat grid"
            + "}"
        )

    return "\n".join(out) + "\n"


def write_chordpro(analysis: Analysis, path: Path, **kwargs) -> Path:
    path.write_text(to_chordpro(analysis, **kwargs))
    return path
