# Overtone

Detects chords, beats, downbeats, key, and tempo in an audio file. Runs entirely
locally on Apple Silicon — no cloud, no API keys, no per-song cost.

Overtone is the name of the app; the repo, the CLI, and the Python package are
still `chord-detect`.

## The app

```bash
desktop/install.sh          # build "Overtone.app" (its own window) into ~/Applications
uv run chord-detect-app     # or just run it from a terminal, in Safari, on port 8770
```

`Overtone.app` is a small Tauri app (`desktop/`): its own window and Dock icon,
no browser tab. At launch it picks a free port, starts this repo's server on it
from `.venv` (no `uv` needed at launch), and quitting the app stops the server.
It runs this repo rather than a frozen copy, so Python edits take effect on the
next launch or the page's Restart button; rebuild only when `desktop/` changes.
Downloads land in `~/Downloads` and show in Finder; links to Ledgr open in your
browser. Logs go to `~/Library/Logs/chord-detect.log`.

`scripts/make_app.py` builds the older Safari-tab launcher; it's superseded.

Drop in a song, watch the progress, and land on a chart you can play along with:
the current measure highlights, a playhead rides across it, and pausing anywhere
shows the chords that fit that beat. Results are cached by file content, so
reopening a song is instant even if you renamed or moved it. Everything stays on
your machine.

## The CLI

```bash
uv run chord-detect song.mp3
uv run chord-detect song.mp3 --timeline          # every chord with timestamps
uv run chord-detect song.mp3 --capo 2            # shapes to play with a capo
uv run chord-detect song.mp3 --transpose -3      # shift the whole chart
uv run chord-detect song.mp3 --json out.json     # full analysis for other tools
uv run chord-detect song.mp3 --html chart.html   # printable chart + beat grid
uv run chord-detect song.mp3 --chordpro s.cho    # ChordPro, for import into Ledgr
uv run chord-detect song.mp3 --suggest-at 1:24   # what chords fit at this point
uv run chord-detect song.mp3 --progressions      # progressions that fit the key
```

Lyrics and chord-over-word placement live in **Ledgr**, not here. This project
handles the part that needs audio; `--chordpro` hands off to Ledgr's editor.

Reads anything ffmpeg can decode: mp3, wav, m4a, flac, aac.

## What it reports

```
╭─ song.wav ─────────╮
│ Key      C         │
│ Tempo    100.0 BPM │
│ Meter    4/4       │
│ Length   0:39      │
│ Chords   8 spans   │
╰─ beats via beat_this ─╯

  | C   | C G | G   | G Am |
  | Am  | Am F| F   | F C  |
```

Four measures per line, lead-sheet style. Bar lines come from detected
downbeats; if the tracker can't find them, it prints a flat chord list instead
of inventing bar lines that would be wrong.

## Three recognizers

**`--model cnnlstm`** (default) — Chord-CNN-LSTM (Jiang et al., ISMIR 2019, MIT,
56 MB). Five CNN-LSTMs whose frame probabilities are averaged and HMM-decoded
over a 301-label vocabulary. The only model here that recognises **inversions**,
so real chart chords like `D/F#`, `G/D` and `G/B` come out as slash chords.

**`--model btc`** — BTC, a bi-directional transformer (Park et al., ISMIR 2019,
MIT, 12 MB). Works from a 144-bin CQT and learned harmonic expectations, so it
survives sparse arrangements where a 12-bin chroma collapses to bare
root-and-fifth. Fastest of the three. No inversions, and it tends to omit sus
chords.

Both are fetched automatically on first use into `~/.cache/chord-detect/`.

**`--model template`** — the chroma + template + Viterbi pipeline described
below. Kept because it's transparent and tunable (`--sensitivity`,
`--vocabulary`), and because its per-beat scores power the suggestion tools.
Those flags do nothing on the BTC path.

## How the template model works

```
audio ─┬─> beat + downbeat tracking (Beat This!, transformer)
       └─> HPSS ─> CQT chroma ─> noise-floor subtraction
                                      │
                    beat-synchronous aggregation (median per beat)
                                      │
              harmonic template scoring ─> Viterbi decoding ─> chord spans
                                      │
                              key estimate (Krumhansl-Schmuckler)
```

Three choices do most of the work:

