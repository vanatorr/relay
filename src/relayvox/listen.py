"""Mic pick, capture, auto-gain, VAD endpointing, Whisper (DESIGN section 5.6, 7, 11).

Extras beyond the frozen contract (all plain module functions, listed for the integrator):
configure(cfg), preload(), model_ready(), last_language(), list_mics(), mic_is_silent(),
set_notifier(fn), is_cancel(text), strip_end_word(text), Endpointer.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from relayvox import config as _config

log = logging.getLogger("relayvox.listen")

RATE = 16000
FRAME = 512  # 32 ms at 16 kHz, the Silero frame
FRAME_MS = 32
MIN_CLIP_S = 0.4
MIN_RMS = 0.002
MAX_KEY_S = 120.0
MAX_HANDSFREE_S = 30.0
NO_SPEECH_S = 4.0
MIN_SPEECH_MS = 300
SPECULATE_MS = 600
TAIL_S = 0.15  # keep the last word after the key is released
DEFAULT_VOCAB = list(_config.DEFAULTS["stt"]["vocabulary"])
_FILLERS = re.compile(r"\b(?:um+|uh+|uhm+|erm+|hmm+|euh+)\b[,.]?\s*", re.I)

_cfg: dict[str, Any] = {k: dict(v) for k, v in _config.DEFAULTS.items()}
_notify: Callable[[str], None] | None = None


def configure(cfg: dict[str, Any]) -> None:
    """Give this module the loaded Config (nested dict)."""
    global _cfg
    _cfg = cfg


def set_notifier(fn: Callable[[str], None] | None) -> None:
    """fn(text) is called with a short line to speak ("I couldn't find that microphone.")."""
    global _notify
    _notify = fn


def _say(text: str) -> None:
    if _notify is not None:
        try:
            _notify(text)
        except Exception:
            log.exception("notifier failed")


# --------------------------------------------------------------------------- VAD


def vad_path() -> str:
    from faster_whisper.utils import get_assets_path

    bundled = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
    if os.path.isfile(bundled):
        return bundled
    from relayvox.tts.models import vad_target  # fetched by `relayvox models --wake`

    return vad_target()


class Vad:
    """Streaming wrapper over faster-whisper's silero_vad_v6.onnx (section 5.6).

    One instance per stream: the LSTM state (h, c) and the 64-sample context are carried across
    frames, so listen and wake must never share an instance.
    """

    CONTEXT = 64

    def __init__(self, path: str | None = None) -> None:
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._sess = onnxruntime.InferenceSession(
            path or vad_path(), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def input_names(self) -> set[str]:
        return {i.name for i in self._sess.get_inputs()}

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def prob(self, frame512: np.ndarray) -> float:
        """Speech probability for one 512-sample 16 kHz frame."""
        frame = np.asarray(frame512, dtype=np.float32).reshape(1, FRAME)
        x = np.concatenate([self._ctx, frame], axis=1)
        out, self._h, self._c = self._sess.run(None, {"input": x, "h": self._h, "c": self._c})
        self._ctx = frame[:, -self.CONTEXT :]
        return float(np.asarray(out).reshape(-1)[0])


# --------------------------------------------------------------------------- endpointing


class Endpointer:
    """Pure end-of-speech state machine over 32 ms frames (section 5.6).

    push(prob) returns an event or None: "speculate" (silence reached 600 ms), "unspeculate"
    (speech resumed after a speculation), "check" (word mode: a >=600 ms pause), "end", or
    "timeout" (no speech started in 4 s). Time is counted in frames, never wall-clock.
    """

    def __init__(
        self,
        mode: str = "silence",
        end_silence_ms: int = 1000,
        threshold: float = 0.5,
        no_speech_s: float = NO_SPEECH_S,
        max_s: float = MAX_HANDSFREE_S,
    ) -> None:
        self.mode = mode
        self.end_silence_ms = end_silence_ms
        self.threshold = threshold
        self.no_speech_ms = int(no_speech_s * 1000)
        self.max_ms = int(max_s * 1000)
        self.total_ms = 0
        self.speech_ms = 0
        self.silence_ms = 0
        self._speculated = False
        self._checked = False

    def push(self, p: float) -> str | None:
        self.total_ms += FRAME_MS
        if p >= self.threshold:
            self.speech_ms += FRAME_MS
            self.silence_ms = 0
            self._checked = False
            if self._speculated:
                self._speculated = False
                return "unspeculate"
            return None
        self.silence_ms += FRAME_MS
        if self.speech_ms < MIN_SPEECH_MS:
            return "timeout" if self.total_ms >= self.no_speech_ms else None
        if self.total_ms >= self.max_ms:
            return "end"
        if self.mode == "word":
            if self.silence_ms >= 4000:
                return "end"
            if self.silence_ms >= SPECULATE_MS and not self._checked:
                self._checked = True
                return "check"
            return None
        if self.silence_ms >= self.end_silence_ms:
            return "end"
        if self.silence_ms >= SPECULATE_MS and not self._speculated:
            self._speculated = True
            return "speculate"
        return None


def _norm_words(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower(), flags=re.UNICODE).strip()


def is_cancel(text: str, words: list[str] | None = None) -> bool:
    """True when "cancel" / "never mind" is the whole utterance or its last words."""
    words = words if words is not None else _cfg["listen"]["cancel_words"]
    t = " " + _norm_words(text) + " "
    return any(t.endswith(" " + _norm_words(w) + " ") for w in words if _norm_words(w))


def strip_end_word(text: str, words: list[str] | None = None) -> tuple[str, bool]:
    """Remove a trailing end word ("send it", "over"); returns (text, found)."""
    words = words if words is not None else _cfg["listen"]["end_words"]
    for w in words:
        parts = _norm_words(w).split()
        if not parts:
            continue
        sep = r"[\s,.;:!?-]+"
        pat = re.compile(sep.join(map(re.escape, parts)) + r"[\s.!?]*$", re.I)
        m = pat.search(text)
        if m and (m.start() == 0 or not text[m.start() - 1].isalnum()):
            return text[: m.start()].strip(" ,.;:-"), True
    return text, False


# --------------------------------------------------------------------------- audio helpers


def apply_gain(pcm: np.ndarray, max_gain: float = 20.0) -> np.ndarray | None:
    """Auto-gain (section 7): None when too short or still silent after gain."""
    if len(pcm) < RATE * MIN_CLIP_S:
        return None
    peak = float(np.abs(pcm).max())
    if peak <= 0:
        return None
    if peak < 0.5:
        pcm = np.clip(pcm * min(0.9 / peak, float(max_gain)), -1.0, 1.0).astype(np.float32)
    if float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) < MIN_RMS:
        return None
    return pcm


def list_mics() -> list[str]:
    import miniaudio

    return [d["name"] for d in miniaudio.Devices().get_captures()]


def pick_mic(want: str) -> Any:
    """Device id for a name substring (case-insensitive); None = system default."""
    if not want:
        return None
    import miniaudio

    caps = miniaudio.Devices().get_captures()
    for d in caps:
        if want.lower() in d["name"].lower():
            return d["id"]
    log.warning("mic %r not found; devices: %s", want, [d["name"] for d in caps])
    _say("I couldn't find that microphone.")
    return None


class _Capture:
    """miniaudio capture, 16 kHz mono int16. Chunks go to cb; nothing touches the disk."""

    def __init__(self, cb: Callable[[bytes], None]) -> None:
        import miniaudio

        self._dev = miniaudio.CaptureDevice(
            input_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=RATE,
            buffersize_msec=32,
            device_id=pick_mic(str(_cfg["audio"]["mic"])),
        )

        def gen() -> Any:
            _ = yield
            while True:
                data = yield
                if data:
                    cb(bytes(data))

        g = gen()
        next(g)
        self._dev.start(g)

    def close(self) -> None:
        try:
            self._dev.close()
        except Exception:
            log.debug("capture close failed", exc_info=True)


_make_capture: Callable[[Callable[[bytes], None]], Any] = _Capture


def mic_is_silent(seconds: float = 0.3) -> bool:
    """Capture briefly; True if every sample is zero (the silent-virtual-default failure)."""
    chunks: list[bytes] = []
    cap = _make_capture(chunks.append)
    time.sleep(seconds)
    cap.close()
    data = b"".join(chunks)
    return not data or not np.frombuffer(data, dtype=np.int16).any()


# --------------------------------------------------------------------------- recording

_lock = threading.Lock()
_chunks: list[bytes] = []
_cap: Any = None
_worker: threading.Thread | None = None
_stop = threading.Event()
_q: queue.Queue[bytes | None] = queue.Queue()
_endpoint_cb: Callable[[np.ndarray | None], None] | None = None
_started_at = 0.0


def on_endpoint(cb: Callable[[np.ndarray | None], None]) -> None:
    """Register the callback fired by VAD / end word in hands-free mode (section 5.6)."""
    global _endpoint_cb
    _endpoint_cb = cb


def _snapshot() -> np.ndarray:
    data = b"".join(_chunks)
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _final(pcm: np.ndarray) -> np.ndarray | None:
    return apply_gain(pcm, float(_cfg["audio"]["max_gain"]))


def start_recording(end: str = "key") -> None:
    """Open the mic; end is "key" | "silence" | "word" (section 7, 5.6)."""
    global _cap, _worker, _started_at
    with _lock:
        if _cap is not None:
            return
        _chunks.clear()
        _stop.clear()
        while not _q.empty():
            _q.get_nowait()
        _started_at = time.monotonic()

        def on_chunk(b: bytes) -> None:
            _chunks.append(b)
            if end != "key":
                _q.put(b)

        _cap = _make_capture(on_chunk)
        if end != "key":
            _worker = threading.Thread(target=_endpoint_loop, args=(end,), daemon=True)
            _worker.start()
        else:
            _worker = threading.Thread(target=_key_limit, daemon=True)
            _worker.start()


def _key_limit() -> None:
    """Record for 120 s at most, then fire the endpoint (the app sends)."""
    if not _stop.wait(MAX_KEY_S) and _endpoint_cb is not None:
        _endpoint_cb(stop_recording())


def stop_recording() -> np.ndarray | None:
    """Close the mic; float32 16 kHz after auto-gain, or None if too short/silent (section 7)."""
    global _cap
    with _lock:
        cap, _cap = _cap, None
    if cap is None:
        return None
    time.sleep(TAIL_S)
    cap.close()
    _stop.set()
    _q.put(None)
    pcm = _snapshot()
    peak = float(np.abs(pcm).max()) if len(pcm) else 0.0
    log.info("recorded %.1fs, peak %.3f", len(pcm) / RATE, peak)
    return _final(pcm)


def _endpoint_loop(mode: str) -> None:
    lis = _cfg["listen"]
    vad = Vad()
    ep = Endpointer(mode, int(lis["end_silence_ms"]))
    buf = np.zeros(0, dtype=np.float32)
    n_done = 0  # samples already fed to the VAD
    spec_len = 0  # >0 while a speculative transcription of audio[:spec_len] is still valid
    while not _stop.is_set():
        b = _q.get()
        if b is None:
            return
        buf = np.concatenate([buf, np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0])
        while len(buf) >= FRAME:
            frame, buf = buf[:FRAME], buf[FRAME:]
            n_done += FRAME
            ev = ep.push(vad.prob(frame))
            if ev == "speculate":
                spec_len = n_done
                _speculate(spec_len)
            elif ev == "unspeculate":
                spec_len = 0
            elif ev == "check" and _word_check(lis):
                _finish(0)
                return
            elif ev == "end":
                _finish(spec_len)
                return
            elif ev == "timeout":
                _finish(0, empty=True)
                return


def _speculate(n_samples: int) -> None:
    """Start transcribing the audio so far; the result is cached for the final transcribe()."""
    audio = _final(_snapshot()[:n_samples])
    if audio is not None:
        threading.Thread(target=_transcribe_cached, args=(audio,), daemon=True).start()


def _word_check(lis: dict[str, Any]) -> bool:
    audio = _final(_snapshot()[-RATE * 2 :])
    if audio is None:
        return False
    return strip_end_word(transcribe(audio, [], cache=False), lis["end_words"])[1]


def _finish(n_samples: int, empty: bool = False) -> None:
    """Close the mic and fire the endpoint; n_samples > 0 trims to the speculated audio."""
    stop_recording()
    snap = _snapshot()
    pcm = None if empty else _final(snap[:n_samples] if n_samples else snap)
    if _endpoint_cb is not None:
        _endpoint_cb(pcm)


# --------------------------------------------------------------------------- Whisper

_model: dict[str, Any] = {"m": None}
_model_done = threading.Event()
_lang = {"code": ""}
_cache: dict[str, str] = {}
_cache_lang: dict[str, str] = {}
_cache_lock = threading.Lock()


def _add_cuda_dlls() -> None:
    """Make the nvidia pip packages' DLLs visible to ctranslate2 (Windows)."""
    if sys.platform != "win32":
        return
    import glob
    import site

    for sp in [*site.getsitepackages(), site.getusersitepackages()]:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if os.path.isdir(d):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        return False


