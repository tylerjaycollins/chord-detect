# Next steps

Living backlog for `chord-detect`. Newest thinking at the top of each section.

## Where things stand (read this first)

**What it is.** Audio in, chord chart out. Runs entirely locally on Apple
Silicon. `README.md` covers usage; this file is the backlog and the record of
*why* things are the way they are.

**How to run it.** Double-click `~/Applications/Chord Detect.app`, or
`uv run chord-detect-app`, or `uv run chord-detect song.mp3` for the CLI.

**Where the parts live.** `pipeline.analyze()` is the single entry point every
front end uses. Three interchangeable recognizers behind it — `cnnlstm.py`
(default, large vocabulary, detects slash chords), `btc.py` (transformer), and
`chords.py` (chroma templates, still powering the suggestion tools). Models are
cloned on demand into `~/.cache/chord-detect/`.

**Division of labour with Ledgr.** This project does the part that needs audio;
Ledgr owns lyrics, chord-over-word placement, and printing. `--chordpro` is the
handoff. Don't build a chart editor here — see the section below.

**The one thing to keep in mind.** Accuracy is scored against published chord
charts, which compares *vocabulary and duration but not timing*. It cannot tell
whether chords land at the right moments, which is exactly what "missing chords"
means. Every number in this file carries that caveat. Timed ground truth (a stem
export, or 20 minutes annotating one verse with `scripts/make_lab.py`) is the
single highest-value thing still missing.

## Validation status (real recordings, checked against published chord charts)

Now scored on **exact chord symbols**, not just roots. The old root-coverage
metric was too weak — it can't tell `G` from `Gm` from `Gsus4`, and it rewards a
model that just holds the tonic.

| Song | Key | Tempo | Meter | BTC: exact chart chords |
|---|---|---|---|---|
| Jesus, Firm Foundation (Donehey/Chapman/Hall/Mandisa) | correct¹ | 79 vs chart 78 ✓ | 4/4 ✓ | **98%** |
| It Was Finished Upon That Cross (CityAlight) | Eb ✓ | 158 vs chart 88 ✗ | 6 ✓ | **95%** |
| Jesus, Strong and Kind (feat. Colin Buchanan) | C ✓ | 91 vs chart 90 ✓ | 4/4 ✓ | **88%** |

**The consistent miss is sus decorations.** BTC never emitted `Gsus4`, `Csus4`
or `G5` where the charts have them, and occasionally reports a 7th variant of a
chart chord (`Am7` for `Am`, `Fmaj7` for `F`). It is the exact mirror of the
template model's failure, which *over*-produced sus chords at 7-14% of playing
time. Under-reporting a sus is far less harmful — the plain triad still plays
fine — but it's the clearest remaining accuracy gap.

**Still unverified: timing.** Even "exact chart chords" only compares
vocabulary and how long each chord sounds. It cannot tell whether chords land at
the right moments, which is what "missing chords" complaints are actually about.
Timed ground truth remains the missing piece.

¹ The recording sounds a whole step above its chart: chart is written for Bb
(capo 3, G shapes), the recording is in C. Detection was correct; the *chart* is
in a different key. Confirmed by `--capo 5`, which reproduces the chart's
vocabulary exactly (G/C/D/Em plus Gsus4 and Em7).

Detected vocabularies matched the charts closely, including sus and m7 colors.
The only chart chord never found was `G5`, which `--vocabulary simple` excludes
by design.

**The minor-chord worry is largely resolved.** Minors show up where the charts
put them (Em+Em7 ~6% in song 1, Bm+Bm7 ~13% in song 2). Song 2 does still emit a
little `B`/`B7` where `Bm` is expected, so the relative-minor confusion is real
but small.

### Open issues from this round

- [ ] **Tempo is ~1.8x the notated tempo on the 6/8 song** (158 detected vs 88
      on the chart) while the *meter grouping* was right. The tracker is locking
      onto a subdivision rather than the notated pulse. Note this did **not**
      hurt chord accuracy (98%), because beat-synchronous aggregation only needs
      a consistent grid, not a correctly-labeled one — so this is a reporting
      and UI problem, not a recognition problem. Fix by detecting compound
      meter and reporting the dotted-quarter pulse.
