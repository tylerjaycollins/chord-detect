"""BTC: a learned chord recognizer, as an alternative to chroma templates.

Bi-directional Transformer for Chord Recognition (Park et al., ISMIR 2019),
MIT licensed, ~12 MB of weights. Trained on hand-annotated corpora, it works
from a 144-bin CQT rather than a 12-bin chroma summary — which matters because
collapsing the spectrum to twelve numbers is where our template pipeline hits
its ceiling. On sparse arrangements the chroma can come back as bare root and
fifth, leaving nothing to tell major from minor; the transformer still has the
full time-frequency picture plus learned expectations about how chords move.

The upstream code is from 2019 and doesn't run as-is on current numpy/PyYAML,
so `ensure_model()` clones it and applies deterministic compatibility patches.
Patching after cloning (rather than vendoring a fork) keeps us honest about
what's upstream and lets a re-clone recover cleanly.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np

from .chords import ChordSpan, merge_short_spans

REPO_URL = "https://github.com/jayg996/BTC-ISMIR19.git"
HOME = Path.home() / ".cache" / "chord-detect" / "btc"

# Upstream quality names -> how this project spells chords.
_QUALITY_MAP = {
    "maj": "",
    "min": "m",
    "dim": "dim",
    "aug": "aug",
    "min6": "m6",
    "maj6": "6",
    "min7": "m7",
    "maj7": "maj7",
    "7": "7",
    "dim7": "dim7",
    "hdim7": "m7b5",
    "sus2": "sus2",
    "sus4": "sus4",
    # No minor-major 7th in our vocabulary; the plain minor triad is the
    # closest honest reduction.
    "minmaj7": "m",
}

_model_cache: dict[bool, tuple] = {}


def is_installed() -> bool:
    return (HOME / "btc_model.py").exists() and (
        HOME / "test" / "btc_model_large_voca.pt"
    ).exists()


def ensure_model(quiet: bool = False) -> Path:
    """Clone the upstream repo and patch it for modern dependencies."""
    if is_installed():
        return HOME

    HOME.parent.mkdir(parents=True, exist_ok=True)
    if not quiet:
        print(f"Fetching BTC model into {HOME} (~40 MB, one time)…")
    subprocess.run(
        ["git", "clone", "--depth", "1", "-q", REPO_URL, str(HOME)],
        check=True,
    )
    _patch_for_modern_deps()
    return HOME


def _patch_for_modern_deps() -> None:
    """Rewrite 2019-era API calls that current numpy and PyYAML rejected.

    `np.float`/`np.int`/`np.bool` were removed in numpy 1.24, and PyYAML 6
    requires an explicit Loader. Both are mechanical, behaviour-preserving
    substitutions.
    """
    import re

    aliases = [
        (re.compile(r"\bnp\.float\b(?!\d)"), "float"),
        (re.compile(r"\bnp\.int\b(?!\d)"), "int"),
        (re.compile(r"\bnp\.bool\b(?!_)"), "bool"),
        (re.compile(r"\bnp\.object\b"), "object"),
    ]
    for path in HOME.rglob("*.py"):
        text = original = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, replacement in aliases:
            text = pattern.sub(replacement, text)
        if path.name == "hparams.py":
            text = text.replace("yaml.load(f)", "yaml.safe_load(f)")
        if text != original:
            path.write_text(text)


def _load(large_voca: bool, device: str):
    """Load the model once per (vocabulary, device) and keep it around."""
    key = (large_voca, device)
    if key in _model_cache:
        return _model_cache[key]

    ensure_model()
    if str(HOME) not in sys.path:
        sys.path.insert(0, str(HOME))

    import torch
    from utils.hparams import HParams  # type: ignore
    from btc_model import BTC_model  # type: ignore
    from utils.mir_eval_modules import idx2chord, idx2voca_chord  # type: ignore

    config = HParams.load(str(HOME / "run_config.yaml"))
    if large_voca:
        config.feature["large_voca"] = True
        config.model["num_chords"] = 170
        weights = HOME / "test" / "btc_model_large_voca.pt"
        labels = idx2voca_chord()
    else:
        weights = HOME / "test" / "btc_model.pt"
        labels = {i: name for i, name in enumerate(idx2chord)}

    model = BTC_model(config=config.model).to(device)
    checkpoint = torch.load(weights, map_location=device, weights_only=False)
    model.load_state_dict(checkpoint["model"])
    model.eval()

    bundle = (model, config, labels, float(checkpoint["mean"]), float(checkpoint["std"]))
    _model_cache[key] = bundle
    return bundle


def _resolve_device(requested: str | None) -> str:
    if requested:
        return requested
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def harte_to_label(chord: str) -> str:
    """Convert 'C:min7' / 'C' / 'N' / 'X' to this project's spelling."""
    if chord in ("N", "X", ""):
        return "N"
    if ":" not in chord:
        return chord  # bare root means major
    root, quality = chord.split(":", 1)
    suffix = _QUALITY_MAP.get(quality)
    if suffix is None:
        return root  # unknown quality: fall back to the plain triad
    return f"{root}{suffix}"


def recognize(
    audio_path: str | Path,
    *,
    large_voca: bool = True,
    device: str | None = None,
    min_duration: float = 0.28,
) -> list[ChordSpan]:
    """Run BTC over a file and return chord spans."""
    import torch

    resolved = _resolve_device(device)
    try:
        model, config, labels, mean, std = _load(large_voca, resolved)
    except (RuntimeError, NotImplementedError):
        # Some ops still lack MPS kernels; CPU is only a few seconds slower.
        resolved = "cpu"
        model, config, labels, mean, std = _load(large_voca, resolved)

    from utils.mir_eval_modules import audio_file_to_features  # type: ignore

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        feature, seconds_per_frame, _ = audio_file_to_features(
            str(audio_path), config
        )

    feature = (feature.T - mean) / std
    n_timestep = config.model["timestep"]
    pad = n_timestep - (feature.shape[0] % n_timestep)
    feature = np.pad(feature, ((0, pad), (0, 0)), mode="constant")
    n_windows = feature.shape[0] // n_timestep

    tensor = torch.tensor(feature, dtype=torch.float32).unsqueeze(0).to(resolved)

    # Decode window by window, exactly as upstream does: the model is
    # bi-directional over a fixed 108-frame span, so windows aren't independent
    # of frame position and can't be batched arbitrarily.
    predictions: list[int] = []
    with torch.no_grad():
        for window in range(n_windows):
            chunk = tensor[:, n_timestep * window : n_timestep * (window + 1), :]
            attended, _ = model.self_attn_layers(chunk)
            predicted, _ = model.output_layer(attended)
            predictions.extend(predicted.squeeze().tolist())

    # Drop the frames that only exist because of padding.
    valid = len(predictions) - pad
    predictions = predictions[:valid]
    if not predictions:
        return []

    spans: list[ChordSpan] = []
    start = 0.0
    current = predictions[0]
    for index in range(1, len(predictions)):
        if predictions[index] == current:
            continue
        end = seconds_per_frame * index
        spans.append(
            ChordSpan(
                label=harte_to_label(labels[current]),
                start=start,
                end=end,
                # BTC emits labels, not calibrated probabilities. Reporting a
                # flat nominal value is more honest than inventing a number
                # that looks like the template model's posterior.
                confidence=0.75,
            )
        )
        start = end
        current = predictions[index]

    spans.append(
        ChordSpan(
            label=harte_to_label(labels[current]),
            start=start,
            end=seconds_per_frame * len(predictions),
            confidence=0.75,
        )
    )

    return merge_short_spans(spans, min_duration=min_duration)
