"""Command line interface."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from .chords import ChordRecognizerConfig
from . import ledgr
from .chordpro import to_chordpro, write_chordpro
from .features import chroma_features, load_audio
from .pipeline import Analysis, analyze
from .render import write_html
from .suggest import suggest_at as suggest_chords_at
from .suggest import suggest_progressions
from .theory import (
    DEFAULT_MAX_CAPO,
    SIMPLE_QUALITY_NAMES,
    choose_shapes,
    transpose_label,
)

app = typer.Typer(
    add_completion=False,
    help="Detect chords, beats, key, and tempo in an audio file.",
)
console = Console()


@app.command()
def main(
    audio: Path = typer.Argument(..., help="Audio file (mp3, wav, m4a, flac, ...)"),
    transpose: int = typer.Option(
        0, "--transpose", "-t", help="Shift the output by N semitones."
    ),
    capo: int = typer.Option(
        0, "--capo", help="Show shapes for a capo at fret N (transposes down N)."
    ),
    shapes: Optional[str] = typer.Option(
        None,
        "--shapes",
        help="Preferred shape family, e.g. 'G' or 'G,C,D'. The capo is worked "
        "out for you and overrides --capo.",
    ),
    max_capo: int = typer.Option(
        DEFAULT_MAX_CAPO,
        "--max-capo",
        help="Highest capo to accept before trying the next preferred shape.",
    ),
    json_out: Optional[Path] = typer.Option(
        None, "--json", help="Write the full analysis to a JSON file."
    ),
    html_out: Optional[Path] = typer.Option(
        None,
        "--html",
        help="Write a printable HTML chart with chart and beat-grid views.",
    ),
    chordpro_out: Optional[Path] = typer.Option(
        None,
        "--chordpro",
        help="Write ChordPro (bar notation) for import into Ledgr.",
    ),
    upload_ledgr: bool = typer.Option(
        False,
        "--upload-ledgr",
        help="Push the chart straight into Ledgr as a song. Needs an "
        "api-scoped LEDGR_API_TOKEN; LEDGR_URL overrides the target.",
    ),
    chart: bool = typer.Option(
        True, "--chart/--no-chart", help="Print a measure-grouped chart."
    ),
    timeline: bool = typer.Option(
        False, "--timeline", help="Print every chord with timestamps."
    ),
    no_beat_sync: bool = typer.Option(
        False, "--no-beat-sync", help="Skip beat alignment (use for free-time audio)."
    ),
    sensitivity: float = typer.Option(
        0.92,
        "--sensitivity",
        help="Chord-change resistance, 0.5-0.99. Lower detects more changes.",
    ),
    model: str = typer.Option(
        "cnnlstm",
        "--model",
        "-m",
        help="'cnnlstm' (large vocab, detects slash chords), 'btc' (transformer), "
        "or 'template'. --vocabulary/--sensitivity only affect 'template'.",
    ),
    vocabulary: str = typer.Option(
        "simple",
        "--vocabulary",
        "-v",
        help="'simple' (maj/min/7/m7/sus4) or 'full' (adds 6ths, dim, aug, sus2, "
        "power chords). 'full' is more expressive but yields more false exotics.",
    ),
    suggest_at: Optional[str] = typer.Option(
        None,
        "--suggest-at",
        help="Suggest chords that fit at a point in the song, e.g. 1:24 or 84.",
    ),
    progressions: bool = typer.Option(
        False,
        "--progressions",
        help="Suggest common chord progressions that fit the detected key.",
    ),
    device: Optional[str] = typer.Option(
        None, "--device", help="Torch device: mps, cuda, or cpu. Auto-detected."
    ),
) -> None:
    if not audio.exists():
        console.print(f"[red]No such file:[/red] {audio}")
        raise typer.Exit(1)

    if not 0.5 <= sensitivity <= 0.99:
        console.print("[red]--sensitivity must be between 0.5 and 0.99[/red]")
        raise typer.Exit(1)

    if vocabulary not in ("simple", "full"):
        console.print("[red]--vocabulary must be 'simple' or 'full'[/red]")
        raise typer.Exit(1)

    if model not in ("btc", "template", "cnnlstm"):
        console.print("[red]--model must be 'cnnlstm', 'btc' or 'template'[/red]")
        raise typer.Exit(1)

    config = ChordRecognizerConfig(
        self_transition=sensitivity,
        quality_names=SIMPLE_QUALITY_NAMES if vocabulary == "simple" else None,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=28),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Starting", total=1.0)

        def report(stage: str, fraction: float) -> None:
            progress.update(task, description=stage, completed=fraction)

        analysis = analyze(
            audio,
            config=config,
            device=device,
            beat_sync=not no_beat_sync,
            # Per-beat alternatives are only needed by the HTML player, and
            # they're cheap enough to fold into the same pass.
            suggestions=html_out is not None,
            model=model,
            progress=report,
        )

    original_key = analysis.key

    # A shape preference determines the capo, so it overrides an explicit one.
    chosen_shape: str | None = None
    if shapes:
        preferences = [s.strip() for s in shapes.split(",") if s.strip()]
        picked = choose_shapes(analysis.key, preferences, max_capo=max_capo)
        if picked is None:
            console.print(f"[red]Unrecognized shapes: {shapes}[/red]")
            raise typer.Exit(1)
        capo, chosen_shape = picked

    shift = transpose - capo
    if shift:
        analysis = analysis.transposed(shift)

    _print_summary(analysis, shift)

    if chart:
        _print_chart(analysis)
    if timeline:
        _print_timeline(analysis)

    if json_out:
        json_out.write_text(analysis.to_json())
        console.print(f"\n[green]Wrote[/green] {json_out}")

    if html_out:
        write_html(
            analysis,
            html_out,
            capo=capo,
            source_key=original_key if shift else None,
            audio_src=_audio_href(audio, html_out),
            shift=shift,
            shapes=chosen_shape,
        )
        console.print(f"[green]Wrote[/green] {html_out}")

    if chordpro_out:
        write_chordpro(
            analysis,
            chordpro_out,
            capo=capo,
            # Transposed into the shapes above, so the key to write down is the
            # one it still sounds in.
            sounding_key=original_key if shift else None,
            measures_per_section=16,
        )
        console.print(f"[green]Wrote[/green] {chordpro_out}")

    if upload_ledgr:
        _upload_to_ledgr(analysis, capo, original_key if shift else None)

    if suggest_at is not None:
        _print_suggestions(audio, analysis, suggest_at, shift)

    if progressions:
        _print_progressions(analysis)


def _upload_to_ledgr(
    analysis: Analysis, capo: int, sounding_key: str | None = None
) -> None:
    """Send the chart to Ledgr, reporting the item URL or why it didn't go."""
    source = to_chordpro(
        analysis, capo=capo, sounding_key=sounding_key, measures_per_section=16
    )
    try:
        result = ledgr.upload_song(source, title=Path(analysis.path).stem)
    except ledgr.LedgrError as err:
        console.print(f"[red]Ledgr upload failed[/red] — {err}")
        raise typer.Exit(1) from err
    console.print(f"[green]Uploaded to Ledgr[/green] {result.url}")