def preload() -> None:
    """Load Whisper in a background thread and keep it loaded (section 7)."""
    threading.Thread(target=_load_model, daemon=True).start()


def model_ready() -> bool:
    return _model_done.is_set() and _model["m"] is not None


def _load_model() -> None:
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        from faster_whisper import WhisperModel

        name = str(_cfg["stt"]["model"])
        _add_cuda_dlls()
        gpu = name == "auto" and _cuda_available()
        size = "large-v3-turbo" if gpu else ("small" if name == "auto" else name)
        _offline_if_cached(size)
        try:
            if gpu or (name != "auto" and _cuda_available()):
                _model["m"] = WhisperModel(size, device="cuda", compute_type="float16")
                log.info("whisper %s ready on GPU", size)
            else:
                raise RuntimeError("cpu")
        except Exception:
            if gpu:
                log.warning("GPU load failed, falling back to CPU small")
                size = "small"
            _model["m"] = WhisperModel(size, device="cpu", compute_type="int8")
            log.info("whisper %s ready on CPU", size)
    except Exception:
        log.exception("whisper failed to load")
    finally:
        _model_done.set()


def _offline_if_cached(size: str) -> None:
    try:
        from faster_whisper.utils import download_model

        download_model(size, local_files_only=True)
        os.environ["HF_HUB_OFFLINE"] = "1"
    except Exception:
        log.info("whisper %s not cached yet, first run will download it", size)


