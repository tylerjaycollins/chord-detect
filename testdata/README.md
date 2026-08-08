# Test audio

Drop audio files in this folder and run:

```bash
uv run python scripts/eval.py testdata/
```

Audio here is gitignored — recordings stay on your machine. Only `.lab`
annotation files get committed, so the eval set stays reproducible for anyone
who has the same audio.

## What's most useful to add

Roughly in priority order. Five or six files covering the first four rows beats
twenty similar ones.

| Priority | Material | What it tests |
|---|---|---|
| 1 | Solo piano or acoustic guitar + vocal | Baseline. Should be near-perfect. If this fails, something is structurally wrong. |
| 2 | Full band — drums, bass, electric guitar | The real stress test. Drums are broadband and attack the noise-floor subtraction directly. |
| 3 | Organ or piano hymn | Sustained dense harmony. Tests whether `--sensitivity 0.92` holds chords long enough. |
| 4 | Anything with 7ths, sus, or slash chords | Tests the chord vocabulary beyond triads — the hardest thing to get right. |
| 5 | A song in a flat key, or one you play with a capo | Tests `Bb` vs `A#` spelling and the `--capo` path. |
| 6 | Something that modulates | A known limitation — one global key is currently assumed. Confirms how badly it degrades. |

Full songs are fine, but a 30–60 second excerpt of a representative section
(one verse + chorus) iterates much faster and is usually enough to expose a
problem. Any format ffmpeg reads: mp3, wav, m4a, flac, aac.

## You don't have to annotate anything

The sanity report needs no ground truth. It reports the quality mix, chords
falling outside the detected key, confidence, and how much of the track is
shaky — enough to spot most failures. The single most diagnostic line is the
triad/4-note split: if a straightforward hymn comes back mostly 4-note chords,
the 7th-inflation bug is back.

The lowest-effort path is genuinely: drop files in, run the eval, and tell me
which chords are wrong. That catches gross errors immediately.

## If you do want scored numbers

**You don't have to write timestamps.** Write the chords in measures and let
`scripts/make_lab.py` place the bar lines using detected downbeats:

```bash
# 1. analyze once, saving the beat grid
uv run chord-detect song.mp3 --json /tmp/song.json

# 2. write a chord sheet (see format below)
# 3. turn it into a timed annotation
uv run python scripts/make_lab.py song.mp3 song.chords \
    --from-json /tmp/song.json -o testdata/song.lab

# 4. score it
uv run python scripts/eval.py testdata/
```

The chord sheet is plain text — one line per group of bars:

```
# Comments start with #.
@ 0:12            anchor: next bar starts near 0:12 (snaps to a downbeat)
D | G | A | D     four bars, one chord each
D | G | A D | D   bar 3 holds two chords, split evenly
x3                repeat the previous line 3 more times
@ 1:45            jump to another section
Bm | G | D | A
```

Verified: regenerating `synthetic_c_g_am_f.lab` this way reproduces the
hand-written file exactly.

One caveat — bar lines come from *detected* downbeats, so a generated `.lab`
scores chord identity but not timing (it inherits the tracker's timing). That's
the right trade, since quality is the open question and timing was already
verified to ~20 ms. Sanity-check the tempo and meter first.

### Watch out for key

The tool reports the key the recording actually *sounds* in. If you know a song
from a chart that's capo'd or transposed for a vocalist, your chords may be in a
different key than the recording. Tell me which key you know it in and I'll
reconcile it, or use `--transpose` to compare.

### Writing a .lab by hand

Still fine if you prefer. Same basename as the audio (`song.mp3` → `song.lab`),
one chord per line, `start_seconds end_seconds chord`. See
`synthetic_c_g_am_f.lab`. Spelling follows what the tool prints — `C`, `Am`,
`G7`, `Fmaj7`, `Dsus4`, `Bbm7b5`, `C5`, and `N` for no chord. Timings needn't be
perfect; frame-level scoring tolerates small boundary error.

That split is the useful part: if root accuracy is much higher than exact
accuracy, roots are landing and the *quality* is wrong, which points at
templates and priors rather than chroma or timing. The eval prints that
diagnosis for you.

Annotating one verse and chorus of two or three songs is plenty to tune
against — no need to label whole tracks.