def _print_summary(analysis: Analysis, shift: int) -> None:
    minutes, seconds = divmod(int(analysis.duration), 60)
    meter = analysis.signature or "unknown"
    confidence_note = "" if analysis.key_confidence >= 0.15 else " [dim](low)[/dim]"

    lines = [
        f"[bold]Key[/bold]      {analysis.key}{confidence_note}",
        f"[bold]Tempo[/bold]    {analysis.tempo:.1f} BPM",
        f"[bold]Meter[/bold]    {meter}",
        f"[bold]Length[/bold]   {minutes}:{seconds:02d}",
        f"[bold]Chords[/bold]   {len(analysis.chords)} spans",
    ]
    if shift:
        lines.append(f"[bold]Shifted[/bold]  {shift:+d} semitones")

    console.print(
        Panel(
            "\n".join(lines),
            title=Path(analysis.path).name,
            subtitle=f"beats via {analysis.beat_source}",
            expand=False,
        )
    )


def _print_chart(analysis: Analysis) -> None:
    if not analysis.measures:
        console.print(
            "\n[yellow]No downbeats detected — showing a flat chord list.[/yellow]"
        )
        unique: list[str] = []
        for span in analysis.chords:
            if not unique or unique[-1] != span.label:
                unique.append(span.label)
        console.print("  " + "  ".join(f"[cyan]{c}[/cyan]" for c in unique))
        return

    console.print()
    # Four measures per line is the standard lead-sheet convention.
    row: list[str] = []
    for measure in analysis.measures:
        cell = " ".join(measure.chords) if measure.chords else "—"
        row.append(f"| {cell:<14}")
        if len(row) == 4:
            console.print("  " + "".join(row) + "|")
            row = []
    if row:
        console.print("  " + "".join(row) + "|")


