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
    out.append(f"{{key: {analysis.key}}}")
    if capo:
        out.append(f"{{capo: {capo}}}")
    out.append(f"{{tempo: {analysis.tempo:.0f}}}")
    if analysis.meter:
        out.append(f"{{time: {analysis.meter}/4}}")

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


def write_chordpro(analysis: Analysis, path: Path, **kwargs) -> Path:
    path.write_text(to_chordpro(analysis, **kwargs))
    return path
