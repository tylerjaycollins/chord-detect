// Vendored from Ledgr — DO NOT EDIT HERE.
// Ledgr commit 86b8ae8d600b (2026-08-02T18:41:39-05:00)
// Rebuild: uv run python scripts/vendor_chordpro.py
var ChordPro = (() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

  // ../../../../private/var/folders/4_/bvh5jxkx6sl_s54g4fv9g1x00000gn/T/tmpzs_8klys/entry.ts
  var entry_exports = {};
  __export(entry_exports, {
    CHORDPRO_FORMAT: () => CHORDPRO_FORMAT,
    COMMON_CHORDS: () => COMMON_CHORDS,
    addKeyChange: () => addKeyChange,
    addLine: () => addLine,
    addRepeat: () => addRepeat,
    addSection: () => addSection,
    chartToLyricsMarkdown: () => chartToLyricsMarkdown,
    chartToLyricsText: () => chartToLyricsText,
    classifySection: () => classifySection,
    editLineToPairs: () => editLineToPairs,
    keyOfCapo: () => keyOfCapo,
    lyricPairs: () => lyricPairs,
    lyricsToSections: () => lyricsToSections,
    mergeLyricsIntoChart: () => mergeLyricsIntoChart,
    moveSection: () => moveSection,
    pairsToEditLine: () => pairsToEditLine,
    parseChordPro: () => parseChordPro,
    removeLine: () => removeLine,
    removeSection: () => removeSection,
    semitonesBetween: () => semitonesBetween,
    serializeChordChart: () => serializeChordChart,
    setChordAt: () => setChordAt,
    setLine: () => setLine,
    setLineText: () => setLineText,
    setSectionLabel: () => setSectionLabel,
    transposeChartChords: () => transposeChartChords,
    transposeChord: () => transposeChord,
    transposeNote: () => transposeNote,
    updateLine: () => updateLine,
    updateMeta: () => updateMeta,
    updateSection: () => updateSection
  });

  // ../Ledgr/src/lib/chordpro/types.ts
  var CHORDPRO_FORMAT = "chordpro";
  function classifySection(label) {
    const l = label.trim().toLowerCase();
    if (l.startsWith("pre")) return "prechorus";
    if (l.startsWith("intro")) return "intro";
    if (l.startsWith("verse")) return "verse";
    if (l.startsWith("chorus")) return "chorus";
    if (l.startsWith("bridge")) return "bridge";
    if (l.startsWith("tag")) return "tag";
    if (l.startsWith("instrumental") || l.startsWith("inst")) return "instrumental";
    if (l.startsWith("turn")) return "turn";
    if (l.startsWith("end") || l.startsWith("outro")) return "end";
    return "other";
  }

  // ../Ledgr/src/lib/chordpro/parse.ts
  var DIRECTIVE_RE = /^\{\s*([a-zA-Z_]+)\s*(?::\s*([\s\S]*?))?\s*\}$/;
  var INLINE_CHORD_RE = /\[([^\]]*)\]/g;
  var ENV_OPEN = {
    soc: "Chorus",
    start_of_chorus: "Chorus",
    sov: "Verse",
    start_of_verse: "Verse",
    sob: "Bridge",
    start_of_bridge: "Bridge"
  };
  var ENV_CLOSE = /* @__PURE__ */ new Set([
    "eoc",
    "end_of_chorus",
    "eov",
    "end_of_verse",
    "eob",
    "end_of_bridge"
  ]);
  function parseLyric(line) {
    const matches = [];
    let m;
    INLINE_CHORD_RE.lastIndex = 0;
    while (m = INLINE_CHORD_RE.exec(line)) {
      matches.push({ chord: m[1], start: m.index, end: m.index + m[0].length });
    }
    if (matches.length === 0) return [{ chord: null, text: line }];
    const pairs = [];
    if (matches[0].start > 0) {
      pairs.push({ chord: null, text: line.slice(0, matches[0].start) });
    }
    for (let i = 0; i < matches.length; i++) {
      const textEnd = i + 1 < matches.length ? matches[i + 1].start : line.length;
      pairs.push({ chord: matches[i].chord, text: line.slice(matches[i].end, textEnd) });
    }
    return pairs;
  }
  function parseBars(line) {
    return line.split("|").map((bar) => bar.trim()).filter((bar) => bar.length > 0).map((bar) => bar.split(/\s+/));
  }
  function parseChordPro(text) {
    const meta = {};
    const sections = [];
    let current = null;
    let pendingBreak = false;
    let pendingPageBreak = false;
    const openSection = (label, ref = false) => {
      if (current) sections.push(current);
      current = { label, kind: classifySection(label), ref, lines: [] };
      if (pendingBreak) {
        current.breakBefore = true;
        pendingBreak = false;
      }
      if (pendingPageBreak) {
        current.pageBreakBefore = true;
        pendingPageBreak = false;
      }
      if (ref) {
        sections.push(current);
        current = null;
      }
    };
    const ensureSection = () => {
      if (!current) current = { label: "", kind: "other", ref: false, lines: [] };
    };
    const pushLine = (line) => {
      ensureSection();
      current.lines.push(line);
    };
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (line === "") continue;
      if (line.toUpperCase() === "COLUMN_BREAK") {
        pendingBreak = true;
        continue;
      }
      if (line.toUpperCase() === "PAGE_BREAK") {
        pendingPageBreak = true;
        continue;
      }
      const kc = /^(TRANSPOSE|REDEFINE)\s+KEY\s*([+-]?\d+)$/i.exec(line);
      if (kc) {
        pushLine({
          kind: "keychange",
          mode: kc[1].toLowerCase() === "redefine" ? "redefine" : "transpose",
          semitones: parseInt(kc[2], 10) || 0
        });
        continue;
      }
      const dir = DIRECTIVE_RE.exec(line);
      if (dir) {
        const name = dir[1].toLowerCase();
        const value = (dir[2] ?? "").trim();
        switch (name) {
          case "title":
          case "t":
            meta.title = value;
            break;
          case "subtitle":
          case "artist":
          case "st":
            meta.artist = value;
            break;
          case "key":
            meta.key = value;
            break;
          case "capo": {
            const n = parseInt(value, 10);
            if (!Number.isNaN(n)) meta.capo = n;
            break;
          }
          case "tempo": {
            const n = parseInt(value, 10);
            if (!Number.isNaN(n)) meta.tempo = n;
            break;
          }
          case "time":
            meta.time = value;
            break;
          case "arrangement":
          case "sequence":
            meta.arrangement = value;
            break;
          case "column_break":
          case "colb":
            pendingBreak = true;
            break;
          case "page_break":
          case "new_page":
            pendingPageBreak = true;
            break;
          case "transpose_key":
            pushLine({ kind: "keychange", mode: "transpose", semitones: parseInt(value, 10) || 0 });
            break;
          case "redefine_key":
            pushLine({ kind: "keychange", mode: "redefine", semitones: parseInt(value, 10) || 0 });
            break;
          case "ccli":
            meta.ccli = value;
            break;
          case "copyright":
          case "footer":
            meta.copyright = value;
            break;
          case "section":
            openSection(value);
            break;
          case "repeat":
            openSection(value, true);
            break;
          case "comment":
          case "c":
            pushLine({ kind: "comment", text: value });
            break;
          default:
            if (name in ENV_OPEN) openSection(value || ENV_OPEN[name]);
            else if (ENV_CLOSE.has(name)) {
            }
            break;
        }
        continue;
      }
      if (line.includes("|")) pushLine({ kind: "bars", bars: parseBars(line) });
      else pushLine({ kind: "lyric", pairs: parseLyric(line) });
    }
    if (current) sections.push(current);
    return { meta, sections };
  }
  function lyricToText(pairs) {
    return pairs.map((p) => (p.chord ? `[${p.chord}]` : "") + p.text).join("");
  }
  function barsToText(bars) {
    return "| " + bars.map((bar) => bar.join(" ")).join(" | ") + " |";
  }
  var META_ORDER = [
    ["title", "title"],
    ["artist", "artist"],
    ["key", "key"],
    ["capo", "capo"],
    ["tempo", "tempo"],
    ["time", "time"],
    ["arrangement", "arrangement"],
    ["ccli", "ccli"],
    ["copyright", "copyright"]
  ];
  function serializeChordChart(chart) {
    const out = [];
    for (const [field, name] of META_ORDER) {
      const v = chart.meta[field];
      if (v !== void 0 && v !== "") out.push(`{${name}: ${v}}`);
    }
    for (const section of chart.sections) {
      if (out.length > 0) out.push("");
      if (section.pageBreakBefore) out.push("{page_break}");
      if (section.breakBefore) out.push("{column_break}");
      if (section.ref) {
        out.push(`{repeat: ${section.label}}`);
        continue;
      }
      out.push(`{section: ${section.label}}`);
      for (const line of section.lines) {
        if (line.kind === "lyric") out.push(lyricToText(line.pairs));
        else if (line.kind === "bars") out.push(barsToText(line.bars));
        else if (line.kind === "keychange") {
          const dir = line.mode === "redefine" ? "redefine_key" : "transpose_key";
          out.push(`{${dir}: ${line.semitones}}`);
        } else out.push(`{c: ${line.text}}`);
      }
    }
    return out.join("\n");
  }

  // ../Ledgr/src/lib/chordpro/transpose.ts
  var SHARP_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  var FLAT_NOTES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"];
  var PITCH_CLASS = {};
  SHARP_NOTES.forEach((n, i) => PITCH_CLASS[n] = i);
  FLAT_NOTES.forEach((n, i) => PITCH_CLASS[n] = i);
  function mod12(n) {
    return (n % 12 + 12) % 12;
  }
  function transposeNote(note, semitones, preferFlats = false) {
    const pc = PITCH_CLASS[note];
    if (pc === void 0) return note;
    const next = mod12(pc + semitones);
    return (preferFlats ? FLAT_NOTES : SHARP_NOTES)[next];
  }
  var NOTE_RE = /^([A-G][#b]?)(.*)$/;
  function transposeSegment(seg, semitones, preferFlats) {
    const m = NOTE_RE.exec(seg);
    if (!m) return seg;
    return transposeNote(m[1], semitones, preferFlats) + m[2];
  }
  function transposeChord(chord, semitones, preferFlats = false) {
    if (semitones % 12 === 0) return chord;
    if (chord === "/" || chord === "|" || chord.trim() === "") return chord;
    const slash = chord.indexOf("/");
    if (slash === -1) return transposeSegment(chord, semitones, preferFlats);
    const main = chord.slice(0, slash);
    const bass = chord.slice(slash + 1);
    return transposeSegment(main, semitones, preferFlats) + "/" + transposeSegment(bass, semitones, preferFlats);
  }
  function semitonesBetween(fromKey, toKey) {
    const a = NOTE_RE.exec(fromKey);
    const b = NOTE_RE.exec(toKey);
    if (!a || !b) return 0;
    return mod12((PITCH_CLASS[b[1]] ?? 0) - (PITCH_CLASS[a[1]] ?? 0));
  }
  function keyOfCapo(soundingKey, capo, preferFlats = false) {
    return transposeNote(soundingKey, -capo, preferFlats);
  }
  function transposeChartChords(chart, semitones, preferFlats = false) {
    if (semitones % 12 === 0) return chart;
    const tx = (c) => transposeChord(c, semitones, preferFlats);
    return {
      ...chart,
      sections: chart.sections.map((s) => ({
        ...s,
        lines: s.lines.map((l) => {
          if (l.kind === "lyric") {
            return { ...l, pairs: l.pairs.map((p) => ({ ...p, chord: p.chord ? tx(p.chord) : null })) };
          }
          if (l.kind === "bars") {
            return { ...l, bars: l.bars.map((bar) => bar.map(tx)) };
          }
          return l;
        })
      }))
    };
  }

  // ../Ledgr/src/lib/chordpro/lyrics.ts
  var SECTION_KEYWORDS = [
    { re: /^pre[\s-]?chorus\b/i, kind: "prechorus" },
    { re: /^chorus\b/i, kind: "chorus" },
    { re: /^verse\b/i, kind: "verse" },
    { re: /^bridge\b/i, kind: "bridge" },
    { re: /^intro\b/i, kind: "intro" },
    { re: /^(outro|ending|end)\b/i, kind: "end" },
    { re: /^tag\b/i, kind: "tag" },
    { re: /^(instrumental|interlude)\b/i, kind: "instrumental" },
    { re: /^(turn(around)?|vamp)\b/i, kind: "turn" },
    { re: /^(refrain|hook)\b/i, kind: "chorus" }
  ];
  function detectHeader(line) {
    const bracketed = /^\[.+\]$/.test(line.trim());
    const cleaned = line.replace(/^\[/, "").replace(/\]$/, "").replace(/:\s*$/, "").trim();
    if (!cleaned || cleaned.length > 24) return null;
    const kind = SECTION_KEYWORDS.find((k) => k.re.test(cleaned))?.kind;
    if (kind) return { label: cleaned.toUpperCase(), kind };
    if (bracketed) return { label: cleaned.toUpperCase(), kind: "other" };
    return null;
  }
  function lyricsToSections(lyrics) {
    const sections = [];
    const seen = /* @__PURE__ */ new Set();
    let current = null;
    let skipping = false;
    const key = (label) => label.trim().toLowerCase();
    for (const raw of lyrics.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const header = detectHeader(line);
      if (header) {
        if (seen.has(key(header.label))) {
          sections.push({ label: header.label, kind: header.kind, ref: true, lines: [] });
          current = null;
          skipping = true;
        } else {
          seen.add(key(header.label));
          current = { label: header.label, kind: header.kind, ref: false, lines: [] };
          sections.push(current);
          skipping = false;
        }
        continue;
      }
      if (skipping) continue;
      if (!current) {
        current = { label: "VERSE 1", kind: "verse", ref: false, lines: [] };
        seen.add(key("VERSE 1"));
        sections.push(current);
      }
      current.lines.push({ kind: "lyric", pairs: [{ chord: null, text: line }] });
    }
    return sections;
  }
  var lyricTextOf = (l) => l.pairs.map((p) => p.text).join("");
  function chartToLyricsText(chart) {
    const linesFor = (s) => s.ref ? chart.sections.find((o) => !o.ref && o.label.trim().toLowerCase() === s.label.trim().toLowerCase())?.lines ?? [] : s.lines;
    return chart.sections.map((s) => {
      const header = detectHeader(s.label) ? s.label : `[${s.label}]`;
      const lyrics = linesFor(s).filter((l) => l.kind === "lyric").map(lyricTextOf).join("\n");
      return lyrics ? `${header}
${lyrics}` : header;
    }).join("\n\n");
  }
  function mergeLyricsIntoChart(chart, lyrics) {
    const parsed = lyricsToSections(lyrics);
    const oldByLabel = /* @__PURE__ */ new Map();
    for (const s of chart.sections) {
      const k = s.label.trim().toLowerCase();
      (oldByLabel.get(k) ?? oldByLabel.set(k, []).get(k)).push(s);
    }
    const sections = parsed.map((ns) => {
      if (ns.ref) return ns;
      const old = oldByLabel.get(ns.label.trim().toLowerCase())?.shift();
      if (!old) return ns;
      const oldLyrics = old.lines.filter((l) => l.kind === "lyric");
      const used = /* @__PURE__ */ new Set();
      const lines = ns.lines.map((nl) => {
        if (nl.kind !== "lyric") return nl;
        const text = lyricTextOf(nl);
        const i = oldLyrics.findIndex((ol, idx) => !used.has(idx) && lyricTextOf(ol) === text);
        if (i >= 0) {
          used.add(i);
          return oldLyrics[i];
        }
        return nl;
      });
      const extras = old.lines.filter((l) => l.kind !== "lyric");
      return { ...ns, breakBefore: old.breakBefore, pageBreakBefore: old.pageBreakBefore, lines: [...lines, ...extras] };
    });
    return { meta: chart.meta, sections };
  }
  function chartToLyricsMarkdown(chart) {
    const head = chart.meta.title?.trim() ? `# ${chart.meta.title.trim()}

` : "";
    const linesFor = (s) => {
      if (!s.ref) return s.lines;
      const orig = chart.sections.find((o) => !o.ref && o.label.trim().toLowerCase() === s.label.trim().toLowerCase());
      return orig ? orig.lines : s.lines;
    };
    const body = chart.sections.map((s) => {
      const lyrics = linesFor(s).filter((l) => l.kind === "lyric").map((l) => l.pairs.map((p) => p.text).join("")).join("\n");
      const header = `## ${s.label.trim() || "Section"}`;
      return lyrics ? `${header}

${lyrics}` : header;
    }).join("\n\n");
    return `${head}${body}
`;
  }

  // ../Ledgr/src/components/chord-editor/chordpro-edit.ts
  var COMMON_CHORDS = [
    "G",
    "C",
    "D",
    "Em",
    "Am",
    "A",
    "E",
    "F",
    "Bm",
    "B",
    "G/B",
    "D/F#",
    "C2",
    "Em7",
    "Dsus",
    "Gsus",
    "Cmaj7",
    "Asus",
    "Esus",
    "Fmaj7"
  ];
  function pairsToEditLine(pairs) {
    let text = "";
    const chords = [];
    for (const p of pairs) {
      if (p.chord) {
        const leading = p.text === "" && text === "";
        chords.push({ at: leading ? -1 : text.length, chord: p.chord });
      }
      text += p.text;
    }
    return { text, chords };
  }
  function editLineToPairs(line) {
    const leading = line.chords.filter((c) => c.at < 0).sort((a, b) => a.at - b.at);
    const positioned = line.chords.filter((c) => c.at >= 0).sort((a, b) => a.at - b.at);
    const pairs = leading.map((c) => ({ chord: c.chord, text: "" }));
    let cursor = 0;
    let pending = null;
    for (const c of positioned) {
      const at = Math.min(c.at, line.text.length);
      pairs.push({ chord: pending, text: line.text.slice(cursor, at) });
      cursor = at;
      pending = c.chord;
    }
    pairs.push({ chord: pending, text: line.text.slice(cursor) });
    const out = pairs.filter((p) => !(p.chord === null && p.text === ""));
    return out.length ? out : [{ chord: null, text: "" }];
  }
  function setChordAt(line, at, chord) {
    const chords = line.chords.filter((c) => c.at !== at);
    if (chord) chords.push({ at, chord });
    return { text: line.text, chords };
  }
  function setLineText(line, text) {
    const chords = line.chords.filter((c) => c.at <= text.length).map((c) => ({ ...c, at: Math.min(c.at, text.length) }));
    return { text, chords };
  }
  function lyricPairs(l) {
    return l.kind === "lyric" ? l.pairs : [];
  }
  function updateMeta(chart, patch) {
    return { ...chart, meta: { ...chart.meta, ...patch } };
  }
  function updateSection(chart, i, patch) {
    const sections = chart.sections.map((s, idx) => idx === i ? { ...s, ...patch } : s);
    return { ...chart, sections };
  }
  function moveSection(chart, i, dir) {
    const j = i + dir;
    if (j < 0 || j >= chart.sections.length) return chart;
    const sections = [...chart.sections];
    [sections[i], sections[j]] = [sections[j], sections[i]];
    return { ...chart, sections };
  }
  function removeSection(chart, i) {
    return { ...chart, sections: chart.sections.filter((_, idx) => idx !== i) };
  }
  function addSection(chart, label) {
    const section = {
      label,
      kind: classifySection(label),
      ref: false,
      lines: [{ kind: "lyric", pairs: [{ chord: null, text: "" }] }]
    };
    return { ...chart, sections: [...chart.sections, section] };
  }
  function addRepeat(chart, label) {
    const section = { label, kind: classifySection(label), ref: true, lines: [] };
    return { ...chart, sections: [...chart.sections, section] };
  }
  function setSectionLabel(chart, i, label) {
    return updateSection(chart, i, { label, kind: classifySection(label) });
  }
  function setLine(chart, si, li, line) {
    const section = chart.sections[si];
    if (!section) return chart;
    const lines = section.lines.map(
      (l, idx) => idx === li ? { kind: "lyric", pairs: editLineToPairs(line) } : l
    );
    return updateSection(chart, si, { lines });
  }
  function addLine(chart, si) {
    const section = chart.sections[si];
    if (!section) return chart;
    const lines = [...section.lines, { kind: "lyric", pairs: [{ chord: null, text: "" }] }];
    return updateSection(chart, si, { lines });
  }
  function removeLine(chart, si, li) {
    const section = chart.sections[si];
    if (!section) return chart;
    return updateSection(chart, si, { lines: section.lines.filter((_, idx) => idx !== li) });
  }
  function updateLine(chart, si, li, line) {
    const section = chart.sections[si];
    if (!section) return chart;
    return updateSection(chart, si, { lines: section.lines.map((l, idx) => idx === li ? line : l) });
  }
  function addKeyChange(chart, si) {
    const section = chart.sections[si];
    if (!section) return chart;
    const lines = [
      ...section.lines,
      { kind: "keychange", mode: "transpose", semitones: 2 }
    ];
    return updateSection(chart, si, { lines });
  }
  return __toCommonJS(entry_exports);
})();
