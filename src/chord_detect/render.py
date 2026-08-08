"""Render an analysis as a self-contained HTML chord chart.

Two views over the same analysis:

  Chart -- measures laid out four per line, lead-sheet style. Compact, prints
           well, good for reading at a music stand.
  Grid  -- one row per measure, subdivided into beat cells, with the chord
           placed in the beat where it actually starts and its scale degree
           underneath. Shows *where in the bar* a change lands, which the chart
           view can't express. This is the view that opens by default, since
           it's the one you read while the song is playing; the chart takes
           over as the default only when there are no downbeats to draw.

The output is a single file with no external requests, so it opens offline and
can be emailed or printed as-is.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .pipeline import Analysis
from .theory import (
    NO_CHORD,
    SHAPE_KEYS,
    label_roman,
    parse_chord,
    transpose_label,
)


@dataclass
class Cell:
    """One beat within a measure."""

    beat: int  # 1-based position in the measure
    label: str = ""  # chord symbol, or "" if nothing starts here
    roman: str = ""
    continuation: bool = False  # chord carried in from an earlier measure
    start: float = 0.0


@dataclass
class Row:
    """One measure."""

    number: int
    start: float
    end: float
    cells: list[Cell] = field(default_factory=list)


def build_grid(analysis: Analysis, max_beats: int = 12) -> list[Row]:
    """Lay chords out on a beat grid.

    Returns an empty list when there are no downbeats -- without bar lines a
    grid would be arbitrary, and the chart view is the honest fallback.
    """
    downbeats = list(analysis.downbeats)
    if len(downbeats) < 2:
        return []

    beats = list(analysis.beats)
    edges = [*downbeats, analysis.duration]
    rows: list[Row] = []

    # Snap chord starts onto the beat grid before assigning them to cells.
    # Span boundaries come from frame-quantized beat times (~23 ms resolution)
    # while beats and downbeats are raw floats, so a change that musically lands
    # *on* a downbeat can sit a few milliseconds before it -- which would file it
    # in the last cell of the previous measure and make every chord look like it
    # changes on the final beat of the bar.
    starts = _snap_to_beats([s.start for s in analysis.chords], beats)

    for number, (start, end) in enumerate(zip(edges[:-1], edges[1:]), start=1):
        in_measure = [b for b in beats if start - 1e-6 <= b < end - 1e-6]
        if not in_measure:
            in_measure = [start]
        # Guard against a runaway beat grid producing absurdly wide rows.
        in_measure = in_measure[:max_beats]

        row = Row(number=number, start=start, end=end)
        cell_edges = [*in_measure, end]

        for index, (cell_start, cell_end) in enumerate(
            zip(cell_edges[:-1], cell_edges[1:]), start=1
        ):
            cell = Cell(beat=index, start=cell_start)

            # A chord "belongs" to the beat it starts on.
            starting = [
                span
                for span, snapped in zip(analysis.chords, starts)
                if cell_start - 1e-6 <= snapped < cell_end - 1e-6
                and span.label != NO_CHORD
            ]
            if starting:
                cell.label = starting[0].label
            elif index == 1:
                # Nothing new starts on the downbeat, so show what's still
                # sounding -- otherwise a chord held for four bars leaves three
                # rows blank and the chart becomes unreadable.
                held = analysis.chord_at(cell_start + 1e-3)
                if held is not None and held.label != NO_CHORD:
                    cell.label = held.label
                    cell.continuation = True

            if cell.label:
                cell.roman = label_roman(cell.label, analysis.key)
            row.cells.append(cell)

        rows.append(row)

    return rows


def _snap_to_beats(
    times: list[float], beats: list[float], tolerance_fraction: float = 0.35
) -> list[float]:
    """Move each time onto the nearest beat when it's already close to one.

    Tolerance is a fraction of the local beat interval, so it scales with tempo.
    Times that aren't near any beat are left alone -- genuinely off-beat chord
    changes should stay where they are.
    """
    if not beats:
        return list(times)

    beat_array = np.asarray(beats, dtype=float)
    interval = float(np.median(np.diff(beat_array))) if beat_array.size > 1 else 0.5
    tolerance = interval * tolerance_fraction

    snapped: list[float] = []
    for time in times:
        index = int(np.argmin(np.abs(beat_array - time)))
        nearest = float(beat_array[index])
        snapped.append(nearest if abs(nearest - time) <= tolerance else time)
    return snapped


def chart_lines(analysis: Analysis, per_line: int = 4) -> list[list[str]]:
    """Measure-grouped text chart: each entry is one measure's chords."""
    if analysis.measures:
        cells = [
            " ".join(m.chords) if m.chords else "—" for m in analysis.measures
        ]
    else:
        # No bar lines: fall back to the chord sequence with repeats collapsed.
        cells = []
        for span in analysis.chords:
            if span.label == NO_CHORD:
                continue
            if not cells or cells[-1] != span.label:
                cells.append(span.label)

    return [cells[i : i + per_line] for i in range(0, len(cells), per_line)]


def render_html(
    analysis: Analysis,
    *,
    title: str | None = None,
    capo: int = 0,
    source_key: str | None = None,
    audio_src: str | None = None,
    shift: int = 0,
    shapes: str | None = None,
    nav: bool = False,
) -> str:
    """Build the full HTML document.

    `audio_src` should be a path relative to where the HTML will be written, so
    the page can play the song and follow along. It's referenced rather than
    embedded to keep the file small; the page offers a file picker if the
    reference can't be resolved (the audio moved, or the page was emailed on).
    """
    rows = build_grid(analysis)
    lines = chart_lines(analysis)
    name = title or Path(analysis.path).stem

    minutes, seconds = divmod(int(analysis.duration), 60)
    meter = f"{analysis.meter}/4" if analysis.meter else "—"

    sounding_key = transpose_label(analysis.key, -shift) if shift else analysis.key
    facts = [
        ("Key", f'<span id="keyfact">{html.escape(analysis.key)}</span>'),
        ("Tempo", f"{analysis.tempo:.0f}"),
        ("Time", meter),
        ("Length", f"{minutes}:{seconds:02d}"),
        # These two are hidden at capo 0 and revealed by the capo control.
        ("Capo", f'<span id="capofact">{capo}</span>'),
        ("Sounds in", f'<span id="soundfact">{html.escape(sounding_key)}</span>'),
    ]

    player = (
        f'<audio id="audio" src="{html.escape(audio_src, quote=True)}" preload="metadata"></audio>'
        if audio_src
        else '<audio id="audio" preload="metadata"></audio>'
    )

    return _TEMPLATE.format(
        title=html.escape(name),
        facts="".join(
            f'<div class="fact"><span class="k">{k}</span>'
            f'<span class="v">{v}</span></div>'
            for k, v in facts
        ),
        player=player,
        chart=_render_chart(lines, shift),
        grid=_render_grid(rows, analysis, shift),
        nav=_NAV if nav else "",
        navjs=_NAV_JS if nav else "",
        # Only on server-served charts: editing lyrics needs somewhere to save
        # them, and a CLI export has no server behind it.
        chordprojs=_chordpro_bundle() if nav else "",
        shapeopts="".join(
            f'<option value="{s}"{" selected" if s == shapes else ""}>{s} shapes</option>'
            for s in SHAPE_KEYS
        ),
        capo0=capo,
        sounding=json.dumps(transpose_label(analysis.key, -shift)),
        # Beats & measures is the view you actually read while playing, so it
        # opens first -- unless there are no bar lines to draw, in which case
        # landing on an empty grid would be worse than the chart.
        chart_sel="false" if rows else "true",
        grid_sel="true" if rows else "false",
        chart_hidden=" hidden" if rows else "",
        grid_hidden="" if rows else " hidden",
        # Server-served only: the panel needs somewhere to fetch alignment from.
        lyricstab=(
            '<button role="tab" aria-selected="false" data-view="lyrics">'
            "Lyrics</button>"
            '<button role="tab" aria-selected="false" data-view="manual">'
            "Chart (Manual)</button>"
            '<button role="tab" aria-selected="false" data-view="pinned">'
            "Chart (Pinned)</button>"
            if nav
            else ""
        ),
        grid_note=(
            ""
            if rows
            else '<p class="empty">No downbeats were detected, so bar lines '
            "can't be placed. The chart view is the reliable one.</p>"
        ),
        # Timeline carries sounding pitch so the transport's "now playing"
        # chord re-spells with the capo like everything else.
        timeline=json.dumps(
            [
                {
                    "t": round(s.start, 3),
                    "c": s.label,
                    "r": _sounding_root(s.label, shift),
                    "q": _quality(s.label),
                }
                for s in analysis.chords
            ]
        ),
        options=json.dumps(
            [
                {"t": round(b.time, 3), "o": [[c, f] for c, f in b.options]}
                for b in analysis.beat_options
            ]
        ),
        key=json.dumps(analysis.key),
    )


