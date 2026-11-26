"""Optional wake word: openWakeWord ONNX pipeline, VAD-gated (DESIGN section 5.6, 11).

Runs openWakeWord's three ONNX files directly on onnxruntime (no openwakeword package).
Pipeline per 80 ms chunk: energy gate, Silero VAD, melspectrogram, embedding, wake model.
Models are never downloaded here; they are looked up in <data dir>/wake (relayvox models --wake).
"""

from __future__ import annotations

import collections
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

log = logging.getLogger("relayvox.wake")

CHUNK = 1280  # 80 ms at 16 kHz
MEL_CONTEXT = 160 * 3
PREROLL_CHUNKS = 12  # ~1 s
HANGOVER_CHUNKS = 5  # keep processing ~400 ms after the VAD drops
REFRACTORY_S = 1.5
TAIL_S = 0.3
MIN_FLOOR = 0.004

# fakes in tests: mel(int16-scale float32 (1, n)) -> (frames, 32); emb((1, 76, 32, 1)) -> (96,);
# wake((1, 16, 96)) -> score
Model = Callable[[np.ndarray], Any]


class Detector:
    """One wake pipeline; feed() takes 1280 int16-scale float32 samples, True on wake."""

    def __init__(
        self,
        mel: Model,
        emb: Model,
        wake: Model,
        vad: Any,
        threshold: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mel, self.emb, self.wake, self.vad = mel, emb, wake, vad
        self.threshold = threshold
        self.clock = clock
        self._depth = 0
        self._tail_until = 0.0
        self._lock = threading.Lock()
        self._floor = MIN_FLOOR
        self._open = False
        self._quiet = 0
        self._pre: collections.deque[np.ndarray] = collections.deque(maxlen=PREROLL_CHUNKS)
        self._ctx = np.zeros(MEL_CONTEXT, dtype=np.float32)
        self._mel = np.zeros((0, 32), dtype=np.float32)
        self._feat: list[np.ndarray] = []
        self._hits = 0
        self._cool_until = 0.0
        self._vbuf = np.zeros(0, dtype=np.float32)

    # ---- pause / resume: a depth counter so speech and push-to-talk can both hold it ----
    def pause(self) -> None:
        with self._lock:
            self._depth += 1

    def resume(self) -> None:
        with self._lock:
            self._depth = max(0, self._depth - 1)
            self._tail_until = self.clock() + TAIL_S

    def paused(self) -> bool:
        with self._lock:
            return self._depth > 0 or self.clock() < self._tail_until

    def _reset(self) -> None:
        self._ctx = np.zeros(MEL_CONTEXT, dtype=np.float32)
        self._mel = np.zeros((0, 32), dtype=np.float32)
        self._feat = []
        self._hits = 0

    def _speech(self, chunk: np.ndarray) -> bool:
        """Max Silero probability over the 512-sample frames completed by this chunk."""
        self._vbuf = np.concatenate([self._vbuf, chunk / 32768.0])
        p = 0.0
        while len(self._vbuf) >= 512:
            p = max(p, float(self.vad.prob(self._vbuf[:512])))
            self._vbuf = self._vbuf[512:]
        return p >= 0.5

    def _model_step(self, chunk: np.ndarray) -> float:
        audio = np.concatenate([self._ctx, chunk])
        self._ctx = audio[-MEL_CONTEXT:]
        frames = np.asarray(self.mel(audio[None, :]), dtype=np.float32).reshape(-1, 32) / 10 + 2
        self._mel = np.concatenate([self._mel, frames])[-970:]
        if len(self._mel) < 76:
            return 0.0
        window = self._mel[-76:][None, :, :, None]
        self._feat.append(np.asarray(self.emb(window), dtype=np.float32).reshape(96))
        self._feat = self._feat[-120:]
        if len(self._feat) < 16:
            return 0.0
        return float(np.asarray(self.wake(np.stack(self._feat[-16:])[None])).reshape(-1)[0])

    def feed(self, chunk: np.ndarray) -> bool:
        """Process one 80 ms chunk; True when the wake word fired."""
        if self.paused():
            self._open, self._quiet = False, 0
            self._pre.clear()
            self._vbuf = np.zeros(0, dtype=np.float32)
            return False
        rms = float(np.sqrt(np.mean((chunk / 32768.0) ** 2)))
        loud = rms > max(MIN_FLOOR, 2.5 * self._floor)
        speech = loud and self._speech(chunk)
        if not loud:
            self._floor = 0.95 * self._floor + 0.05 * rms
        if speech:
            self._quiet = 0
            if not self._open:
                self._open = True
                self._reset()
                for old in self._pre:  # 1 s of pre-roll
                    self._model_step(old)
                self._pre.clear()
        elif self._open:
            self._quiet += 1
            if self._quiet > HANGOVER_CHUNKS:
                self._open = False
        if not self._open:
            self._pre.append(chunk)
            return False
        score = self._model_step(chunk)
        self._hits = self._hits + 1 if score >= self.threshold else 0
        if self._hits >= 2 and self.clock() >= self._cool_until:
            self._hits = 0
            self._cool_until = self.clock() + REFRACTORY_S
            return True
        return False


# --------------------------------------------------------------------------- running it


def wake_dir() -> str:
    import platformdirs

    return os.path.join(platformdirs.user_data_dir("relayvox"), "wake")


def _session(path: str) -> Any:
    import onnxruntime

    o = onnxruntime.SessionOptions()
    o.intra_op_num_threads = 1
    o.inter_op_num_threads = 1
    o.log_severity_level = 4
    return onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=o)


