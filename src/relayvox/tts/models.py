"""`relayvox models` and `relayvox voices`: fetch the pinned model files, list Kokoro voices."""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Any

from relayvox.tts import kokoro

log = logging.getLogger("relayvox.tts.models")

_OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
_VAD = "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/faster_whisper/assets/"
VOICES_FILE = "voices-v1.0.bin"

# name -> (https URL, SHA-256). Files are verified before they are renamed into place.
WAKE_FILES: dict[str, tuple[str, str]] = {
    "melspectrogram.onnx": (
        _OWW + "melspectrogram.onnx",
        "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    ),
    "embedding_model.onnx": (
        _OWW + "embedding_model.onnx",
        "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    ),
    "hey_jarvis_v0.1.onnx": (
        _OWW + "hey_jarvis_v0.1.onnx",
        "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    ),
    "alexa_v0.1.onnx": (
        _OWW + "alexa_v0.1.onnx",
        "6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e",
    ),
}
VAD_FILE = (
    "silero_vad_v6.onnx",
    _VAD + "silero_vad_v6.onnx",
    "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2",
)


def data_dir(sub: str) -> str:
    import platformdirs

    return os.path.join(platformdirs.user_data_dir("relayvox"), sub)


def fetch(url: str, dest: str, sha256: str) -> None:
    """Download an https URL to a temp file, verify its checksum, then rename into place."""
    if not url.startswith("https://"):
        raise ValueError("only https downloads are allowed")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(url)  # noqa: S310 - https checked above
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:  # noqa: S310
            for block in iter(lambda: r.read(1 << 20), b""):
                f.write(block)
        if kokoro._sha256(tmp) != sha256:
            raise RuntimeError(f"{os.path.basename(dest)} failed its checksum and was deleted")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _get(label: str, url: str, dest: str, sha256: str) -> bool:
    if os.path.isfile(dest):
        print(f"{label}: already present")
        return True
    try:
        fetch(url, dest, sha256)
    except (OSError, RuntimeError, ValueError) as e:
        print(f"{label}: failed ({e})")
        return False
    print(f"{label}: downloaded to {dest}")
    return True


def download_models(wake: bool = False, cfg: Any = None) -> int:
    """Fetch the Kokoro files into tts.model_dir; with wake, the wake-word and VAD files too."""
    if cfg is None:
        from relayvox import config

        cfg = config.load()
    ok = True
    d = kokoro.model_dir(cfg)
    for name in ("kokoro-v1.0.onnx", VOICES_FILE):
        ok &= _get(name, kokoro._BASE + name, os.path.join(d, name), str(kokoro.SHA256[name]))
    if wake:
        wd = data_dir("wake")
        chosen = f"{cfg['wake']['model']}_v0.1.onnx"
        for name, (url, sha) in WAKE_FILES.items():
            if name in ("melspectrogram.onnx", "embedding_model.onnx", chosen):
                ok &= _get(name, url, os.path.join(wd, name), sha)
        if chosen not in WAKE_FILES:
            print(f"{cfg['wake']['model']}: custom wake model, nothing to download")
        name, url, sha = VAD_FILE
        if _bundled_vad():
            print(f"{name}: shipped with faster-whisper")
        else:
            ok &= _get(name, url, vad_target(), sha)
    return 0 if ok else 1


def _bundled_vad() -> bool:
    try:
        from faster_whisper.utils import get_assets_path

        return os.path.isfile(os.path.join(get_assets_path(), VAD_FILE[0]))
    except Exception:  # noqa: BLE001 - package missing or moved
        return False


def vad_target() -> str:
    """Where a downloaded Silero VAD file goes (used when faster-whisper does not ship it)."""
    return os.path.join(data_dir("vad"), VAD_FILE[0])


def voices(cfg: Any = None) -> int:
    """List Kokoro voices from the voices file, or say how to get it."""
    if cfg is None:
        from relayvox import config

        cfg = config.load()
    path = os.path.join(kokoro.model_dir(cfg), VOICES_FILE)
    if not os.path.isfile(path):
        print("No voices file yet. Run: relayvox models")
        return 1
    import numpy as np

    with np.load(path) as z:
        names = sorted(str(n) for n in z.files)
    for n in names:
        print(n)
    return 0