def last_language() -> str:
    """Language code Whisper detected for the latest transcribe() call ("" if unknown)."""
    return _lang["code"]


def _prompt(names: list[str]) -> str | None:
    vocab = list(_cfg["stt"].get("vocabulary", DEFAULT_VOCAB))
    parts = []
    if names:
        parts.append("Claude Code sessions: " + ", ".join(sorted(set(names))) + ".")
    if vocab:
        parts.append("Vocabulary: " + ", ".join(vocab) + ".")
    return " ".join(parts) or None


def _key(audio: np.ndarray) -> str:
    return hashlib.sha1(audio.tobytes(), usedforsecurity=False).hexdigest()


def _transcribe_cached(audio: np.ndarray) -> None:
    k = _key(audio)
    with _cache_lock:
        if k in _cache:
            return
    text = transcribe(audio, [], cache=False)
    with _cache_lock:
        _cache[k] = text
        _cache_lang[k] = _lang["code"]
        while len(_cache) > 4:
            old = next(iter(_cache))
            _cache.pop(old, None)
            _cache_lang.pop(old, None)


def transcribe(audio: np.ndarray, names: list[str], cache: bool = True) -> str:
    """Whisper, beam_size=1, initial_prompt with session names and vocabulary (section 7)."""
    if cache:
        with _cache_lock:
            hit = _cache.get(_key(audio))
            if hit is not None:
                _lang["code"] = _cache_lang.get(_key(audio), _lang["code"])
                return hit
    _model_done.wait(90)
    m = _model["m"]
    if m is None or len(audio) < RATE * MIN_CLIP_S:
        return ""
    segs, info = m.transcribe(
        audio,
        language=str(_cfg["stt"]["language"]) or None,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
        without_timestamps=True,
        initial_prompt=_prompt(names),
    )
    _lang["code"] = str(getattr(info, "language", "") or "")
    text = " ".join(s.text.strip() for s in segs).strip()
    return re.sub(r"\s{2,}", " ", _FILLERS.sub("", text)).strip()
