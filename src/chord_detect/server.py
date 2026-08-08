"""Local web app: pick a song, analyze it, play along with the chart.

Runs entirely on your machine -- audio never leaves it, and there's no per-song
cost. Analysis takes ten to twenty seconds, so uploads run on a worker thread
and the browser polls for progress rather than staring at a dead request.

Results are cached by file content hash, so reopening a song you've already
analyzed is instant even if you renamed or moved it.

    chord-detect-app          # opens http://127.0.0.1:8765
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .chords import ChordRecognizerConfig
from .chordpro import to_chordpro
from .pipeline import analyze
from .render import RENDER_VERSION, render_html
from .theory import SIMPLE_QUALITY_NAMES, choose_shapes

CACHE = Path.home() / ".cache" / "chord-detect" / "songs"
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aiff", ".aif"}

app = FastAPI(title="chord-detect")


@dataclass
class Job:
    id: str
    name: str
    stage: str = "Queued"
    fraction: float = 0.0
    done: bool = False
    error: str | None = None
    key: str = ""
    tempo: float = 0.0
    meter: int | None = None
    capo: int = 0
    shape: str = ""
    options: dict = field(default_factory=dict)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Analysis worker
# --------------------------------------------------------------------------


def _cache_key(digest: str, options: dict) -> str:
    """Cache identity: file content, settings, *and* the renderer version.

    The version matters: without it, editing the chart template or player
    silently serves charts built by older code, because the audio and options
    haven't changed. That cost a confusing debugging detour once already.
    """
    stamp = json.dumps(options, sort_keys=True)
    return hashlib.sha256(
        f"{digest}:{stamp}:v{RENDER_VERSION}".encode()
    ).hexdigest()[:16]


def _run(job: Job, audio_path: Path, out_dir: Path, options: dict) -> None:
    try:

        def report(stage: str, fraction: float) -> None:
            job.stage = stage
            job.fraction = fraction

        config = ChordRecognizerConfig(
            self_transition=options["sensitivity"],
            quality_names=(
                SIMPLE_QUALITY_NAMES if options["vocabulary"] == "simple" else None
            ),
        )
        analysis = analyze(
            audio_path,
            config=config,
            beat_sync=not options["free_time"],
            suggestions=True,
            model=options.get("model", "cnnlstm"),
            progress=report,
        )

        original_key = analysis.key

        # A shape preference decides the capo, so it wins over an explicit one.
        capo = options["capo"]
        chosen_shape = None
        if options.get("shapes"):
            picked = choose_shapes(
                analysis.key,
                [s.strip() for s in options["shapes"].split(",") if s.strip()],
            )
            if picked:
                capo, chosen_shape = picked

        shift = options["transpose"] - capo
        if shift:
            analysis = analysis.transposed(shift)
        job.capo = capo
        job.shape = chosen_shape or ""

        job.key = analysis.key
        job.tempo = analysis.tempo
        job.meter = analysis.meter

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "chart.html").write_text(
            render_html(
                analysis,
                title=job.name,
                capo=capo,
                source_key=original_key if shift else None,
                # Served by this app, so an absolute route is stable regardless
                # of where the cache directory happens to live.
                audio_src=f"/audio/{job.id}",
                shift=shift,
                shapes=chosen_shape,
                nav=True,
            )
        )
        (out_dir / "song.cho").write_text(
            to_chordpro(analysis, title=job.name, capo=capo,
                        measures_per_section=16)
        )
        (out_dir / "analysis.json").write_text(analysis.to_json())
        job.stage = "Done"
        job.fraction = 1.0
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        job.error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        job.done = True


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX


@app.post("/analyze")
async def start(
    file: UploadFile = File(...),
    capo: int = Form(0),
    transpose: int = Form(0),
    sensitivity: float = Form(0.92),
    vocabulary: str = Form("simple"),
    free_time: bool = Form(False),
    shapes: str = Form(""),
    model: str = Form("cnnlstm"),
) -> JSONResponse:
    name = Path(file.filename or "song").stem
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise HTTPException(
            400, f"{suffix or 'that file'} isn't an audio format I can read."
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "That file was empty.")

    options = {
        "capo": capo,
        "transpose": transpose,
        "sensitivity": sensitivity,
        "vocabulary": vocabulary,
        "free_time": free_time,
        "shapes": shapes,
        "model": model,
    }
    digest = hashlib.sha256(payload).hexdigest()
    job_id = _cache_key(digest, options)
    out_dir = CACHE / job_id

    audio_path = out_dir / f"audio{suffix}"
    if not audio_path.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(payload)
    (out_dir / "name.txt").write_text(name)

    # Already analyzed with these exact settings -- skip straight to the chart.
    if (out_dir / "chart.html").exists():
        job = Job(id=job_id, name=name, stage="Done", fraction=1.0, done=True)
        with JOBS_LOCK:
            JOBS[job_id] = job
        return JSONResponse({"id": job_id, "cached": True})

    job = Job(id=job_id, name=name, options=options)
    with JOBS_LOCK:
        JOBS[job_id] = job

    threading.Thread(
        target=_run, args=(job, audio_path, out_dir, options), daemon=True
    ).start()
    return JSONResponse({"id": job_id, "cached": False})


@app.get("/jobs/{job_id}")
def status(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if job is None:
        # Server restarted but the chart survived on disk.
        if (CACHE / job_id / "chart.html").exists():
            return JSONResponse({"stage": "Done", "fraction": 1.0, "done": True})
        raise HTTPException(404, "No such job")
    return JSONResponse(
        {
            "stage": job.stage,
            "fraction": job.fraction,
            "done": job.done,
            "error": job.error,
            "key": job.key,
            "tempo": job.tempo,
            "meter": job.meter,
            "capo": job.capo,
            "shape": job.shape,
        }
    )


@app.get("/chart/{job_id}", response_class=HTMLResponse)
def chart(job_id: str) -> str:
    path = CACHE / job_id / "chart.html"
    if not path.exists():
        raise HTTPException(404, "That chart hasn't been built yet.")
    return path.read_text()


@app.get("/audio/{job_id}")
def audio(job_id: str) -> FileResponse:
    for candidate in (CACHE / job_id).glob("audio.*"):
        return FileResponse(candidate)
    raise HTTPException(404, "Audio not found")


@app.get("/chordpro/{job_id}")
def chordpro(job_id: str) -> FileResponse:
    path = CACHE / job_id / "song.cho"
    if not path.exists():
        raise HTTPException(404, "Not built yet")
    name = (CACHE / job_id / "name.txt").read_text() if (
        CACHE / job_id / "name.txt"
    ).exists() else "song"
    return FileResponse(path, filename=f"{name}.cho", media_type="text/plain")


@app.get("/library")
def library() -> JSONResponse:
    """Previously analyzed songs, newest first."""
    out = []
    if CACHE.exists():
        for folder in CACHE.iterdir():
            chart_file = folder / "chart.html"
            name_file = folder / "name.txt"
            if not chart_file.exists():
                continue
            out.append(
                {
                    "id": folder.name,
                    "name": name_file.read_text() if name_file.exists() else folder.name,
                    "when": chart_file.stat().st_mtime,
                }
            )
    out.sort(key=lambda row: row["when"], reverse=True)
    return JSONResponse(out[:40])


@app.post("/library/{job_id}/name")
def rename(job_id: str, name: str = Form(...)) -> JSONResponse:
    """Rename a song. The chart reads its title from here at load time, so a
    rename takes effect without re-rendering the whole page."""
    folder = CACHE / job_id
    if not folder.exists():
        raise HTTPException(404, "No such song")
    clean = name.strip()[:120]
    if not clean:
        raise HTTPException(400, "Name can't be empty")
    (folder / "name.txt").write_text(clean)
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].name = clean
    return JSONResponse({"ok": True, "name": clean})


@app.delete("/library/{job_id}")
def forget(job_id: str) -> JSONResponse:
    folder = CACHE / job_id
    if folder.exists():
        shutil.rmtree(folder)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------
# Landing page
# --------------------------------------------------------------------------

_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>chord-detect</title>
<style>
  :root { --bg:#16202e; --panel:#1e2b3d; --line:#2b3a4f; --ink:#e8edf5;
          --dim:#8fa0b8; --gold:#e8c98a; --cell:#3a4657; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; background:var(--bg); color:var(--ink);
    font:16px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
    display:flex; align-items:flex-start; justify-content:center; padding:40px 20px; }
  .wrap { width:100%; max-width:680px; }
  h1 { font-size:1.6rem; margin:0 0 6px; }
  .sub { color:var(--dim); margin:0 0 26px; font-size:.92rem; }
  #drop { border:2px dashed var(--line); border-radius:14px; padding:44px 24px;
    text-align:center; background:var(--panel); transition:border-color .15s,background .15s; }
  #drop.over { border-color:var(--gold); background:#233247; }
  #drop p { margin:8px 0 0; color:var(--dim); font-size:.88rem; }
  .pick { display:inline-block; background:var(--gold); color:#14202e; font-weight:700;
    padding:11px 22px; border-radius:999px; cursor:pointer; }
  input[type=file] { display:none; }
  details { margin-top:18px; background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:12px 16px; }
  summary { cursor:pointer; color:var(--dim); font-size:.86rem; font-weight:600; }
  .opts { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
    gap:12px; margin-top:14px; }
  label.f { display:flex; flex-direction:column; gap:5px; font-size:.76rem;
    color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
  label.f input, label.f select { background:var(--cell); color:var(--ink);
    border:1px solid var(--line); border-radius:7px; padding:7px 9px; font-size:.9rem; }
  #working[hidden], #picker[hidden] { display:none; }
  #working { text-align:center; padding:48px 24px; background:var(--panel);
    border:1px solid var(--line); border-radius:14px; }
  .spinner { width:38px; height:38px; margin:0 auto 20px; border-radius:50%;
    border:3px solid var(--cell); border-top-color:var(--gold);
    animation:spin .9s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @media (prefers-reduced-motion:reduce) { .spinner { animation-duration:3s; } }
  .wname { font-size:1.15rem; font-weight:600; margin-bottom:6px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #stage { color:var(--dim); font-size:.9rem; }
  .bar { height:7px; background:var(--cell); border-radius:99px; overflow:hidden;
    margin:16px auto 0; max-width:420px; }
  .bar i { display:block; height:100%; width:0; background:var(--gold);
    transition:width .25s ease; }
  .found { margin-top:14px; color:var(--gold); font-weight:600; min-height:1.3em; }
  .back { margin-top:22px; background:none; border:1px solid var(--line);
    color:var(--dim); border-radius:8px; padding:8px 16px; cursor:pointer;
    font-size:.85rem; }
  .back:hover { color:var(--ink); }
  .err { color:#ff9d9d; }
  .rename { background:none; border:0; color:var(--dim); cursor:pointer;
    font-size:.8rem; }
  .rename:hover { color:var(--gold); }
  h2 { font-size:.78rem; text-transform:uppercase; letter-spacing:.06em;
    color:var(--dim); margin:30px 0 10px; }
  .song { display:flex; align-items:center; gap:12px; padding:11px 14px;
    background:var(--panel); border:1px solid var(--line); border-radius:9px;
    margin-bottom:7px; }
  .song a { color:var(--ink); text-decoration:none; font-weight:600; flex:1;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .song a:hover { color:var(--gold); }
  .song .del { color:var(--dim); cursor:pointer; font-size:.8rem; background:none;
    border:0; }
  .song .del:hover { color:#ff9d9d; }
  .cho { color:var(--dim); font-size:.78rem; text-decoration:none; }
  .cho:hover { color:var(--gold); }
</style></head>
<body><div class="wrap">
  <h1>chord-detect</h1>
  <p class="sub">Pick a song. It works out the chords, beats, key and tempo, then
     you can play along and see where every chord falls.</p>

  <div id="picker">
  <div id="drop">
    <label class="pick" for="file">Choose a song</label>
    <input type="file" id="file" accept="audio/*">
    <p>or drag one here &middot; mp3, wav, m4a, flac &middot; stays on your machine</p>
  </div>

  <details>
    <summary>Options</summary>
    <div class="opts">
      <label class="f">Shapes
        <select id="shapes">
          <option value="">As detected</option>
          <option value="G">G shapes</option>
          <option value="C">C shapes</option>
          <option value="D">D shapes</option>
          <option value="A">A shapes</option>
          <option value="E">E shapes</option>
          <option value="G,C,D">G, then C, then D</option>
        </select></label>
      <label class="f">Capo<input type="number" id="capo" value="0" min="0" max="11"></label>
      <label class="f">Transpose<input type="number" id="transpose" value="0" min="-11" max="11"></label>
      <label class="f">Model
        <select id="model">
          <option value="cnnlstm" selected>Large vocab (slash chords)</option>
          <option value="btc">Transformer (BTC)</option>
          <option value="template">Chroma templates</option>
        </select></label>
      <label class="f">Chords
        <select id="vocabulary">
          <option value="simple" selected>Simple</option>
          <option value="full">Full</option>
        </select></label>
      <label class="f">Sensitivity<input type="number" id="sensitivity" value="0.92"
        min="0.5" max="0.99" step="0.01"></label>
    </div>
  </details>

  <h2>Recent</h2>
  <div id="library"></div>
  </div>

  <!-- Analysis takes 10-20s. Without a view of its own it isn't obvious
       anything is happening, and the home screen looks frozen. -->
  <div id="working" hidden>
    <div class="spinner"></div>
    <div class="wname" id="wname">song</div>
    <div id="stage">Uploading…</div>
    <div class="bar"><i id="fill"></i></div>
    <div class="found" id="found"></div>
    <button class="back" id="cancel">Choose a different song</button>
  </div>
</div>
<script>
  var drop = document.getElementById('drop');
  var fileInput = document.getElementById('file');
  var picker = document.getElementById('picker');
  var working = document.getElementById('working');
  var stage = document.getElementById('stage');
  var fill = document.getElementById('fill');
  var found = document.getElementById('found');
  var wname = document.getElementById('wname');

  function showWorking(name) {
    wname.textContent = name;
    stage.textContent = 'Uploading…';
    stage.className = '';
    found.textContent = '';
    fill.style.width = '4%';
    picker.hidden = true;
    working.hidden = false;
  }
  function showPicker() {
    working.hidden = true;
    picker.hidden = false;
  }
  document.getElementById('cancel').addEventListener('click', showPicker);

  ['dragenter','dragover'].forEach(function (e) {
    drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.add('over'); });
  });
  ['dragleave','drop'].forEach(function (e) {
    drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', function (ev) {
    if (ev.dataTransfer.files.length) send(ev.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', function () {
    if (fileInput.files.length) send(fileInput.files[0]);
  });

  function send(file) {
    // Name the song after the file, minus its extension.
    var label = file.name.replace(/\.[^.]+$/, '');
    showWorking(label);
    var body = new FormData();
    body.append('file', file);
    body.append('capo', document.getElementById('capo').value || 0);
    body.append('transpose', document.getElementById('transpose').value || 0);
    body.append('sensitivity', document.getElementById('sensitivity').value || 0.92);
    body.append('vocabulary', document.getElementById('vocabulary').value);
    body.append('shapes', document.getElementById('shapes').value);
    body.append('model', document.getElementById('model').value);
    try { localStorage.setItem('shapes', document.getElementById('shapes').value); } catch (e) {}
    fetch('/analyze', { method:'POST', body: body })
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || 'Upload failed'); return j; }); })
      .then(function (job) {
        if (job.cached) { go(job.id); return; }
        poll(job.id);
      })
      .catch(function (err) {
        stage.textContent = err.message; stage.className = 'err'; fill.style.width = '0';
      });
  }

  function poll(id) {
    fetch('/jobs/' + id).then(function (r) { return r.json(); }).then(function (s) {
      if (s.error) {
        stage.textContent = s.error; stage.className = 'err'; return;
      }
      stage.textContent = s.stage;
      // Surface findings as they arrive, so the wait shows real progress.
      var bits = [];
      if (s.key) bits.push('Key of ' + s.key);
      if (s.tempo) bits.push(Math.round(s.tempo) + ' bpm');
      if (s.shape) bits.push(s.shape + ' shapes, capo ' + s.capo);
      found.textContent = bits.join('  ·  ');
      fill.style.width = Math.max(4, s.fraction * 100) + '%';
      if (s.done) { go(id); return; }
      setTimeout(function () { poll(id); }, 400);
    }).catch(function () { setTimeout(function () { poll(id); }, 800); });
  }

  function go(id) { window.location.href = '/chart/' + id; }

  function loadLibrary() {
    fetch('/library').then(function (r) { return r.json(); }).then(function (rows) {
      var box = document.getElementById('library');
      if (!rows.length) {
        box.innerHTML = '<p class="sub">Nothing yet — analyze a song and it shows up here.</p>';
        return;
      }
      box.innerHTML = rows.map(function (row) {
        var safe = document.createElement('div');
        safe.textContent = row.name;
        return '<div class="song"><a href="/chart/' + row.id + '">' + safe.innerHTML + '</a>' +
          '<button class="rename" data-id="' + row.id + '" data-name="' +
            safe.innerHTML.replace(/"/g, '&quot;') + '">rename</button>' +
          '<a class="cho" href="/chordpro/' + row.id + '">.cho</a>' +
          '<button class="del" data-id="' + row.id + '">remove</button></div>';
      }).join('');
      box.querySelectorAll('.del').forEach(function (btn) {
        btn.addEventListener('click', function () {
          fetch('/library/' + btn.dataset.id, { method:'DELETE' }).then(loadLibrary);
        });
      });
      box.querySelectorAll('.rename').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var next = prompt('Name this song:', btn.dataset.name);
          if (next === null || !next.trim()) return;
          var body = new FormData();
          body.append('name', next.trim());
          fetch('/library/' + btn.dataset.id + '/name', { method:'POST', body: body })
            .then(loadLibrary);
        });
      });
    });
  }
  // Remember the shape preference — it's a standing habit, not a per-song choice.
  try {
    var saved = localStorage.getItem('shapes');
    if (saved !== null) document.getElementById('shapes').value = saved;
    if (saved) document.querySelector('details').open = true;
  } catch (e) {}

  loadLibrary();
</script></body></html>
"""


def main() -> None:
    import uvicorn

    port = 8765
    url = f"http://127.0.0.1:{port}"
    CACHE.mkdir(parents=True, exist_ok=True)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"chord-detect running at {url}  (ctrl-c to stop)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
