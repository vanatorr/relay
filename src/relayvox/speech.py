"""Speak queue with priorities, barge-in, playback, ticks and chimes (DESIGN section 6.2, 11).

Extras beyond the frozen contract (listed for the integrator): configure(cfg), set_mute(on),
is_muted(), last_spoken(), clear_repeat(session), pending(), say/answer accept lang=.
`session` passed to say() is the session's display name; only its first 3 words are spoken.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from relayvox import config as _config

log = logging.getLogger("relayvox.speech")

CHUNK_MS = 20  # playback checks the stop flag at least this often
REPEAT_AFTER_S = 120.0  # a needs_you line repeats once
MAX_PENDING_STATUS = 8
_RANK = {"answer": 0, "needs_you": 1, "status": 2}


# --------------------------------------------------------------------------- tones and player


def _tone(freqs: list[float], ms: int, rate: int = 24000, gain: float = 0.25) -> bytes:
    out = []
    for f in freqs:
        t = np.arange(int(rate * ms / 1000)) / rate
        env = np.minimum(1.0, np.minimum(t, t[::-1] + 1 / rate) / 0.008)  # 8 ms fades
        out.append(np.sin(2 * np.pi * f * t) * env * gain)
    return (np.concatenate(out) * 32767).astype(np.int16).tobytes()


_TONES = {
    "tick": ([880.0], 60),
    "send": ([660.0], 70),
    "chime": ([880.0, 1175.0], 90),
    "needs_you": ([660.0, 988.0, 1320.0], 110),
    "error": ([300.0, 220.0], 110),
}


class MiniPlayer:
    """Blocking miniaudio playback; stop() ends play() within one 20 ms chunk."""

    def __init__(self, volume: float = 0.6) -> None:
        self.volume = volume
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        """Clear a stale stop; called once at the start of each line, never inside play()."""
        self._stop.clear()

    def play(self, pcm: bytes, rate: int) -> bool:
        import array

        import miniaudio

        data = np.frombuffer(pcm, dtype=np.int16)
        if self.volume != 1.0:
            data = np.clip(data.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16)
        samples = array.array("h", data.tobytes())
        done = threading.Event()
        step = max(1, rate * CHUNK_MS // 1000)

        def gen() -> Any:
            pos = 0
            required = yield b""
            while pos < len(samples) and not self._stop.is_set():
                n = min(required, step)
                chunk = samples[pos : pos + n]
                pos += n
                required = yield chunk
            done.set()

        try:
            dev = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate
            )
        except miniaudio.MiniaudioError:
            log.exception("no playback device")
            return False
        g = gen()
        next(g)
        dev.start(g)
        end = time.monotonic() + len(samples) / rate + 1.0
        while not done.is_set() and not self._stop.is_set() and time.monotonic() < end:
            time.sleep(0.005)
        if not self._stop.is_set():
            time.sleep(0.1)  # let the device drain
        dev.close()
        return not self._stop.is_set()


# --------------------------------------------------------------------------- helpers


def split_sentences(text: str) -> list[str]:
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < 20:
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def shorten(text: str, max_sentences: int, max_chars: int) -> str:
    """Keep spoken lines short: at most N sentences and max_chars characters."""
    text = " ".join(text.split())
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:max_sentences])
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return out


def _first_words(name: str, n: int = 3) -> str:
    return " ".join(name.split()[:n])


@dataclass(eq=False)
class Item:
    text: str
    kind: str  # "answer" | "needs_you" | "status"
    session: str | None = None
    lang: str = "en"
    seq: int = 0
    replayed: bool = False  # a barge-in replay happens at most once
    final: str | None = None  # text with the name decision baked in, set at first play
    fold: int = 0  # >0: a "and N more updates" line
    repeat_of: bool = False
    sid: str | None = None  # source session id, for drop_from()
    at: float = 0.0  # arrival time (speaker clock), for the drop_from() cutoff


class Speaker:
    def __init__(
        self,
        synth: Callable[..., Any],
        player: Any,
        cfg: dict[str, Any] | None = None,
        tone_player: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wake: Any = None,
        claude_in_front: Callable[[], bool] | None = None,
    ) -> None:
        self.synth, self.player, self.tone_player = synth, player, tone_player or player
        self.cfg = cfg if cfg is not None else {k: dict(v) for k, v in _config.DEFAULTS.items()}
        self.clock, self.wake, self._front = clock, wake, claude_in_front
        self._cv = threading.Condition()
        self._q: list[Item] = []
        self._current: Item | None = None
        self._epoch = 0
        self._seq = 0
        self._hold = False
        self._muted = False
        self._last_session: str | None = None
        self._last_text = ""
        self._repeats: list[tuple[float, Item]] = []
        self._thread: threading.Thread | None = None
        self._speaking = False

    # ---- public ----
    def say(
        self,
        text: str,
        priority: str = "status",
        session: str | None = None,
        lang: str = "en",
        session_id: str | None = None,
    ) -> None:
        kind = "needs_you" if priority == "needs_you" else "status"
        sp = self.cfg["speech"]
        item = Item(
            shorten(text, int(sp["max_sentences"]), int(sp["max_chars"])),
            kind,
            session,
            lang,
            sid=session_id,
            at=self.clock(),
        )
        if not item.text:
            return
        with self._cv:
            self._enqueue(item)
            if kind == "needs_you":
                self._repeats.append(
                    (
                        self.clock() + REPEAT_AFTER_S,
                        Item(
                            item.text,
                            kind,
                            session,
                            lang,
                            repeat_of=True,
                            sid=session_id,
                            at=item.at,
                        ),
                    )
                )
                self._cut_status_locked()
            self._fold_locked()
            self._ensure_thread()
            self._cv.notify_all()

    def answer(self, text: str, lang: str = "en") -> None:
        if not text.strip():
            return
        with self._cv:
            self._enqueue(Item(text.strip(), "answer", None, lang))
            self._ensure_thread()
            self._cv.notify_all()

    def barge(self) -> None:
        """Cut the current line (requeued once, if genuinely cut) and hold the queue."""
        with self._cv:
            self._hold = True
            self._cut_locked(count_replay=True)

    def release(self) -> None:
        with self._cv:
            self._hold = False
            self._cv.notify_all()

    def quiet(self) -> None:
        with self._cv:
            self._epoch += 1
            self._q.clear()
            self._repeats.clear()
            self._current = None
        self.player.stop()
        with self._cv:
            self._cv.notify_all()

    def tone(self, name: str) -> None:
        freqs, ms = _TONES.get(name, _TONES["tick"])
        pcm = _tone(freqs, ms)
        threading.Thread(target=self.tone_player.play, args=(pcm, 24000), daemon=True).start()

    def set_mute(self, on: bool) -> None:
        with self._cv:
            self._muted = on
            self._cv.notify_all()

    def is_muted(self) -> bool:
        return self._muted

    def last_spoken(self) -> str:
        return self._last_text

    def drop_from(self, session_id: str, before: float | None = None) -> None:
        """Remove a session's lines (queued, held replay, repeat) and cut its current one.

        With `before`, only lines that arrived before that time go; lines from the session that
        arrived later (while the message was being recorded) still play.
        """

        def stale(i: Item) -> bool:
            return i.sid == session_id and (before is None or i.at < before)

        with self._cv:
            self._q = [i for i in self._q if not stale(i)]
            self._repeats = [r for r in self._repeats if not stale(r[1])]
            cur = self._current
            cut = cur is not None and stale(cur)
            if cut:
                self._epoch += 1
                self._current = None
        if cut:
            self.player.stop()
        with self._cv:
            self._cv.notify_all()

    def clear_repeat(self, session: str | None) -> None:
        """A session sent Stop or was delivered to: its pending needs_you repeat is cancelled."""
        with self._cv:
            self._repeats = [r for r in self._repeats if r[1].session != session]

    def pending(self) -> int:
        with self._cv:
            return len(self._q) + (1 if self._current else 0)

    # ---- queue (call with the lock held) ----
    def _enqueue(self, item: Item) -> None:
        self._seq += 1
        item.seq = self._seq
        self._q.append(item)

    def _cut_locked(self, count_replay: bool) -> None:
        cur = self._current
        self._epoch += 1
        self._current = None
        if cur is not None and (not count_replay or not cur.replayed):
            cur.replayed = cur.replayed or count_replay
            cur.seq = -self._seq - 1  # front of its class
            self._seq += 1
            self._q.append(cur)
        self.player.stop()

    def _cut_status_locked(self) -> None:
        if self._current is not None and self._current.kind == "status":
            self._cut_locked(count_replay=False)

    def _fold_locked(self) -> None:
        status = sorted((i for i in self._q if i.kind == "status"), key=lambda i: i.seq)
        if len(status) <= MAX_PENDING_STATUS:
            return
        old = status[: len(status) - (MAX_PENDING_STATUS - 1)]
        n = sum(i.fold or 1 for i in old)
        for i in old:
            self._q.remove(i)
        fold = Item(f"and {n} more updates. Say status to hear them.", "status", None, "en", fold=n)
        fold.seq = old[0].seq
        self._q.append(fold)

    def _next_locked(self) -> Item | None:
        if not self._q or self._hold or self._muted:
            return None
        best = min(self._q, key=lambda i: (_RANK[i.kind], i.seq))
        self._q.remove(best)
        return best

    def _due_repeats_locked(self) -> None:
        now = self.clock()
        due = [r for r in self._repeats if r[0] <= now]
        for r in due:
            self._repeats.remove(r)
            self._enqueue(
                Item(
                    r[1].text,
                    "needs_you",
                    r[1].session,
                    r[1].lang,
                    repeat_of=True,
                    sid=r[1].sid,
                    at=r[1].at,
                )
            )

    # ---- playback thread ----
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="speech")
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cv:
                while True:
                    self._due_repeats_locked()
                    item = self._next_locked()
                    if item is not None:
                        break
                    self._set_speaking(False)
                    wait = None
                    if self._repeats:
                        wait = max(0.05, min(r[0] for r in self._repeats) - self.clock())
                    self._cv.wait(wait)
                self._current = item
                epoch = self._epoch
                self._set_speaking(True)
                if item.final is None:
                    item.final = self._decorate(item)
                self._last_text = item.final
            try:
                self._speak(item, epoch)
            except Exception:
                log.exception("speak failed")
            with self._cv:
                if self._current is item and self._epoch == epoch:
                    self._current = None

    def _set_speaking(self, on: bool) -> None:
        if on == self._speaking:
            return
        self._speaking = on
        if self.wake is not None:
            try:
                (self.wake.pause if on else self.wake.resume)()
            except Exception:
                log.exception("wake pause/resume failed")

    def _decorate(self, item: Item) -> str:
        """Name only when the speaker changes (6.1); decided once so a replay is identical."""
        text = item.text
        if item.session and item.session != self._last_session and item.kind != "answer":
            text = f"{_first_words(item.session)}. {text[:1].upper()}{text[1:]}"
        else:
            text = text[:1].upper() + text[1:]
        if item.session and item.kind != "answer":
            self._last_session = item.session
        return text

    def _speak(self, item: Item, epoch: int) -> None:
        text = item.final or item.text
        reset = getattr(self.player, "reset", None)
        if reset is not None:
            reset()
        if (
            item.kind != "answer"
            and not self.cfg["speech"]["speak_when_claude_focused"]
            and self._front is not None
            and self._front()
        ):
            self.tone_player.play(_tone(*_TONES["chime"]), 24000)
            return
        if item.kind == "needs_you" and not item.repeat_of:
            self.player.play(_tone(*_TONES["needs_you"]), 24000)
        sentences = split_sentences(text)
        q: queue.Queue[tuple[bytes, int] | None] = queue.Queue(maxsize=4)

        def alive() -> bool:
            return self._epoch == epoch

        def produce() -> None:
            try:
                for s in sentences:
                    if not alive():
                        return
                    gen = (
                        self.synth(s, self.cfg)
                        if item.lang == "en"
                        else self.synth(s, self.cfg, lang=item.lang)
                    )
                    for pcm, rate in gen:
                        while alive():
                            try:
                                q.put((pcm, rate), timeout=0.02)
                                break
                            except queue.Full:
                                continue
            except Exception:
                log.exception("synth failed")
            finally:
                while alive():
                    try:
                        q.put(None, timeout=0.02)
                        break
                    except queue.Full:
                        continue

        threading.Thread(target=produce, daemon=True, name="synth").start()
        while alive():
            try:
                got = q.get(timeout=0.02)
            except queue.Empty:
                continue
            if got is None:
                return
            if not alive():
                return
            self.player.play(*got)


# --------------------------------------------------------------------------- module API

_cfg: dict[str, Any] | None = None
_default_speaker: Speaker | None = None
_dlock = threading.Lock()


def configure(cfg: dict[str, Any]) -> None:
    global _cfg, _default_speaker
    with _dlock:
        _cfg = cfg
        _default_speaker = None


def _default() -> Speaker:
    global _default_speaker
    with _dlock:
        if _default_speaker is None:
            from relayvox import tts

            cfg = _cfg if _cfg is not None else {k: dict(v) for k, v in _config.DEFAULTS.items()}
            vol = float(cfg["speech"]["volume"])
            _default_speaker = Speaker(
                tts.synth,
                MiniPlayer(vol),
                cfg,
                tone_player=MiniPlayer(vol),
                wake=_LazyWake(),
                claude_in_front=_claude_in_front,
            )
        return _default_speaker


class _LazyWake:
    def pause(self) -> None:
        from relayvox import wake

        wake.pause()

    def resume(self) -> None:
        from relayvox import wake

        wake.resume()


def _claude_in_front() -> bool:
    import importlib

    return bool(importlib.import_module("relayvox.platform").claude_in_front())


def say(
    text: str,
    priority: str = "status",
    session: str | None = None,
    lang: str = "en",
    session_id: str | None = None,
) -> None:
    """Queue a line; "{name}. " is prefixed only on speaker change (section 6.1, 6.2).

    session is the display name; session_id tags the line for drop_from().
    """
    _default().say(text, priority, session, lang, session_id)


def drop_from(session_id: str, before: float | None = None) -> None:
    """After delivering to a session: drop its lines that arrived before `before` (6.2)."""
    _default().drop_from(session_id, before)


def answer(text: str, lang: str = "en") -> None:
    """Play ahead of queued updates; the caller includes the name (section 6.2, 8.7)."""
    sp = _default()
    from relayvox import tts

    note = tts.language_note(lang, sp.cfg)
    if note:
        sp.answer(note, "en")
    sp.answer(text, lang)


def barge() -> None:
    """Cut the current line, requeue it at the front, hold the queue (section 6.2)."""
    _default().barge()


def release() -> None:
    """Recording over: play held items, needs_you first (section 6.2)."""
    _default().release()


def quiet() -> None:
    """Stop the current line and clear the queue: tap of talk key, "be quiet" (section 6.2)."""
    _default().quiet()


def tone(name: str) -> None:
    """Play "tick", "send", "chime", "needs_you" or "error" (section 6.2)."""
    _default().tone(name)


def set_mute(on: bool) -> None:
    _default().set_mute(on)


def is_muted() -> bool:
    return _default().is_muted()


def last_spoken() -> str:
    return _default().last_spoken()


def clear_repeat(session: str | None) -> None:
    _default().clear_repeat(session)


def pending() -> int:
    return _default().pending()