def _sounding_root(label: str, shift: int) -> int | None:
    """Pitch class of a displayed chord as it actually sounds."""
    parsed = parse_chord(label) if label else None
    return None if parsed is None else (parsed[0] - shift) % 12


def _quality(label: str) -> str:
    parsed = parse_chord(label) if label else None
    return "" if parsed is None else parsed[1]


def _chord_span(label: str, shift: int, extra_class: str = "") -> str:
    """A chord marked up so the page can re-spell it at any capo.

    `data-root`/`data-q` carry the chord as it *sounds*, recovered by undoing
    whatever shift was applied for display. The rendered text stays correct
    without JavaScript (so printing works), while the data attributes let the
    capo control re-derive every label live.
    """
    parsed = parse_chord(label) if label else None
    classes = f"chord {extra_class}".strip()
    if parsed is None:
        return f'<span class="{classes}">{html.escape(label)}</span>'
    root, quality = parsed
    sounding = (root - shift) % 12
    return (
        f'<span class="{classes}" data-root="{sounding}" '
        f'data-q="{html.escape(quality, quote=True)}">{html.escape(label)}</span>'
    )


def _render_chart(lines: list[list[str]], shift: int) -> str:
    out: list[str] = []
    for line in lines:
        cells: list[str] = []
        for cell in line:
            if cell == "—":
                inner = '<span class="chord">—</span>'
            else:
                inner = " ".join(
                    _chord_span(chord, shift) for chord in cell.split()
                )
            cells.append(f'<span class="bar">{inner}</span>')
        out.append(f'<div class="chartline">{"".join(cells)}</div>')
    return "\n".join(out)


def _render_grid(rows: list[Row], analysis: Analysis, shift: int = 0) -> str:
    if not rows:
        return ""
    out: list[str] = []
    for row in rows:
        cells: list[str] = []
        for index, cell in enumerate(row.cells):
            classes = ["cell"]
            if cell.label:
                classes.append("filled")
            if cell.continuation:
                classes.append("held")
            # Each cell needs its own extent so the beat under the playhead can
            # be found without re-deriving it from the row every frame.
            cell_end = (
                row.cells[index + 1].start
                if index + 1 < len(row.cells)
                else row.end
            )
            roman = html.escape(cell.roman)
            cells.append(
                f'<div class="{" ".join(classes)}" data-t="{cell.start:.3f}"'
                f' data-end="{cell_end:.3f}">'
                f"{_chord_span(cell.label, shift)}"
                f'<span class="roman">{roman}</span></div>'
            )
        out.append(
            f'<div class="row" data-start="{row.start:.3f}" '
            f'data-end="{row.end:.3f}">'
            f'<div class="num">{row.number}</div>'
            f'<div class="cells">{"".join(cells)}'
            f'<div class="playhead"></div></div></div>'
        )
    return "\n".join(out)


# Only rendered when the app is serving the page -- a standalone file exported
# by the CLI has no server behind these links.
_NAV = """<nav class="nav">
  <a class="navlink" href="/">&#8592; New song</a>
  <select class="navsel" id="songswitch"><option value="">Recent songs…</option></select>
  <button class="navlink right" id="toledgr" hidden>Upload to Ledgr</button>
  <a class="navlink right" id="chodl" href="#" download>Download .cho</a>
</nav>"""