- [ ] **Slash chords / bass-aware inversion detection.** Deferred by choice —
      worth doing eventually, not now. It's conspicuous on real charts (the
      CityAlight chart is full of `D/F#`, `G/D`, `Bm/D`, `A/D`, `G/B`) and would
      be the most visible accuracy gain, but the charts are readable without it.
      Approach: a separate low-register chroma to identify the bass note, paired
      with the already-detected chord. `theory.Vocabulary.support` is the hook.
- [ ] **Chord changes still get missed** — audible changes the detector holds
      through. Span count rose 98 → 107 on `He Who Knew No Sin` after the noise
      floor fix, so lowering the floor helped, but it isn't the whole story.
      Next suspects, in order: `self_transition` (0.92) is a blunt global
      stickiness with no notion of where a change is *likely* (downbeats);
      beat-synchronous aggregation can straddle a mid-beat change; and
      `merge_short_spans` (0.28s) may be absorbing real fast changes. A
      downbeat-aware transition prior — cheaper to change chords on a downbeat
      than mid-bar — is probably the right fix and would help boundaries too.
- [ ] `Gm` shows up at ~2% in a D-major song after the floor change. Not
      diatonic, and worth a look — possibly a passing Gm or a new artifact.
- [x] ~~Check whether Bm is under-detected in `He Who Knew No Sin.mp3`.~~
      Chord AI's grid for that track shows `Em` at capo 7 (= sounding Bm) at the
      head of every other measure, while our run gave only ~4% minor. Both tools
      agree exactly on key (D), tempo (75), and meter (4/4), so this is a
      quality-level disagreement worth chasing. Note Chord AI is not ground
      truth either — confirm by ear before "fixing" anything.
- [ ] Neither song has a timed `.lab` yet, so these are root-coverage and
      vocabulary comparisons rather than frame-level scores. Good enough to have
      caught the priors bug; write `.lab` files via `scripts/make_lab.py` for
      real numbers.