def _print_timeline(analysis: Analysis) -> None:
    table = Table(title="Chord timeline", show_edge=False)
    table.add_column("Start", justify="right", style="dim")
    table.add_column("Dur", justify="right", style="dim")
    table.add_column("Chord", style="cyan bold")
    table.add_column("Confidence", justify="right")

    for span in analysis.chords:
        confidence = span.confidence
        color = "green" if confidence > 0.5 else "yellow" if confidence > 0.3 else "red"
        table.add_row(
            _timestamp(span.start),
            f"{span.duration:.2f}s",
            span.label,
            f"[{color}]{confidence:.2f}[/{color}]",
        )

    console.print()
    console.print(table)


def _audio_href(audio: Path, html_out: Path) -> str:
    """URL for the audio, relative to where the HTML is written.

    A relative reference keeps the HTML small (embedding a 6 MB mp3 as a data
    URI would balloon it) at the cost of breaking if either file moves -- which
    the page's file picker covers.
    """
    audio = audio.resolve()
    base = html_out.resolve().parent
    try:
        relative = os.path.relpath(audio, base)
    except ValueError:  # different volume on Windows
        return audio.as_uri()
    return quote(relative.replace(os.sep, "/"))


def _parse_time(text: str) -> float:
    text = text.strip()
    if ":" not in text:
        return float(text)
    minutes, seconds = text.rsplit(":", 1)
    return int(minutes) * 60 + float(seconds)


def _print_suggestions(
    audio: Path, analysis: Analysis, at: str, shift: int
) -> None:
    """Show chords that fit a chosen point in the song."""
    try:
        when = _parse_time(at)
    except ValueError:
        console.print(f"[red]Could not read a time from {at!r}[/red]")
        return

    if not 0 <= when <= analysis.duration:
        console.print(
            f"[red]{at} is outside the track (0:00–{_timestamp(analysis.duration)})[/red]"
        )
        return

    clip = load_audio(audio)
    chroma = chroma_features(clip)
    current = analysis.chord_at(when)

    # Suggestions are scored against the recording, which is in its original
    # key -- so ask in that key, then shift the answers to match the display.
    key_for_scoring = transpose_label(analysis.key, -shift) if shift else analysis.key
    current_for_scoring = (
        transpose_label(current.label, -shift) if current and shift else
        (current.label if current else None)
    )

    results = suggest_chords_at(
        clip,
        when,
        key_for_scoring,
        current=current_for_scoring,
        chroma=chroma,
    )
    if not results:
        console.print(f"\n[yellow]Nothing audible at {at}.[/yellow]")
        return

    table = Table(
        title=f"Chords that fit at {_timestamp(when)}", show_edge=False
    )
    table.add_column("Chord", style="cyan bold")
    table.add_column("Degree", style="dim")
    table.add_column("Fit", justify="right")
    table.add_column("", style="dim")

    for item in results:
        label = transpose_label(item.label, shift) if shift else item.label
        bar = "█" * max(1, int(round(item.fit * 18)))
        colour = "green" if item.fit > 0.3 else "yellow" if item.fit > 0.12 else "red"
        table.add_row(
            label,
            item.roman or "—",
            f"[{colour}]{item.fit:.0%}[/{colour}] {bar}",
            item.note,
        )

    console.print()
    console.print(table)
    if current:
        # `current` came from the already-transposed analysis, so it is in
        # display space -- shifting it again would double-transpose.
        console.print(f"  [dim]detected here: {current.label}[/dim]")


def _print_progressions(analysis: Analysis) -> None:
    """Show common progressions that fit the detected key."""
    used = [s.label for s in analysis.chords]
    results = suggest_progressions(analysis.key, used=used)
    if not results:
        console.print("\n[yellow]No progressions available for this key.[/yellow]")
        return

    table = Table(title=f"Progressions in {analysis.key}", show_edge=False)
    table.add_column("Progression", style="bold")
    table.add_column("Chords", style="cyan")
    table.add_column("Degrees", style="dim")
    table.add_column("In song", justify="right")

    for item in results:
        share = item.overlap
        colour = "green" if share >= 0.75 else "yellow" if share >= 0.5 else "dim"
        table.add_row(
            item.name,
            "  ".join(item.chords),
            " ".join(item.numerals),
            f"[{colour}]{share:.0%}[/{colour}]",
        )

    console.print()
    console.print(table)
    console.print(
        "  [dim]'In song' = how much of that progression this song already uses.[/dim]"
    )


def _timestamp(seconds: float) -> str:
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}:{remainder:05.2f}"


if __name__ == "__main__":
    app()