# NOTE: single braces here. This string is passed *as an argument* to
# _TEMPLATE.format(), so it is never itself a format string -- doubling the
# braces would emit literal "{{" into the JavaScript.
_NAV_JS = """
  fetch('/library').then(function (r) { return r.json(); }).then(function (rows) {
    var sel = document.getElementById('songswitch');
    var here = window.location.pathname.split('/').pop();
    rows.forEach(function (row) {
      var opt = document.createElement('option');
      opt.value = row.id;
      opt.textContent = row.name + (row.id === here ? '  (this one)' : '');
      if (row.id === here) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', function () {
      if (sel.value && sel.value !== here) window.location.href = '/chart/' + sel.value;
    });
    var dl = document.getElementById('chodl');
    if (dl) dl.href = '/chordpro/' + here;
    // Title comes from the library rather than being baked in, so renaming a
    // song updates its chart without re-rendering the page.
    var mine = rows.filter(function (row) { return row.id === here; })[0];
    if (mine) {
      var h1 = document.querySelector('h1');
      if (h1) h1.textContent = mine.name;
      document.title = mine.name;
    }
  }).catch(function () {});

  // ---- Lyrics panel ------------------------------------------------------
  // Words appear under the playhead as the song plays. Lines the aligner is
  // unsure about are marked rather than hidden: knowing which few to check is
  // the difference between correcting a chart and re-doing one.
  (function () {
    var songId = window.location.pathname.split('/').pop();
    var panel = document.getElementById('lyriclines');
    var status = document.getElementById('lyricstate');
    var tab = document.querySelector('.tabs button[data-view="lyrics"]');
    if (!panel || !tab) return;
    var LINES = [], anchors = [], current = -1;

    function say(html) { status.innerHTML = html; }

    function draw() {
      panel.innerHTML = '';
      var section = null;
      LINES.forEach(function (line, i) {
        if (line.label !== section) {
          section = line.label;
          var h = document.createElement('div');
          h.className = 'lsection';
          h.textContent = section;
          panel.appendChild(h);
        }
        var el = document.createElement('div');
        el.className = 'lline' + (line.flags && line.flags.length ? ' suspect' : '');
        el.dataset.index = i;
        var t = document.createElement('span');
        t.className = 'ltime';
        t.textContent = fmt(line.start);
        el.appendChild(t);
        var words = document.createElement('span');
        words.textContent = line.text;
        el.appendChild(words);
        if (line.flags && line.flags.length) {
          var f = document.createElement('span');
          f.className = 'lflag';
          f.textContent = line.flags.join(' ');
          el.appendChild(f);
        }
        // Click to jump; shift-click to anchor this line to the current time,
        // which is the rescue when a stretch has drifted.
        el.addEventListener('click', function (ev) {
          if (ev.shiftKey) {
            anchors = anchors.filter(function (a) { return a[0] !== i; });
            anchors.push([i, audio.currentTime || 0]);
            say('Anchor set on line ' + i + ' at ' + fmt(audio.currentTime || 0) +
                '. <button class="small" id="realign">Re-align with ' +
                anchors.length + ' anchor(s)</button>');
            document.getElementById('realign').addEventListener('click', runAlign);
          } else {
            audio.currentTime = line.start;
            update();
          }
        });
        panel.appendChild(el);
      });
    }

    function fmt(t) {
      var m = Math.floor(t / 60), s = Math.floor(t % 60);
      return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function follow(now) {
      if (!LINES.length) return;
      var at = -1;
      for (var i = 0; i < LINES.length; i++) {
        if (LINES[i].start <= now) at = i; else break;
      }
      if (at === current) return;
      current = at;
      var all = panel.querySelectorAll('.lline');
      all.forEach(function (el) { el.classList.remove('now'); });
      if (at >= 0 && all[at]) {
        all[at].classList.add('now');
        if (!document.getElementById('lyrics').hidden) {
          all[at].scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
      }
    }
    window.__lyricFollow = follow;

    function runAlign() {
      say('Aligning… this takes about as long as the song is minutes.');
      fetch('/align/' + songId, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ anchors: anchors })
      }).then(poll);
    }

    function poll() {
      fetch('/align/' + songId).then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.running) {
            say('Aligning… ' + (d.stage || '') +
                ' ' + Math.round((d.fraction || 0) * 100) + '%');
            return setTimeout(poll, 1000);
          }
          if (d.error) return say('Alignment failed — ' + d.error);
          if (!d.aligned) return offer();
          LINES = d.lines || [];
          anchors = d.anchors || [];
          var bad = LINES.filter(function (l) { return l.flags && l.flags.length; }).length;
          say((d.stale ? '<b>Lyrics changed since this was aligned.</b> ' : '') +
              LINES.length + ' lines aligned' +
              (bad ? ', ' + bad + ' worth checking (marked)' : '') +
              '. Click a line to jump; shift-click to anchor it to the current time. ' +
              '<button class="small" id="realign">Re-align</button>');
          document.getElementById('realign').addEventListener('click', runAlign);
          draw();
        }).catch(function () { say('Could not load alignment.'); });
    }

    function offer() {
      fetch('/lyrics/' + songId).then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.lyrics) {
            say('No lyrics yet for this song. Add them from the home page when ' +
                'you upload, or paste them below.' +
                '<div><textarea id="lyricsbox" rows="8" placeholder="Verse 1&#10;…"></textarea>' +
                '<button class="small" id="savelyrics">Save lyrics</button></div>');
          } else {
            say('Lyrics saved (' + d.lyrics.split('\\n').filter(Boolean).length +
                ' lines) but not aligned yet. ' +
                '<button class="small" id="realign">Align to the recording</button>' +
                '<div><textarea id="lyricsbox" rows="8"></textarea>' +
                '<button class="small" id="savelyrics">Update lyrics</button></div>');
            document.getElementById('lyricsbox').value = d.lyrics;
            document.getElementById('realign').addEventListener('click', runAlign);
          }
          document.getElementById('savelyrics').addEventListener('click', function () {
            fetch('/lyrics/' + songId, {
              method: 'PUT',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ lyrics: document.getElementById('lyricsbox').value })
            }).then(function () { poll(); });
          });
        });
    }

    tab.addEventListener('click', function () {
      if (!LINES.length) poll();
    });
    poll();
  })();

  // ---- Manual chord placement -------------------------------------------
  // Two ways at the same job, sharing one implementation.
  //   manual  -- pick any chord, put it on any word, in any order.
  //   pinned  -- step through the detected chord changes in order. Each carries
  //              its own timestamp, which is the part of the analysis that
  //              proved trustworthy, so a placed chart still knows where it
  //              sits in the recording even though lyric timing doesn't.
  function placement(mode) {
    var songId = window.location.pathname.split('/').pop();
    var bar = document.getElementById(mode + 'bar');
    var wrap = document.getElementById(mode + 'words');
    if (!bar || !wrap) return null;

    var LINES = [];          // {label, words:[..]}
    var placed = {};         // "line:word" -> {chord, t}
    var intro = '';          // bar notation, e.g. "Em / / G | Em / / G"
    var queue = [];          // pinned: the detected chord run
    var at = 0;              // pinned: position in that run
    var pick = null;         // chord armed, awaiting a word
    var history = [];        // placement order, so Undo can step back
    var dirty = false;

    function key(l, w) { return l + ':' + w; }

    function appendIntro(token) {
      var base = intro.replace(/\\s+$/, '');
      return base ? base + ' ' + token : token;
    }

    // Spell a pitch class without re-applying the capo. `spell()` subtracts it,
    // which is right for a detected chord but wrong for one derived from the
    // key we're already playing in.
    function nameOf(pc, quality) {
      var names = useFlats() ? FLAT : SHARP;
      return names[((pc % 12) + 12) % 12] + (quality || '');
    }

    // Everything the song might want, in the shapes currently selected.
    // Detected chords alone isn't enough: a chord that's plainly in the key but
    // never got detected still has to be one click away.
    function vocabulary() {
      var seen = {}, out = [];
      function add(label) {
        if (label && !seen[label]) { seen[label] = 1; out.push(label); }
      }
      (TIMELINE || []).forEach(function (row) {
        if (row.c && row.c !== 'N') add(spell(row.r, row.q));
      });
      var k = parseChord(shapeKey());
      if (k) {
        var steps = [0, 2, 4, 5, 7, 9, 11];
        var quals = k.q === 'm' ? ['m', 'dim', '', 'm', 'm', '', '']
                                : ['', 'm', 'm', '', '', 'm', 'dim'];
        for (var i = 0; i < 7; i++) add(nameOf(k.root + steps[i], quals[i]));
      }
      return out;
    }

    // The chord sounding at a moment -- what "Place" uses, so playing along and
    // hitting Place puts down whatever you're hearing.
    function chordAt(now) {
      var found = null;
      (TIMELINE || []).forEach(function (row) {
        if (row.t <= now + 0.02 && row.c && row.c !== 'N') found = row;
      });
      return found;
    }

    function buildQueue() {
      queue = (TIMELINE || []).filter(function (row) {
        return row.c && row.c !== 'N';
      });
    }

    function save() {
      var rows = Object.keys(placed).map(function (k) {
        var parts = k.split(':');
        var here = placed[k];
        return {
          line: parseInt(parts[0], 10), word: parseInt(parts[1], 10),
          // Save what's on screen, so the export matches the shapes chosen.
          chord: here.r == null ? here.chord : spell(here.r, here.q),
          t: here.t
        };
      });
      return fetch('/placement/' + songId + '/' + mode, {
        method: 'PUT', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ placements: rows, intro: intro })
      }).then(function () { dirty = false; status(); });
    }

    function status() {
      var count = Object.keys(placed).length;
      var tail = '<span class="pcount">' + count + ' placed' +
                 (dirty ? ' &middot; <b>unsaved</b>' : '') + '</span>' +
                 '<button class="small" data-act="save">Save</button>';

      if (mode === 'pinned') {
        var live = chordAt(audio.currentTime || 0);
        bar.innerHTML =
          '<span class="pnext">Sounding now: <b>' +
          (live ? spell(live.r, live.q) : '—') + '</b></span>' +
          '<button class="small go" data-act="place">Place it</button>' +
          '<span class="phelp">then click a word &mdash; or click a word to ' +
          'place what&rsquo;s sounding there</span>' +
          // The intro has no words to click, so it gets its own way in: append
          // the sounding chord, a held beat, or a bar line, straight into the
          // bar notation at the top.
          '<span class="introadd">Intro: ' +
          '<button class="small" data-act="i-chord">+ chord</button>' +
          '<button class="small" data-act="i-beat">+ /</button>' +
          '<button class="small" data-act="i-bar">+ |</button>' +
          '<button class="small" data-act="i-del">&larr;</button>' +
          '</span>' +
          '<button class="small" data-act="undo">Undo last</button>' + tail;
      } else {
        bar.innerHTML =
          '<span class="pnext">' +
          (pick ? 'Now click a word for <b>' + pick + '</b>'
                : 'Pick a chord, then click a word') +
          '</span>' +
          '<span class="ppal">' + vocabulary().map(function (c) {
            return '<button class="chip' + (c === pick ? ' on' : '') +
                   '" data-chord="' + c + '">' + c + '</button>';
          }).join('') +
          '<input class="chordin" id="' + mode + 'typed" placeholder="or type…" ' +
          'value="" size="7" spellcheck="false">' +
          '</span>' + tail;
        var typed = document.getElementById(mode + 'typed');
        if (typed) {
          typed.addEventListener('change', function () {
            if (typed.value.trim()) { pick = typed.value.trim(); status(); }
          });
        }
      }
    }

    function draw() {
      wrap.innerHTML = '';

      // Every song gets an intro whether or not the lyrics mention one, since
      // there are always some bars before the first word. It's typed as bars
      // rather than placed on words, because there are no words yet.
      var introWrap = document.createElement('div');
      introWrap.className = 'introbox';
      var introLabel = document.createElement('div');
      introLabel.className = 'lsection';
      introLabel.textContent = 'Intro';
      introWrap.appendChild(introLabel);
      var introField = document.createElement('input');
      introField.type = 'text';
      introField.className = 'introin';
      introField.value = intro;
      introField.placeholder = 'Em / / G | Em / / G';
      introField.spellcheck = false;
      introField.addEventListener('input', function () {
        intro = introField.value;
        dirty = true;
      });
      introWrap.appendChild(introField);
      var hint = document.createElement('div');
      hint.className = 'introhint';
      hint.textContent =
        'One bar per group, one token per beat. "/" holds the previous chord. ' +
        'Separate bars with "|".';
      introWrap.appendChild(hint);
      wrap.appendChild(introWrap);

      var section = null;
      LINES.forEach(function (line, li) {
        if (line.label !== section) {
          section = line.label;
          var h = document.createElement('div');
          h.className = 'lsection';
          h.textContent = section;
          wrap.appendChild(h);
        }
        var row = document.createElement('div');
        row.className = 'pline';

        // A chord can land before the first word or after the last -- a lead-in
        // or a turnaround. Without a slot for those they'd have to be shoved
        // onto a word they don't belong to.
        function slotFor(wi, word, edge) {
          var slot = document.createElement('span');
          slot.className = 'pword' + (edge ? ' edge' : '');
          var here = placed[key(li, wi)];
          if (here) {
            var c = document.createElement('span');
            c.className = 'pchord';
            // Re-spell for the shapes currently selected, so changing capo
            // moves every placed chord with it.
            c.textContent = here.r == null ? here.chord : spell(here.r, here.q);
            slot.appendChild(c);
            slot.classList.add('has');
          }
          var t = document.createElement('span');
          t.className = 'ptext';
          t.textContent = edge ? '\\u00b7' : word;
          slot.appendChild(t);
          slot.addEventListener('click', function (ev) {
            if (ev.shiftKey || ev.altKey) { clear(li, wi); return; }
            put(li, wi);
          });
          return slot;
        }

        row.appendChild(slotFor(-1, '', true));
        line.words.forEach(function (word, wi) {
          row.appendChild(slotFor(wi, word, false));
        });
        row.appendChild(slotFor(line.words.length, '', true));
        wrap.appendChild(row);
      });
      status();
    }

    // Store pitch, not just a name. A chord written down as "C" in G shapes
    // has to become "A" when the capo moves, so the placement has to remember
    // what note it was -- keeping only the label freezes it at the spelling it
    // happened to have when placed.
    function remember(label, t) {
      var parsed = parseChord(label);
      return parsed
        ? { chord: label, r: (parsed.root + capo) % 12, q: parsed.q, t: t }
        : { chord: label, r: null, q: null, t: t };
    }

    function put(li, wi) {
      if (mode === 'pinned') {
        // Whatever was armed by "Place it", else whatever is sounding now, so
        // playing along and clicking words is enough on its own.
        var row = pick ? null : chordAt(audio.currentTime || 0);
        var label = pick || (row ? spell(row.r, row.q) : null);
        if (!label) return;
        placed[key(li, wi)] = remember(label, row ? row.t : null);
        pick = null;
      } else {
        if (!pick) return;
        placed[key(li, wi)] = remember(pick, null);
      }
      history.push(key(li, wi));
      dirty = true;
      draw();
    }

    function clear(li, wi) {
      if (placed[key(li, wi)]) {
        delete placed[key(li, wi)];
        dirty = true;
        draw();
      }
    }

    bar.addEventListener('click', function (ev) {
      var chip = ev.target.closest ? ev.target.closest('[data-chord]') : null;
      if (chip) { pick = chip.dataset.chord; status(); return; }
      var btn = ev.target.closest ? ev.target.closest('[data-act]') : null;
      if (!btn) return;
      var act = btn.dataset.act;
      if (act === 'save') save();
      if (act === 'place') {
        var row = chordAt(audio.currentTime || 0);
        if (row) { pick = spell(row.r, row.q); status(); }
      }
      if (act.indexOf('i-') === 0) {
        var live = chordAt(audio.currentTime || 0);
        if (act === 'i-chord' && live) intro = appendIntro(spell(live.r, live.q));
        if (act === 'i-beat') intro = appendIntro('/');
        if (act === 'i-bar') intro = appendIntro('|');
        if (act === 'i-del') {
          var bits = intro.trim().split(/\\s+/);
          bits.pop();
          intro = bits.join(' ');
        }
        dirty = true;
        draw();
      }
      if (act === 'undo') {
        var last = history.pop();
        if (last) { delete placed[last]; dirty = true; draw(); }
      }
    });

    // Keep "Sounding now" honest while the song plays.
    audio.addEventListener('timeupdate', function () {
      if (mode === 'pinned' && !document.getElementById('pinned').hidden) status();
    });

    function load() {
      buildQueue();
      return fetch('/lyrics/' + songId).then(function (r) { return r.json(); })
        .then(function (d) {
          LINES = linesFrom(d.lyrics || '');
          if (!LINES.length) {
            bar.innerHTML = '<span class="pnext">Add lyrics on the Lyrics tab first.</span>';
            wrap.innerHTML = '';
            return;
          }
          return fetch('/placement/' + songId + '/' + mode)
            .then(function (r) { return r.json(); })
            .then(function (p) {
              placed = {};
              intro = p.intro || '';
              (p.placements || []).forEach(function (row) {
                placed[key(row.line, row.word)] = { chord: row.chord, t: row.t };
              });
              // Resume where the queue left off rather than from the top.
              if (mode === 'pinned') at = Math.min(queue.length, (p.placements || []).length);
              draw();
            });
        });
    }

    return { load: load, redraw: draw };
  }

  // Section markers are [Verse 1] style; everything else is a sung line.
  function linesFrom(text) {
    var out = [], label = 'Song';
    text.split('\\n').forEach(function (raw) {
      var s = raw.trim().replace(/^\\\\\\[/, '[').replace(/\\\\\\]$/, ']');
      if (!s) return;
      var m = s.match(/^\\[(.+?)\\]$/);
      if (m) { label = m[1]; return; }
      out.push({ label: label, words: s.split(/\\s+/) });
    });
    return out;
  }

  var PLACERS = { manual: placement('manual'), pinned: placement('pinned') };

  // Changing capo or shapes re-spells every chord on the page; the placement
  // views hold chords too, so they have to be told.
  window.__refreshPlacers = function () {
    Object.keys(PLACERS).forEach(function (mode) {
      if (PLACERS[mode]) PLACERS[mode].redraw();
    });
  };
  Object.keys(PLACERS).forEach(function (mode) {
    var tab = document.querySelector('.tabs button[data-view="' + mode + '"]');
    if (tab && PLACERS[mode]) {
      tab.addEventListener('click', function () { PLACERS[mode].load(); });
    }
  });

  // Upload to Ledgr. The button only appears when the server has a token, so
  // the common no-token case is a hidden button rather than a failed click.
  fetch('/ledgr').then(function (r) { return r.json(); }).then(function (cfg) {
    var btn = document.getElementById('toledgr');
    if (!btn || !cfg.configured) return;
    var id = window.location.pathname.split('/').pop();
    btn.hidden = false;
    btn.title = 'Send this chart to ' + cfg.target;
    btn.addEventListener('click', function () {
      btn.disabled = true;
      var label = btn.textContent;
      btn.textContent = 'Uploading…';
      fetch('/ledgr/' + id, { method: 'POST' })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, b: b }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.b.error || 'upload failed');
          // Opened rather than navigated to: you're usually still playing along
          // here, and losing the audio position to a page change is annoying.
          btn.textContent = 'Opened in Ledgr';
          window.open(res.b.url, '_blank');
        })
        .catch(function (err) {
          btn.textContent = label;
          window.alert('Ledgr upload failed — ' + err.message);
        })
        .finally(function () { btn.disabled = false; });
    });
  }).catch(function () {});
"""