- [ ] Surface "recording key vs your chart key" as a feature — it's genuinely
      useful (it told us this recording needs capo 5, not the chart's capo 3),
      and it falls straight out of what's already computed.

## Earlier — the first real file

`He Who Knew No Sin.mp3` (5:14, D major, 75 BPM) is the first real recording
run through this, and it drove the prior recalibration below. It's still
**unscored** — nobody has told the tool what the chords actually are, so all we
have is plausibility.

- [ ] Get a recording whose chords are known for certain, plus those chords.
      `He Who Knew No Sin.mp3` is a *use-case* example (the chords aren't known
      — that's the point of the tool), so it can't serve as ground truth. It
      stays valuable as a sanity-report target.
- [ ] Use `scripts/make_lab.py` rather than hand-writing timestamps — write the
      chords in measures and it places bar lines from detected downbeats.
- [ ] Specifically check whether **minor chords are under-detected**. The run
      came back 89% major with only 1% minor + 3% m7, which is low for a
      D-major worship song that presumably leans on Bm. A major triad and its
      relative minor 7th share three of four notes (D-F#-A vs B-D-F#-A), so
      this is genuinely ambiguous from chroma and the corpus prior favors
      major. If Bm is being swallowed by D, that's the next real bug.
- [ ] Also check the `B` / `B7` spans (~5% combined). `B7` and `F#7` are
      plausible secondary dominants in D, but plain `B` major where `Bm` is
      expected smells like the same relative-minor confusion.
- [ ] ~32% of that track is still below the confidence floor. Find out whether
      that's concentrated in specific sections (intro, instrumental break,
      key change) or spread evenly — the former is fixable, the latter is a
      front-end limit.

## Also — validate on more real audio

**The single most important next step.** Everything currently in the repo was
tuned against one synthetic file (`scratchpad/make_test_audio.py` renders a
known C–G–Am–F progression). It scores ~99.9% on that file, which proves the
plumbing is correct but says little about real recordings. Synthetic audio has
no reverb, no vocals, no guitar distortion, no bass glissando, and a perfectly
steady tempo.

- [ ] Run it on 10–15 real songs you know the chords to. Hymns, worship sets,
      and pop all stress different things.
- [ ] Specifically check: does the `maj7`/triad confusion come back? That was
      the hardest failure to kill, and the fix (median noise-floor subtraction
      in `features._subtract_noise_floor`) was chosen by grid search on
      synthetic audio — the 50th-percentile threshold may not be optimal for
      dense mixes.
- [ ] Re-run the sweep (`scratchpad/sweep.py`) once there's real ground truth.
      Its grid over gamma / floor percentile / off-template penalty is already
      written; it just needs better data. Note the sweep still points at the
      synthetic wav — repoint it at `testdata/` when real files land.
- [ ] Build a proper eval set. The standard academic ones are Isophonics
      (Beatles), RWC-Pop, and USPOP; `mir_eval` computes the standard
      weighted chord-symbol recall so results are comparable to published
      numbers.

## Accuracy upgrades

- [ ] **Swap in a learned chord model.** Template matching plus an HMM is a
      solid baseline, but it plateaus — it has no notion of key context or
      voicing. BTC (Bi-directional Transformer for Chord recognition, ISMIR
      2019) reaches ~83% on large vocabularies. `pipeline.analyze` is
      deliberately the only entry point, and `ChordRecognizer` is the only
      thing that touches chroma, so this swap shouldn't touch the CLI at all.
- [ ] **Bass-aware inversion detection.** Chord AI shows slash chords (`C/E`).
      Needs a separate low-register chroma to identify the bass note, then
      pair it with the detected chord. The templates already carry a `support`
      matrix that would make this straightforward.
- [ ] **Key-aware chord priors.** Once the key is known, chords in that key
      should be cheaper in the Viterbi transition matrix. Currently the
      transition matrix is uniform off the diagonal, which ignores the fact
      that G→C is far likelier than G→C# in C major. Probably the highest
      accuracy-per-line-of-code win available.
- [ ] Two-pass analysis: estimate key, then re-decode chords with key priors.
- [ ] Handle modulation — a single global key is wrong for a lot of material.
      Segment the track and estimate key per section.
- [ ] `--sensitivity` is currently a raw self-transition probability. Worth
      remapping to something musical ("dense jazz" ↔ "slow hymn").

## Features to match Chord AI

- [ ] **Stem separation** via Demucs (`htdemucs_ft`, MIT). Both a feature in
      its own right and an accuracy play: running chord detection on an
      isolated harmonic stem, with drums and vocals removed, should help a
      lot. Runs on MPS.
- [ ] **Guitar/piano chord diagrams.** Voicing generation from a
      `(root, quality)` pair is pure logic — `theory.QUALITIES` already has
      the interval sets needed.
- [ ] **MIDI export.** `chords.spans_to_pitch_classes` is already written and
      unused; it just needs `pretty_midi` wired up.
- [ ] **ChordPro / lead-sheet text export** for printing.
- [ ] Section detection (verse/chorus). Taejun Kim's `all-in-one` analyzer
      does structure + beats + downbeats in one model, and could replace
      `beats.py` outright.
- [ ] Loop-a-section playback, and time-stretch practice mode via
      `pyrubberband`.
- [ ] Nashville-number / Roman-numeral display, once key detection is trusted.

## Lyrics — automatic chord-over-word placement

**Decided (2026-08-08).** Chords get matched to words *here*, automatically,
and the ChordPro that goes to Ledgr arrives with placement already done. Ledgr
stays the editor; what it stops being is the place where every chord is dragged
onto a syllable from scratch.

The reason this belongs here rather than in Ledgr: **we know when each chord
sounds, to the millisecond, and Ledgr never will.** If we also know when each
word is sung, placement is a join, not a manual task.

The pipeline:

1. Lyrics pasted in as plain text, one line per sung line. The only manual input.
2. **Demucs** isolates the vocal stem. Already wanted for accuracy reasons (see
   "Features to match Chord AI"); needed here because alignment against a full
   band mix is measurably worse.
3. **Forced alignment**, not transcription — `torchaudio.functional.forced_align`
   with the known lyrics as the target, giving a start time per word. Because it
   never has to guess *what* was sung, only *where*, it beats running Whisper at
   the problem. Already installed: torchaudio 2.11 ships `forced_align` and the
   `MMS_FA` pipeline, and torch is already a dependency. **No new dependency for
   the alignment itself** — only Demucs is new.
4. Join word times against chord span starts. Each chord attaches to the word
   sounding when it begins.
5. Emit ChordPro with inline brackets (`[G]word`) — Ledgr's `LyricLine` /
   `ChordPair` shape exactly.

- [ ] **Expect good-not-perfect, and design for it.** Forced aligners are
      trained on speech. Sung, reverb-heavy, harmonized worship vocals are the
      hard case — held syllables and melisma especially. Demucs closes much of
      the gap, not all of it. The correction pass below is therefore load-bearing,
      not polish.
- [ ] **Correction pass with the audio playing** — click a chord, arrow keys
      nudge it one word left or right. This is the one piece of chart editing
      that earns its place here: *hearing* the chord land is how you know it's on
      the right syllable, and Ledgr can't play the song. It is deliberately not
      an editor — no chord picker, no transposition, no section editing. Those
      exist in Ledgr and stay there.
- [ ] A/B `MMS_FA` against the English-specific wav2vec2 pipeline on real
      recordings; keep whichever wins. MMS_FA is multilingual and may lose to a
      dedicated English model here.
- [ ] Fallback for when alignment fails outright: tap a key on each line as the
      song plays to stamp line-level timings (~2 min/song). Cruder, but it can't
      fail, and it still beats placing every chord by hand.
- [ ] **Check the MMS_FA license** before this goes anywhere beyond personal
      use — it comes out of Meta's MMS project. Demucs is MIT and fine. Folds
      into the licensing audit under Maintenance.
- [ ] Show the words in the beat-grid view too, with the playhead moving under
      them, so one screen has lyrics + chords + bar positions while playing.
- [ ] Keep the chart view print-friendly once lyrics land in it.

## Songwriting / arranging tools

The HTML output is read-only today. The point of these is to try ideas quickly
when working a song up with others.

- [ ] **Suggest a chord at a point** — done as `--suggest-at`, but CLI-only.
      Wire it into the UI: click a beat cell, see ranked alternatives. The data
      is already in the page (`window.CHORD_TIMELINE`), and `suggest.suggest_at`
      is the backend call.
- [ ] **Progression suggestions** — done as `--progressions`. Two known
      weaknesses: the ranking is pure chord-set overlap, so on a I–V–vi–IV song
      nearly every common progression scores 100% and the ordering stops being
      useful. Better signals would be *order* (does the song actually move
      V→vi?) and section-local chord sets rather than whole-song.
- [ ] Let the progression tool write a candidate progression *into* the grid as
      a scratch layer, so an idea can be seen against the real measures.
- [ ] Bass-note / voice-leading awareness in suggestions — currently it only
      considers vertical fit at a point, not what would lead smoothly from the
      previous chord.
- [ ] `--suggest-at` re-loads and re-analyzes the audio to get chroma. Cache the
      chromagram alongside the analysis JSON so repeated queries are instant;
      this matters a lot for an interactive UI.

## Ledgr is the editor — don't build another one

**Decision: this project does not get a chart editor.** Ledgr
(`~/Development/Ledgr`) already has one, and it does the thing that matters —
lyrics typed in with chords positioned over the right words. Rebuilding that
here would be weeks of work to arrive somewhere worse.

Division of labour:

- **chord-detect** — the part that needs audio: chords, beats, downbeats, key,
  tempo, meter. Exports ChordPro.
- **Ledgr** — lyrics, chord placement over syllables, transposition, sections,
  printing, sharing.

The bridge is `--chordpro`, which emits Ledgr's `BarsLine` format
(`| C / / / | F / C / |`): one bar per measure, one token per beat, chord name
where a chord starts, `/` where it's held. Verified against Ledgr's real parser
via `tsx` — meta, sections and all 91 bars parse correctly and round-trip
through `serializeChordChart` unchanged.

Workflow: analyze audio here → paste lyrics → automatic chord-over-word
placement (see the lyrics section above) → correct while the song plays →
upload to Ledgr → arrange, transpose, print there.

**No new Ledgr API is needed — one already exists.** `POST /api/machine/items`
takes `{ type: "song", title, body: { format: "chordpro", text } }` with an
`Authorization: Bearer` token, and routes through the same `createItem` the
in-app editor uses, so the surface can't drift from Ledgr's contract.
`scripts/seed-demo-content.mts` creates real song items with exactly this shape.

- [ ] **Upload button / `--upload-ledgr` flag.** Two setup notes: the token must
      be `api`-scoped (`verifyApiToken` requires it — an `mcp`-scoped token is
      rejected), and the target is `https://ledgr-teal.vercel.app`, overridable
      via `LEDGR_URL` for testing against `localhost:3001`.
- [ ] Decide what re-uploading the same song does. Creating a second item every
      time is the wrong default once corrections start round-tripping; matching
      on title and updating is probably right, but it needs a deliberate choice
      rather than falling out of the implementation.
- [ ] Section detection would make the handoff much better — the export
      currently emits `Chart 1`, `Chart 2` … in 16-measure blocks, which is
      arbitrary. Real verse/chorus labels would drop straight into Ledgr's
      section model (`classifySection` already maps labels to kinds).
- [ ] Ledgr's `ChordPicker` and `TransposeControl` already exist; the
      `--suggest-at` results would slot in as a data source for the picker.

## Desktop app (deprioritized)

Was going to be a Tauri shell with an editable grid. Ledgr makes most of that
redundant. What's left that Ledgr *doesn't* do is audio-anchored playback —
hearing the song while watching the beat cursor move. Worth revisiting only if
the CLI → Ledgr handoff proves annoying in practice.

- [ ] Pick the shell. Leaning Tauri (small bundle) over Electron, with the
      Python core behind a local HTTP server; the alternative is a native
      SwiftUI front end talking to a Python sidecar.
- [ ] Waveform view with a playback cursor synced to the chord chart.
      `Analysis.chord_at(t)` already exists for exactly this.
- [ ] Drag-and-drop a file, then cache the analysis so reopening is instant.
      Analysis is slow enough (~25s for 40s of audio, dominated by HPSS and
      CQT) that caching matters.
- [ ] Show per-chord confidence in the UI — low-confidence spans are
      genuinely useful to flag rather than hide, and the value is already
      computed and plumbed through to JSON.
- [ ] Packaging/signing so it launches without Gatekeeper complaints.

## Performance

- [ ] Profile the pipeline. HPSS and the CQT dominate; `nn_filter` is
      O(n²) in frames and will get slow on long tracks.
- [ ] Cache analyses keyed by file content hash.
- [ ] Consider downsampling to 11 kHz — chord content lives below 2 kHz, so
      the top octave of the CQT is mostly wasted work.

## Maintenance / housekeeping

- [ ] No unit test suite yet (`scripts/eval.py` covers accuracy, not
      correctness). The `theory.py` round-trips (parse ↔ label ↔ transpose) are
      pure functions and trivially testable; start there.
- [ ] Pin `beat_this` — it's installed from a GitHub archive URL, not PyPI, so
      the build isn't reproducible.
- [ ] **Licensing, before distributing anything.** `beat_this` and Demucs are
      MIT and fine. But some pretrained MIR checkpoints (notably anything
      madmom-derived) are CC BY-NC-SA — non-commercial only. Fine for personal
      use; needs an audit before this ever ships to anyone else.
- [ ] Python is pinned to 3.11 (`.python-version`) because the ML stack lacks
      3.14 wheels. Revisit periodically.

## Shape families / capo — known gaps

- [ ] **A single preference that doesn't fit yields an unplayable capo.** Asking
      for D shapes on a song in C gives capo 10, because `choose_shapes` falls
      back to the lowest-capo preference rather than refusing. That's better than
      silently ignoring the request, but capo 10 is not a real answer. The UI
      should either warn, or suggest the shape family that *would* fit.
- [ ] The multi-preference option ("G, then C, then D") avoids this and is
      probably what most people want as the default. Consider making it so.
- [ ] `--max-capo` exists on the CLI but the app always uses the default of 7.
- [ ] Shape families cover major keys and their relative minors only. A song in
      a modal or ambiguous key may land somewhere odd.

## App — known gaps

The local app (`chord-detect-app`, `server.py`) now covers the full loop: pick a
song → analyze with live progress → play along with the chart.

- [ ] Analysis runs on a plain `threading.Thread`, so two uploads at once will
      contend for CPU and the progress of the second will look stalled. Fine for
      one person; needs a queue if it's ever more.
- [ ] Jobs live in an in-memory dict. Restarting the server loses in-flight work
      (finished charts survive on disk and `/jobs` falls back to checking for
      them, so only mid-analysis uploads are affected).
- [ ] The cache under `~/.cache/chord-detect/songs` grows without bound — each
      song keeps its audio (several MB). "Remove" in the UI deletes one; there's
      no size cap or age-based cleanup.
- [ ] No auth and no CSRF protection. It binds to 127.0.0.1 only, which is the
      right call for a local tool, but don't expose the port.
- [ ] Re-analyzing with different settings (capo, vocabulary) stores a whole
      separate copy of the audio, since the cache key includes the options.
      Should key audio by content hash alone and charts by content+options.
- [ ] No way to re-run a cached song with different settings from the library —
      you have to re-upload it.

## Model upgrade candidates

- [x] ~~**Chord-CNN-LSTM (music-x-lab, ISMIR 2019)**~~ — **adopted, now the
      default.** No Google Drive needed after all: the non-reweighted
      checkpoints are committed in the repo, and the README says those give the
      best overall accuracy (the Drive folder only holds reweighted variants).
- [ ] **ChordMini** (https://github.com/ptnghia-j/ChordMiniApp) — the app we got
      the Chord-CNN-LSTM lead from. Still worth mining for its BTC-SL/BTC-PL
      variants and its LRC lyrics handling, which bears on the
      lyrics-in-measures item. Pins Python 3.10 and uses madmom — the library
      deliberately avoided here, so don't adopt it wholesale.
- [ ] **autochord** (https://github.com/cjbayron/autochord) — pip-installable,
      LSTM over chroma, but appears to be major/minor only, which would be a
      *downgrade* from both models now installed. Low priority; useful only as a
      cheap third opinion for cross-checking.
- [ ] Try the reweighted Chord-CNN-LSTM checkpoints from the Drive folder as an
      A/B against the committed ones. The README claims worse overall accuracy
      but better rare-chord recall, which might suit charts better.

## BTC — known gaps

- [ ] **Confidence is a flat 0.75.** BTC emits a label per frame with no
      calibrated probability behind it, so the confidence shading in the UI is
      meaningless on this path. Either extract real softmax probabilities from
      `output_layer` or hide the confidence column when BTC is active — the
      current state looks informative and isn't.
- [ ] **Per-beat suggestions still come from chroma templates**, so the "what
      fits here?" panel disagrees with the chords on screen. BTC's logits would
      give a genuine ranking.
- [ ] BTC ignores the detected beat grid entirely — no beat-synchronous
      aggregation, no downbeat-aware transitions. Chord boundaries therefore
      don't snap to beats as tightly as the template path. Snapping BTC spans to
      the beat grid is probably a quick win.
- [ ] `--sensitivity`, `--vocabulary`, and `--no-beat-sync` silently do nothing
      on the BTC path. The CLI help says so; the app's Options panel doesn't.
- [ ] The upstream repo is patched in place after cloning (numpy aliases, PyYAML
      loader). If the clone is ever refreshed, `_patch_for_modern_deps()` must
      run again — `ensure_model()` does this, but a manual `git pull` wouldn't.

## Done (recent)

- **Backed up to a private GitHub repo** —
  `github.com/tylerjaycollins/chord-detect`. The existing `.gitignore` already
  kept audio and generated charts out, so nothing commercial went up with it.
- **The beat grid ("Beats & measures") is now the default view.** It's the one
  you actually read while the song plays; the lead-sheet chart is the thing you
  switch to for printing. Falls back to opening on the chart when there are no
  downbeats, since landing on an empty grid is worse than landing on the view
  that still works. `RENDER_VERSION` bumped to 5 so cached charts re-render.

- **Double-clickable macOS app** (`scripts/make_app.py` → `~/Applications/Chord
  Detect.app`). A thin launcher rather than a frozen bundle, so it runs this
  repo and picks up edits without rebuilding. It reuses an already-running
  server instead of colliding on the port, sets `PATH` explicitly (a GUI launch
  inherits none of the shell's), and shows a dialog rather than dying silently
  if `uv` or the repo is missing. `LSUIElement` keeps it out of the Dock, since
  a bouncing icon that never opens a window is just confusing.
  Icon is drawn with numpy and a hand-rolled PNG encoder — Pillow/matplotlib is
  a lot of dependency for a few rectangles.

- **Chord-CNN-LSTM added and made the default** (`--model cnnlstm`; `btc` and
  `template` still available). An ensemble of five CNN-LSTMs, HMM-decoded over a
  301-label vocabulary. MIT, 56 MB, everything committed upstream.
  **It detects inversions** — the slash-chord gap that has been open since the
  CityAlight chart. On that song it found `D/F#`, `G/D`, `G/B`, `D/A`, four of
  the six inversions the published chart uses. It also recovers sus chords BTC
  systematically omitted (`Gsus4`, `Dsus4`).
  Exact-chart-chord scores vs BTC: 97% / 96% / 91% against 98% / 95% / 88% —
  comparable or better on two of three, plus the inversions.
- Slash-chord support through the theory layer: `split_slash`, `parse_chord`
  tolerating a bass note, and `transpose_label` moving root *and* bass together
  (so a capo'd chart doesn't silently report the wrong bass).

- *(superseded by Chord-CNN-LSTM above, but the reasoning still stands)*
  **Added BTC as the default chord recognizer** (`--model btc`, learned
  transformer; `--model template` keeps the old chroma path). This was the fix
  for held chords and major-where-minor-belongs: those weren't tuning problems,
  they were the 12-bin chroma bottleneck. A sweep of six front-end
  configurations (HPSS on/off x noise floor p25/p10/p0) produced *identical*
  chords, proving the knobs were exhausted while a track's chroma showed bare
  root-and-fifth with C# at exactly 0.000.
  Results: on `I'm waiting for Heaven`, templates held one chord for 8.3s where
  BTC finds real movement matching Chord AI's vocabulary (G, C, D, Em). Across
  the three charted songs both models agree on every key, BTC finds ~10-20% more
  chord changes, and **sus/power-chord inflation drops from 7-14% to 0%** — the
  artifact fought repeatedly on the template path is simply absent.
  It's also ~3x faster, because it needs no HPSS or nn_filter.
  Caveat: chart-root coverage dips slightly on two songs (100→98%, 100→93%). That
  metric rewards under-detection, so a model finding more real changes scores
  lower on it; not treated as a regression, but it's unverified either way until
  there's timed ground truth.
- Only 12 MB of weights, MIT licensed, committed in the upstream repo — not the
  ~400 MB behind a Drive link that was estimated.

- **Downbeat-aware transition prior.** The decoder had one global stickiness
  with no notion of *where* a chord change is plausible, so it either held
  through real changes or invented them mid-bar. Chord changes on a downbeat now
  get a log-odds bonus (`downbeat_change_bonus`, default 2.0), letting it stay
  sticky between bar lines. Swept against references: chord spans 106 → 110 on
  `He Who Knew No Sin`, and the share of changes landing on a downbeat went
  61% → 66%. Synthetic held at 99.9%; chart coverage held at 100% / 98%.
  Partial fix for "chords it misses completely" — better, not solved.
- Navigation on app-served charts: back to home, a recent-songs switcher, and a
  `.cho` download. Only rendered when the app is serving (a CLI-exported file
  has no server behind those links).
- **Fixed the server cache serving stale charts.** The key was file content plus
  options but not the code version, so editing the template kept serving charts
  built by older code — which sent me chasing a phantom "nav isn't rendering"
  bug. `RENDER_VERSION` is now part of the key; bump it when the template or
  player changes.
- Fixed doubled braces in the injected nav JS. It's passed as an argument to
  `.format()`, not used as a format string, so escaped braces would have emitted
  literal `{{` into the JavaScript.

- **Fixed vi chords coming back major** (`E` where `Em` belonged, in G shapes).
  Root cause was *not* the priors, which was the obvious suspect: the
  median-percentile noise floor was erasing the thirds. Thirds are often an
  order of magnitude quieter than the root, so subtracting the median bin took
  one recording's third-to-root ratio from 0.11 down to 0.02 — leaving no
  acoustic evidence at all, so the corpus prior (major 3:1 over minor) decided
  by default. Backing the prior out of the scores showed minor was *acoustically*
  winning by 0.54 all along.
  Fix: noise floor at the 25th percentile instead of the 50th. A joint sweep
  over floor x minor-prior showed floor is the real lever — at p25, B-rooted
  chords went 44% → **100%** correctly minor, and the prior value stopped
  mattering entirely, so the corpus priors were left untouched.
  No regressions: synthetic held at 99.9% with 0% 4-note inflation, and chart
  root coverage went 99% → 100% and 98% → 98% on the two charted songs.

- **Playback synced to the beat grid.** The exported HTML now plays the song:
  the current measure highlights, a playhead rides across it, the current beat
  outlines, and the view auto-scrolls (toggleable). Space plays/pauses, arrows
  seek ±5s, clicking a measure or beat jumps there.
- **Pause anywhere to get chord suggestions for that beat** — no server needed.
  Top-6 alternatives are precomputed for every beat at export time
  (`pipeline._beat_options`) and embedded in the page, so the whole thing works
  offline from a `file://` URL. This costs one extra full-vocabulary matrix
  multiply during analysis, since the recognizer was already scoring every
  chord at every beat and discarding the runners-up.
- Audio is referenced by a relative path rather than embedded, to keep the HTML
  ~100 KB instead of ~10 MB. If the link can't resolve (either file moved, page
  emailed on), the page shows a file picker instead of silently failing.

- **Two HTML views** via `--html`: a lead-sheet chart (4 measures per line) and a
  beat grid (one row per measure, one box per beat, chord placed on the beat it
  starts, scale degree underneath, held chords dimmed). Self-contained single
  file, dark theme, print stylesheet, works offline.
- Roman numerals / scale degrees (`theory.roman_numeral`), case-carrying quality
  as is conventional (`vi` vs `IV`, `°` for diminished).
- **Fixed the grid putting every chord change on the last beat of the bar.**
  Cause was boundary quantization, not a wrong beat grid: chord starts come from
  frame-quantized beat times (~23 ms) while downbeats are raw floats, so a change
  landed a few ms *before* its downbeat and got filed in the previous measure.
  Fixed by snapping chord starts onto the beat grid in `render._snap_to_beats`.
- `--suggest-at TIME` and `--progressions` (see above). Two bugs that only
  surfaced on real audio, both now fixed: the "detected here" footer
  double-transposed when `--capo`/`--transpose` was in play (so it contradicted
  its own table), and the diatonic/borrowed label tested only the chord *root*
  against the key, which called `Dm` diatonic in D major. It now requires every
  chord tone to belong to the key.
- Verified the whole `--html` path end-to-end on real audio, not just on stored
  JSON.
- ⚠️ `pipeline.align_downbeat_phase` was written to fix the above before the real
  cause was found. It's guarded and **has never actually fired** — on the one
  real song tested it correctly declined (61% of chord changes already landed on
  downbeats, no alternative phase beat that by the required margin). Downbeat
  phase errors are a real documented tracker failure mode, so it's kept, but it
  is unexercised code. Delete it if it hasn't proved useful by the time slash
  chords land.

- **Recalibrated chord-quality priors from corpus frequencies.** The originals
  were hand-guessed and far too weak. On the first real recording they produced
  36% sus4, 23% power chords, and only 13% plain major, at 0.31 confidence with
  69% of the track shaky. Priors are now genuine log-probabilities added after
  the emission scaling (so they no longer silently rescale with
  `emission_sharpness`), and the same file gives 89% major, 4% sus4, 0.44
  confidence, 32% shaky. The synthetic file stayed at 99.9% and its confidence
  rose 0.65 → 0.93.
- Added `--vocabulary simple|full`, defaulting to simple (maj/min/7/m7/sus4).
  Isolating the two changes showed the priors did most of the work, but the
  restricted vocabulary independently improved confidence 0.34 → 0.44 and cut
  shaky time 66% → 32%.

- Eval harness (`scripts/eval.py`). Two modes: a sanity report needing no
  annotations at all (quality mix, out-of-key chords, confidence, shaky time),
  and frame-level scoring against `.lab` files. It warns automatically when a
  track comes back >45% 4-note chords, since that's the 7th-inflation
  regression signature, and it reports the exact-vs-root accuracy gap so a
  quality problem is distinguishable from a chroma or timing problem.
- `testdata/` with a worked `.lab` example and a prioritized list of what
  material is worth testing.
- `scripts/make_lab.py` — converts a measure-based chord sheet into a timed
  `.lab` by snapping bars to detected downbeats, so building ground truth needs
  no hand-written timestamps. Verified by regenerating the synthetic annotation
  byte-for-byte.

- Core pipeline end-to-end: audio → beats/downbeats → chroma → chords → key,
  with beat-synchronous chord decisions and Viterbi smoothing.
- CLI with measure-grouped chart, timeline, transpose, capo, JSON export.
- Fixed a one-beat timing skew: `librosa.util.sync(pad=True)` only prepends a
  leading segment when beat one isn't at frame 0, so every chord was landing
  one beat early on tracks starting exactly on the downbeat.
- Fixed triads being reported as 7th chords. Two parts: harmonic-aware
  templates (a major third's 3rd harmonic *is* the major 7th — E→B in a C
  chord, so the leakage has to be predicted rather than treated as evidence),
  and median noise-floor subtraction.
- Established that log compression of chroma **hurts** on this material, badly
  — it amplifies the broadband noise floor into looking like chord tones.
  Grid search picked gamma=0. Don't re-add it without real-audio evidence.
- Fixed flat/sharp spelling: was testing a sharp name for membership in a set
  of flat names, so it never matched and capo'd charts read `A#` for `Bb`.
