"""TTS backend kokoro: Kokoro-82M via kokoro-onnx, bm_george (DESIGN 6.4)."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.request
from collections.abc import Iterator
from typing import Any

import numpy as np

log = logging.getLogger("relayvox.tts.kokoro")

_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# Pinned SHA-256 of the release files. The int8 build has no pin yet: it is never downloaded,
# only used when already present (see ensure_models).
SHA256: dict[str, str | None] = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    "kokoro-v1.0.int8.onnx": None,
}

_lock = threading.Lock()
_model: dict[str, Any] = {"k": None, "key": None}


def model_dir(cfg: Any) -> str:
    custom = str(cfg["tts"].get("model_dir", "") or "")
    if custom:
        return custom
    import platformdirs

    return os.path.join(platformdirs.user_data_dir("relayvox"), "kokoro")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(name: str, dest: str) -> None:
    want = SHA256.get(name)
    if want is None:
        raise RuntimeError(f"{name} has no pinned checksum, refusing to download it")
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(_BASE + name)  # noqa: S310 - fixed https URL
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:  # noqa: S310
        for block in iter(lambda: r.read(1 << 20), b""):
            f.write(block)
    if _sha256(tmp) != want:
        os.remove(tmp)
        raise RuntimeError(f"{name} failed its checksum and was deleted")
    os.replace(tmp, dest)


def ensure_models(cfg: Any) -> tuple[str, str]:
    """Return (model path, voices path), downloading missing pinned files (relayvox models)."""
    d = model_dir(cfg)
    onnx = "kokoro-v1.0.int8.onnx" if cfg["tts"].get("kokoro_small") else "kokoro-v1.0.onnx"
    paths = []
    for name in (onnx, "voices-v1.0.bin"):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            log.info("downloading %s", name)
            _download(name, p)
        paths.append(p)
    return paths[0], paths[1]


def load(cfg: Any) -> Any:
    """Create the Kokoro model once and keep it loaded."""
    with _lock:
        paths = list(ensure_models(cfg))
        if _model["k"] is None or _model["key"] != paths:
            from kokoro_onnx import Kokoro

            _model["k"], _model["key"] = Kokoro(*paths), paths
        return _model["k"]


def synth(
    text: str, cfg: Any, voice: str | None = None, lang: str | None = None
) -> Iterator[tuple[bytes, int]]:
    """Yield (int16 mono PCM, sample_rate) for text (section 6.4)."""
    k = load(cfg)
    voice = voice or str(cfg["tts"]["voice"])
    lang = lang or ("en-gb" if voice[:1] == "b" else "en-us")
    with _lock:  # one ONNX session: serialize concurrent sentences
        samples, rate = k.create(text, voice=voice, speed=float(cfg["tts"]["speed"]), lang=lang)
    yield (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes(), int(rate)