**Beat-synchronous decisions.** Chords are decided once per beat rather than per
23 ms frame. Each decision sees a full beat of harmonic evidence, and changes
land on beats where players expect them.

**Harmonic-aware templates.** Instruments aren't sine waves. A played E also
radiates B (its 3rd harmonic), which is the major 7th of a C chord — so a plain
C triad genuinely has B energy in its chroma. Templates model that leakage, so a
real `Cmaj7` has to show *more* seventh than physics alone accounts for.

**Viterbi smoothing.** Frame-independent matching flickers, because passing
tones and momentary voicings outvote the governing harmony. A high
self-transition probability (`--sensitivity`, default 0.92) encodes that holding
a chord is the norm and changing is the exception.

Deliberately absent: log compression of the chroma. It's a common step and it
measurably *hurt* here — it amplifies the broadband noise floor from drums until
it looks like chord tones. Median noise-floor subtraction replaced it.

## Tuning

| Flag | When to reach for it |
|---|---|
| `--sensitivity 0.85` | Chords changing more often than detected (dense jazz) |
| `--sensitivity 0.96` | Spurious chord changes (slow hymns, sustained pads) |
| `--vocabulary full` | Material that really uses 6ths, dim, aug, sus2, power chords |
| `--no-beat-sync` | Free-time material — rubato solo piano, ambient |
| `--device cpu` | If MPS misbehaves |

`--vocabulary simple` (the default) covers maj/min/7/m7/sus4. Restricting the
vocabulary measurably helps: fewer confusable templates means fewer exotic false
positives. On a real worship recording, `full` produced 79% major chords at 0.34
confidence with 66% of the track flagged shaky; `simple` gave 89% major at 0.44
confidence with 32% shaky.

## Accuracy, honestly

On a synthetic file with known ground truth: 99.9% exact, boundaries within
20 ms, tempo/meter/key correct. That's a test of the plumbing.

On two real worship recordings, checked against their published chord charts:
**100%** and **98%** of playing time landed on the chart's chord roots, with
tempo correct on one (79 vs 78) and meter correct on both — including a compound
6-based meter. Detected vocabularies matched the charts closely, sus and m7
colors included.

Known gaps, measured rather than guessed:

- **No slash chords.** Real charts are full of `D/F#` and `G/D`; these are
  currently reported by root only. The most visible remaining gap.
- **Tempo can lock onto a subdivision** in compound meter (158 reported against
  a notated 88, though the 6-grouping was right). This doesn't hurt chord
  accuracy, since beat-synchronous aggregation needs a consistent grid rather
  than a correctly-labeled one.
- A little residual major/relative-minor confusion (occasional `B` where `Bm`
  belongs).

Note that a chart's key often differs from the recording's — one of the two test
recordings sounds a whole step above its chart. The tool reports what's actually
sounding, so use `--capo` / `--transpose` to line them up.

Template matching plus an HMM is a strong baseline that plateaus — it has no
notion of key context or voicing. `ChordRecognizer` is the only component that
touches chroma, so swapping in a learned model (BTC) later is a contained
change. See `NEXT_STEPS.md`.

Template matching plus an HMM is a strong baseline that plateaus — it has no
notion of key context or voicing. `ChordRecognizer` is the only component that
touches chroma, so swapping in a learned model (BTC) later is a contained
change.

## Layout

| File | Role |
|---|---|
| `theory.py` | Pitch classes, chord vocabulary, harmonic templates, transposition |
| `features.py` | Audio loading, HPSS, CQT chroma, noise floor, beat sync |
| `beats.py` | Beat/downbeat/tempo tracking, with a librosa fallback |
| `chords.py` | Template scoring and Viterbi decoding |
| `key.py` | Krumhansl-Schmuckler key estimation |
| `pipeline.py` | `analyze()` — the single entry point every front end uses |
| `cli.py` | Terminal interface |

## Setup

Python is pinned to 3.11 (the ML stack has no 3.14 wheels yet). `uv run`
handles the environment. Beat tracking weights (~20 MB) download once on first
use and cache under `~/.cache/torch/`.

## Licensing

`beat_this` and Demucs are MIT. Some other pretrained MIR checkpoints — notably
madmom-derived ones — are CC BY-NC-SA, i.e. non-commercial. Fine for personal
use; audit before distributing.