# Bump when the template or player changes, so the server's cache serves a
# freshly rendered chart instead of one built by older code.
RENDER_VERSION = 12

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #16202e; --panel: #1e2b3d; --cell: #3a4657; --cell-on: #46536a;
    --ink: #e8edf5; --dim: #8fa0b8; --gold: #e8c98a; --line: #2b3purple;
    --line: #2b3a4f;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
    font: 16px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ font-size: 1.35rem; margin: 0 0 14px; font-weight: 600; }}
  .facts {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }}
  .fact {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 12px; display: flex; gap: 8px; align-items: baseline;
  }}
  .fact .k {{ color: var(--dim); font-size: .72rem; text-transform: uppercase;
              letter-spacing: .06em; }}
  .fact .v {{ color: var(--gold); font-weight: 600; }}

  .tabs {{ display: flex; gap: 6px; margin-bottom: 16px; }}
  .tabs button {{
    background: var(--panel); color: var(--dim); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 18px; font-size: .9rem; cursor: pointer;
    font-weight: 600;
  }}
  .tabs button[aria-selected="true"] {{ background: var(--cell-on);
    color: var(--ink); border-color: var(--gold); }}
  .view[hidden] {{ display: none; }}

  /* Chart view */
  .chartline {{ display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 0; margin-bottom: 8px; }}
  .bar {{
    border-left: 2px solid var(--gold); padding: 12px 14px; min-height: 52px;
    background: var(--panel); color: var(--gold); font-weight: 600;
    font-size: 1.05rem; letter-spacing: .02em;
  }}
  .chartline .bar:last-child {{ border-right: 2px solid var(--gold); }}

  /* Grid view */
  .row {{ display: flex; align-items: stretch; gap: 10px; margin-bottom: 6px; }}
  .num {{ width: 26px; flex: none; color: var(--dim); font-size: .8rem;
    display: flex; align-items: center; justify-content: flex-end; }}
  .cells {{ display: flex; flex: 1; gap: 4px; }}
  .cell {{
    flex: 1; min-height: 62px; background: var(--cell); border-radius: 4px;
    padding: 7px 9px; display: flex; flex-direction: column; gap: 2px;
    border-left: 3px solid transparent;
  }}
  .cell.filled {{ background: var(--cell-on); border-left-color: var(--gold); }}
  .cell .chord {{ color: var(--gold); font-size: 1.15rem; font-weight: 600;
    line-height: 1.1; }}
  .cell .roman {{ color: var(--ink); font-size: .82rem; opacity: .75; }}
  .cell.held .chord {{ opacity: .45; font-weight: 500; }}
  .cell.held {{ border-left-color: #6b7688; }}

  .legend, .empty {{ color: var(--dim); font-size: .82rem; margin-top: 14px; }}

  /* Navigation (app only) */
  .nav {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
    flex-wrap: wrap; }}
  .navlink {{ color: var(--gold); text-decoration: none; font-size: .88rem;
    font-weight: 600; background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 13px; }}
  .navlink:hover {{ background: var(--cell); }}
  .navlink.right {{ margin-left: auto; }}
  .navsel {{ background: var(--panel); color: var(--ink); border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 10px; font-size: .85rem; max-width: 280px; }}

  /* Lyrics view */
  #lyricstate {{ color: var(--dim); font-size: .84rem; line-height: 1.6;
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 14px; margin-bottom: 14px; }}
  #lyricstate textarea {{ width: 100%; margin-top: 10px; background: var(--cell);
    color: var(--ink); border: 1px solid var(--line); border-radius: 7px;
    padding: 9px 11px; font: inherit; font-size: .86rem; }}
  .lsection {{ color: var(--gold); font-size: .72rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: .07em; margin: 18px 0 6px; }}
  .lline {{ display: flex; gap: 12px; align-items: baseline; padding: 6px 10px;
    border-radius: 7px; cursor: pointer; border-left: 3px solid transparent; }}
  .lline:hover {{ background: var(--panel); }}
  .lline.now {{ background: var(--cell); border-left-color: var(--gold); }}
  .lline.suspect {{ border-left-color: #b5793f; }}
  .ltime {{ color: var(--dim); font-size: .72rem; min-width: 38px;
    font-variant-numeric: tabular-nums; }}
  .lflag {{ color: #b5793f; font-size: .68rem; text-transform: uppercase;
    letter-spacing: .05em; margin-left: auto; }}

  /* Chord placement */
  /* Pinned to the bottom of the window rather than sticky in the flow: the
     transport is already sticky at the top, and two competing sticky bars
     fight over the same space. This way the palette is always reachable no
     matter how far down the lyrics you've scrolled. */
  .placebar {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    background: var(--panel); border-top: 1px solid var(--line);
    padding: 10px 16px; max-height: 42vh; overflow-y: auto;
    box-shadow: 0 -6px 18px rgba(0,0,0,.35); }}
  .placewords {{ padding-bottom: 190px; }}
  .pnext {{ font-size: .9rem; color: var(--ink); }}
  .pnext b {{ color: var(--gold); }}
  .pcount {{ margin-left: auto; color: var(--dim); font-size: .76rem; }}
  .ppal {{ display: flex; gap: 5px; flex-wrap: wrap; }}
  .chip {{ background: var(--cell); color: var(--ink); border: 1px solid var(--line);
    border-radius: 6px; padding: 4px 9px; font-size: .82rem; font-weight: 600;
    cursor: pointer; }}
  .chip.on {{ background: var(--gold); color: #16202e; border-color: var(--gold); }}
  .pline {{ display: flex; flex-wrap: wrap; gap: 2px; margin: 0 0 12px; }}
  .pword {{ position: relative; padding: 20px 4px 4px; border-radius: 5px;
    cursor: pointer; line-height: 1.35; }}
  .pword:hover {{ background: var(--cell); outline: 1px solid var(--line); }}
  .pword.has {{ background: rgba(232,201,138,.10); }}
  /* Centred over the word, as printed charts set them -- a chord at the left
     edge reads as belonging to the gap before the word. */
  .pchord {{ position: absolute; top: 1px; left: 50%; transform: translateX(-50%);
    color: var(--gold); font-size: .78rem; font-weight: 700; white-space: nowrap; }}
  .ptext {{ font-size: .98rem; }}
  /* Lead-in and turnaround slots at each end of a line. */
  .pword.edge {{ opacity: .35; min-width: 16px; text-align: center; }}
  .pword.edge:hover {{ opacity: 1; }}
  .pword.edge.has {{ opacity: 1; }}
  .phelp {{ color: var(--dim); font-size: .74rem; }}
  .chordin {{ background: var(--cell); color: var(--ink); border: 1px solid var(--line);
    border-radius: 6px; padding: 4px 8px; font-size: .82rem; }}
  .small.go {{ background: var(--gold); color: #16202e; border-color: var(--gold);
    font-weight: 700; }}
  @media print {{ .placebar {{ display: none; }} .placewords {{ padding-bottom: 0; }} }}
  .introbox {{ margin-bottom: 18px; }}
  .introin {{ width: 100%; background: var(--cell); color: var(--ink);
    border: 1px solid var(--line); border-radius: 7px; padding: 9px 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .92rem; letter-spacing: .02em; }}
  .introhint {{ color: var(--dim); font-size: .72rem; margin-top: 5px; }}

  /* Transport */
  .transport {{
    position: sticky; top: 0; z-index: 5; background: var(--bg);
    padding: 10px 0 12px; margin-bottom: 6px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  .transport button {{
    background: var(--gold); color: #14202e; border: 0; border-radius: 999px;
    width: 42px; height: 42px; font-size: 1rem; cursor: pointer; font-weight: 700;
    flex: none;
  }}
  .transport .small {{
    background: var(--panel); color: var(--ink); border: 1px solid var(--line);
    width: auto; height: 32px; padding: 0 12px; font-size: .8rem; font-weight: 600;
    border-radius: 8px;
  }}
  .transport input[type=range] {{ flex: 1; min-width: 140px; accent-color: var(--gold); }}
  .clock {{ color: var(--dim); font-variant-numeric: tabular-nums; font-size: .85rem;
    flex: none; }}
  .nowchord {{ color: var(--gold); font-weight: 700; font-size: 1.1rem; min-width: 3.5em; }}
  .transport select.small {{ height: 32px; }}
  .capobox {{ display: flex; align-items: center; gap: 6px; color: var(--dim);
    font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }}
  .capobox b {{ color: var(--gold); font-size: 1rem; min-width: 1.2em;
    text-align: center; font-variant-numeric: tabular-nums; }}
  .transport button.tiny {{ width: 28px; height: 28px; padding: 0; font-size: 1rem;
    line-height: 1; }}
  .loadaudio {{ color: var(--dim); font-size: .8rem; }}
  .loadaudio input {{ display: none; }}
  .loadaudio label {{ color: var(--gold); cursor: pointer; text-decoration: underline; }}

  /* Playback highlighting */
  .cells {{ position: relative; }}
  .playhead {{
    position: absolute; top: -2px; bottom: -2px; width: 2px; background: #fff;
    box-shadow: 0 0 6px rgba(255,255,255,.7); left: 0; opacity: 0;
    pointer-events: none; border-radius: 2px;
  }}
  .row.current .playhead {{ opacity: 1; }}
  .row.current .cell {{ background: #4d5b73; }}
  .row.current .cell.filled {{ background: #5d6c86; }}
  .row.current .num {{ color: var(--gold); font-weight: 700; }}
  .cell.now {{ outline: 1px solid rgba(232,201,138,.55); }}
  .row {{ scroll-margin: 96px; cursor: pointer; }}

  /* Suggestions */
  #suggest {{
    position: sticky; bottom: 0; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
    margin-top: 16px;
  }}
  #suggest[hidden] {{ display: none; }}
  #suggest h2 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--dim); margin: 0 0 10px; font-weight: 600; }}
  .opts {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .opt {{
    background: var(--cell); border-radius: 8px; padding: 7px 11px;
    display: flex; align-items: baseline; gap: 8px; border-left: 3px solid var(--gold);
  }}
  .opt .c {{ color: var(--gold); font-weight: 700; }}
  .opt .f {{ color: var(--dim); font-size: .78rem; }}
  .opt.cur {{ background: var(--cell-on); }}

  @media (max-width: 620px) {{
    body {{ padding: 14px; }}
    .chartline {{ grid-template-columns: repeat(2, 1fr); }}
    .cell {{ min-height: 52px; padding: 5px 6px; }}
    .cell .chord {{ font-size: .95rem; }}
    .cell .roman {{ font-size: .7rem; }}
  }}
  @media print {{
    body {{ background: #fff; color: #111; padding: 0; }}
    .tabs {{ display: none; }}
    .view[hidden] {{ display: block !important; }}
    .fact {{ background: #fff; border-color: #bbb; }}
    .fact .v, .bar, .cell .chord {{ color: #111; }}
    .bar {{ background: #fff; border-left-color: #111; }}
    .cell {{ background: #f2f2f2; border-left-color: #999; }}
    .cell.filled {{ background: #e2e2e2; border-left-color: #111; }}
  }}
</style>
</head>
<body>
{nav}
<h1>{title}</h1>
<div class="facts">{facts}</div>

{player}
<div class="transport">
  <button id="play" title="Play / pause (space)">&#9654;</button>
  <span class="nowchord" id="nowchord">&mdash;</span>
  <input type="range" id="seek" min="0" max="1000" value="0" step="1">
  <span class="clock"><span id="cur">0:00</span> / <span id="dur">0:00</span></span>
  <button class="small" id="back">&#8592; 5s</button>
  <button class="small" id="fwd">5s &#8594;</button>
  <button class="small" id="follow" aria-pressed="true">Follow</button>
  <select class="small" id="shapes" title="Play these shapes; capo is worked out for you">
    <option value="">Sounding key</option>
    {shapeopts}
  </select>
  <span class="capobox">Capo
    <button class="small tiny" id="capodown">&minus;</button>
    <b id="capoval">{capo0}</b>
    <button class="small tiny" id="capoup">+</button>
  </span>
  <span class="loadaudio" id="loadaudio" hidden>
    Audio not found &mdash;
    <label for="audiofile">choose the file</label>
    <input type="file" id="audiofile" accept="audio/*">
  </span>
</div>

<div class="tabs" role="tablist">
  <button role="tab" aria-selected="{grid_sel}" data-view="grid">Beats &amp; measures</button>
  {lyricstab}
  <button role="tab" aria-selected="{chart_sel}" data-view="chart">Chart</button>
</div>

<section class="view" id="chart"{chart_hidden}>
  {chart}
  <p class="legend">Four measures per line. Each measure lists the chords played in it.</p>
</section>

<section class="view" id="lyrics" hidden>
  <div id="lyricstate"></div>
  <div id="lyriclines"></div>
</section>

<section class="view" id="manual" hidden>
  <div class="placebar" id="manualbar"></div>
  <div class="placewords" id="manualwords"></div>
</section>

<section class="view" id="pinned" hidden>
  <div class="placebar" id="pinnedbar"></div>
  <div class="placewords" id="pinnedwords"></div>
</section>

<section class="view" id="grid"{grid_hidden}>
  {grid}
  {grid_note}
  <p class="legend">One row per measure; each box is one beat. A chord appears in
  the beat it starts on, with its scale degree underneath. Dimmed chords are held
  over from a previous measure. Click any measure to jump there.</p>
  <div id="suggest" hidden>
    <h2>Chords that fit here &mdash; <span id="suggestat"></span></h2>
    <div class="opts" id="opts"></div>
  </div>
</section>

<script>
(function () {{
  var TIMELINE = {timeline};
  var OPTIONS = {options};
  var KEY = {key};
  var SOUNDING = {sounding};   // key as the recording actually sounds
  var capo = {capo0};
  window.CHORD_TIMELINE = TIMELINE;

  // ---- live transposition -------------------------------------------------
  var SHARP = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
  var FLAT  = ['C','Db','D','Eb','E','F','Gb','G','Ab','A','Bb','B'];
  var FLAT_MAJOR = [5,10,3,8,1,6];
  var FLAT_MINOR = [2,7,0,5,10,3];

  function parseChord(label) {{
    if (!label || label === 'N') return null;
    for (var len = 2; len >= 1; len--) {{
      var head = label.slice(0, len), tail = label.slice(len);
      var i = SHARP.indexOf(head);
      if (i < 0) i = FLAT.indexOf(head);
      if (i >= 0) return {{ root: i, q: tail }};
    }}
    return null;
  }}

  // Spelling follows the key the capo puts us in, so a chart at capo 3 in Bb
  // reads G/C/D rather than a mix of sharps and flats.
  function useFlats() {{
    var k = parseChord(SOUNDING);
    if (!k) return false;
    var root = (k.root - capo + 1200) % 12;
    var table = k.q === 'm' ? FLAT_MINOR : FLAT_MAJOR;
    return table.indexOf(root) >= 0;
  }}

  function spell(root, quality) {{
    var names = useFlats() ? FLAT : SHARP;
    return names[((root - capo) % 12 + 12) % 12] + (quality || '');
  }}

  function shapeKey() {{
    var k = parseChord(SOUNDING);
    if (!k) return SOUNDING;
    return spell(k.root, k.q);
  }}

  function applyCapo() {{
    document.querySelectorAll('.chord[data-root]').forEach(function (el) {{
      el.textContent = spell(parseInt(el.dataset.root, 10), el.dataset.q);
    }});
    document.getElementById('capoval').textContent = String(capo);
    var keyFact = document.getElementById('keyfact');
    if (keyFact) keyFact.textContent = shapeKey();
    var capoFact = document.getElementById('capofact');
    if (capoFact) capoFact.parentElement.style.display = capo ? '' : 'none';
    if (capoFact) capoFact.textContent = String(capo);
    var soundFact = document.getElementById('soundfact');
    if (soundFact) {{
      soundFact.parentElement.style.display = capo ? '' : 'none';
      soundFact.textContent = SOUNDING;
    }}
    refreshNow();
    if (!suggest.hidden) showSuggestions(audio.currentTime || 0);
    // Placement views carry chords too -- both the palette of what can be
    // placed and everything already placed have to follow the shapes.
    if (window.__refreshPlacers) window.__refreshPlacers();
  }}

  function setCapo(value) {{
    capo = ((value % 12) + 12) % 12;
    document.getElementById('shapes').value = '';
    applyCapo();
  }}

  // Capo needed so a song sounds right while playing a chosen shape family.
  // Minor keys map to the family's relative minor (A minor -> Em in the G
  // family), which is why this isn't just a subtraction.
  function capoForShape(shape) {{
    var key = parseChord(SOUNDING), fam = parseChord(shape);
    if (!key || !fam) return 0;
    var root = fam.root;
    if (key.q === 'm') root = (root - 3 + 12) % 12;
    return ((key.root - root) % 12 + 12) % 12;
  }}

  var tabs = document.querySelectorAll('.tabs button');
  tabs.forEach(function (tab) {{
    tab.addEventListener('click', function () {{
      tabs.forEach(function (other) {{
        var on = other === tab;
        other.setAttribute('aria-selected', on ? 'true' : 'false');
        document.getElementById(other.dataset.view).hidden = !on;
      }});
      if (tab.dataset.view === 'grid') scrollToCurrent(true);
    }});
  }});

  var audio = document.getElementById('audio');
  var rows = Array.prototype.slice.call(document.querySelectorAll('.row'));
  var playBtn = document.getElementById('play');
  var seek = document.getElementById('seek');
  var curEl = document.getElementById('cur');
  var durEl = document.getElementById('dur');
  var nowChord = document.getElementById('nowchord');
  var followBtn = document.getElementById('follow');
  var suggest = document.getElementById('suggest');
  var opts = document.getElementById('opts');
  var suggestAt = document.getElementById('suggestat');
  var follow = true;
  var currentRow = null;
  var seeking = false;

  // Row extents are read once; re-reading them per frame is the easiest way to
  // make playback janky on a long song.
  var extents = rows.map(function (row) {{
    return {{
      el: row,
      start: parseFloat(row.dataset.start),
      end: parseFloat(row.dataset.end),
      cells: Array.prototype.slice.call(row.querySelectorAll('.cell')).map(function (c) {{
        return {{ el: c, start: parseFloat(c.dataset.t), end: parseFloat(c.dataset.end) }};
      }}),
      head: row.querySelector('.playhead')
    }};
  }});

  function fmt(t) {{
    if (!isFinite(t)) return '0:00';
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }}

  // Binary search: linear scanning is fine for 100 rows but not for a long
  // track at 60fps.
  function findIndex(list, t) {{
    var lo = 0, hi = list.length - 1, best = -1;
    while (lo <= hi) {{
      var mid = (lo + hi) >> 1;
      if (list[mid].start <= t) {{ best = mid; lo = mid + 1; }} else hi = mid - 1;
    }}
    return best;
  }}

  function chordAt(t) {{
    var lo = 0, hi = TIMELINE.length - 1, best = null;
    while (lo <= hi) {{
      var mid = (lo + hi) >> 1;
      if (TIMELINE[mid].t <= t) {{ best = TIMELINE[mid]; lo = mid + 1; }} else hi = mid - 1;
    }}
    return best;
  }}

  function refreshNow(t) {{
    if (t === undefined) t = audio.currentTime || 0;
    var chord = chordAt(t);
    nowChord.textContent =
      chord && chord.r !== null ? spell(chord.r, chord.q) : (chord ? chord.c : '—');
  }}

  function scrollToCurrent(force) {{
    if (currentRow && (follow || force)) {{
      currentRow.el.scrollIntoView({{ block: 'center', behavior: force ? 'auto' : 'smooth' }});
    }}
  }}

  function update() {{
    var t = audio.currentTime || 0;
    if (!seeking) seek.value = String(Math.round((t / (audio.duration || 1)) * 1000));
    curEl.textContent = fmt(t);

    refreshNow(t);
    if (window.__lyricFollow) window.__lyricFollow(t);

    var index = findIndex(extents, t);
    var row = index >= 0 ? extents[index] : null;
    if (row !== currentRow) {{
      if (currentRow) currentRow.el.classList.remove('current');
      if (row) row.el.classList.add('current');
      currentRow = row;
      scrollToCurrent(false);
    }}
    if (row) {{
      // Playhead rides across the highlighted measure.
      var span = row.end - row.start;
      var pct = span > 0 ? Math.min(Math.max((t - row.start) / span, 0), 1) : 0;
      row.head.style.left = (pct * 100) + '%';
      for (var i = 0; i < row.cells.length; i++) {{
        var c = row.cells[i];
        c.el.classList.toggle('now', t >= c.start && t < c.end);
      }}
    }}
    if (!audio.paused) requestAnimationFrame(update);
  }}

  function showSuggestions(t) {{
    if (!OPTIONS.length) return;
    var lo = 0, hi = OPTIONS.length - 1, best = null;
    while (lo <= hi) {{
      var mid = (lo + hi) >> 1;
      if (OPTIONS[mid].t <= t) {{ best = OPTIONS[mid]; lo = mid + 1; }} else hi = mid - 1;
    }}
    if (!best) best = OPTIONS[0];
    var detected = chordAt(t);
    suggestAt.textContent = fmt(best.t) + '  ·  key of ' + shapeKey();
    // Options are stored in sounding pitch, so they re-spell with the capo too.
    opts.innerHTML = best.o.map(function (pair) {{
      var parsed = parseChord(pair[0]);
      var shown = parsed ? spell(parsed.root, parsed.q) : pair[0];
      var isCur = detected && detected.r !== null &&
        parsed && parsed.root === detected.r && parsed.q === detected.q;
      return '<div class="opt' + (isCur ? ' cur' : '') + '"><span class="c">' +
        shown + '</span><span class="f">' + Math.round(pair[1] * 100) + '%' +
        (isCur ? ' · playing' : '') + '</span></div>';
    }}).join('');
    suggest.hidden = false;
  }}

  playBtn.addEventListener('click', function () {{
    if (audio.paused) audio.play(); else audio.pause();
  }});
  audio.addEventListener('play', function () {{
    playBtn.innerHTML = '&#10073;&#10073;';
    suggest.hidden = true;
    requestAnimationFrame(update);
  }});
  audio.addEventListener('pause', function () {{
    playBtn.innerHTML = '&#9654;';
    showSuggestions(audio.currentTime || 0);
  }});
  audio.addEventListener('timeupdate', function () {{ if (audio.paused) update(); }});
  audio.addEventListener('loadedmetadata', function () {{
    durEl.textContent = fmt(audio.duration);
  }});
  audio.addEventListener('error', function () {{
    document.getElementById('loadaudio').hidden = false;
  }});

  document.getElementById('audiofile').addEventListener('change', function (e) {{
    var file = e.target.files && e.target.files[0];
    if (file) {{
      audio.src = URL.createObjectURL(file);
      document.getElementById('loadaudio').hidden = true;
    }}
  }});

  seek.addEventListener('input', function () {{ seeking = true; }});
  seek.addEventListener('change', function () {{
    if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration;
    seeking = false;
    update();
  }});
  document.getElementById('back').addEventListener('click', function () {{
    audio.currentTime = Math.max(0, (audio.currentTime || 0) - 5); update();
  }});
  document.getElementById('fwd').addEventListener('click', function () {{
    audio.currentTime = Math.min(audio.duration || 0, (audio.currentTime || 0) + 5); update();
  }});
  document.getElementById('shapes').addEventListener('change', function (event) {{
    var shape = event.target.value;
    if (!shape) {{ capo = 0; applyCapo(); return; }}
    capo = capoForShape(shape);
    applyCapo();
    event.target.value = shape;  // applyCapo's siblings must not clear this
  }});
  document.getElementById('capoup').addEventListener('click', function () {{
    setCapo(capo + 1);
  }});
  document.getElementById('capodown').addEventListener('click', function () {{
    setCapo(capo - 1);
  }});

  followBtn.addEventListener('click', function () {{
    follow = !follow;
    followBtn.setAttribute('aria-pressed', follow ? 'true' : 'false');
    followBtn.style.opacity = follow ? '1' : '.5';
    if (follow) scrollToCurrent(true);
  }});

  // Click a measure to jump there; click a specific beat to land on that beat.
  extents.forEach(function (row) {{
    row.el.addEventListener('click', function (event) {{
      var cell = event.target.closest ? event.target.closest('.cell') : null;
      audio.currentTime = cell ? parseFloat(cell.dataset.t) : row.start;
      update();
      if (audio.paused) showSuggestions(audio.currentTime);
    }});
  }});

  document.addEventListener('keydown', function (event) {{
    if (event.target.tagName === 'INPUT') return;
    if (event.code === 'Space') {{
      event.preventDefault();
      if (audio.paused) audio.play(); else audio.pause();
    }} else if (event.code === 'ArrowLeft') {{
      audio.currentTime = Math.max(0, (audio.currentTime || 0) - 5); update();
    }} else if (event.code === 'ArrowRight') {{
      audio.currentTime = Math.min(audio.duration || 0, (audio.currentTime || 0) + 5); update();
    }}
  }});

  applyCapo();
  update();
{navjs}
}})();
</script>
{chordprojs}
</body>
</html>
"""


def _chordpro_bundle() -> str:
    """Ledgr's ChordPro parser/editor, inlined so the page stays self-contained.

    Missing is not fatal: the chart and playback don't need it, only the lyrics
    editor does. A page without it simply won't offer editing, which beats
    refusing to render a chart because a vendored file wasn't built.
    """
    path = Path(__file__).parent / "static" / "chordpro.js"
    if not path.exists():
        return ""
    # An argument to .format(), never itself a format string -- the bundle is
    # full of braces and doubling them would corrupt the JavaScript.
    return f"<script>\n{path.read_text()}\n</script>"


def write_html(analysis: Analysis, path: Path, **kwargs) -> Path:
    path.write_text(render_html(analysis, **kwargs))
    return path