def _load_models(model: str) -> tuple[Model, Model, Model] | None:
    d = wake_dir()
    wake_path = model if model.endswith(".onnx") else os.path.join(d, f"{model}_v0.1.onnx")
    paths = [
        os.path.join(d, "melspectrogram.onnx"),
        os.path.join(d, "embedding_model.onnx"),
        wake_path,
    ]
    if not all(os.path.isfile(p) for p in paths):
        log.warning("wake models missing in %s (run: relayvox models --wake)", d)
        return None
    mel_s, emb_s, wake_s = (_session(p) for p in paths)
    mel_in, emb_in, wake_in = (s.get_inputs()[0].name for s in (mel_s, emb_s, wake_s))
    return (
        lambda x: mel_s.run(None, {mel_in: x.astype(np.float32)})[0],
        lambda x: emb_s.run(None, {emb_in: x.astype(np.float32)})[0],
        lambda x: wake_s.run(None, {wake_in: x.astype(np.float32)})[0],
    )


_det: Detector | None = None
_stop = threading.Event()


def start(on_wake: Callable[[], None], cfg: Any) -> bool:
    """Start the listener; False if disabled or models missing (section 5.6)."""
    global _det
    w = cfg["wake"]
    if not w["enabled"]:
        return False
    models = _load_models(str(w["model"]))
    if models is None:
        return False
    from relayvox import listen

    det = Detector(*models, vad=listen.Vad(), threshold=float(w["threshold"]))
    _det = det
    _stop.clear()
    q: queue.Queue[bytes] = queue.Queue(maxsize=200)
    cap = listen._make_capture(lambda b: q.put_nowait(b) if not q.full() else None)

    def loop() -> None:
        buf = np.zeros(0, dtype=np.float32)
        while not _stop.is_set():
            try:
                b = q.get(timeout=0.2)
            except queue.Empty:
                continue
            buf = np.concatenate([buf, np.frombuffer(b, dtype=np.int16).astype(np.float32)])
            while len(buf) >= CHUNK:
                chunk, buf = buf[:CHUNK], buf[CHUNK:]
                if det.feed(chunk):
                    try:
                        on_wake()
                    except Exception:
                        log.exception("on_wake failed")
        cap.close()

    threading.Thread(target=loop, daemon=True, name="wake").start()
    return True


def stop() -> None:
    _stop.set()


def pause() -> None:
    """Pause while Relay speaks, plus 300 ms after, and while push-to-talk records (5.6)."""
    if _det is not None:
        _det.pause()


def resume() -> None:
    """Resume the wake listener; the 300 ms tail is counted inside (section 5.6)."""
    if _det is not None:
        _det.resume()
