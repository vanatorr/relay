"""Push-to-talk gesture machine (pure) + starts the platform key listener (DESIGN section 5)."""

from __future__ import annotations

import contextlib
import importlib
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

IDLE, PRESSED, HELD, TAP_WAIT, LATCHED = "idle", "pressed", "held", "tap_wait", "latched"
DEAD = "dead"  # key still down after a shortcut/discard/auto-send: wait for its release
# events: "down" | "up" | "other" | "esc" | "tick"
# actions: "barge", "quiet", "mic_on", "send", "discard", "cancel_pending"

HOLD_MS = 200  # a hold this long opens the mic
DOUBLE_TAP_MS = 350  # second press within this latches hands-free
MIN_AUDIO_MS = 300  # shorter recordings are discarded
MAX_REC_MS = 120_000  # recording longer than this stops and sends


@dataclass(frozen=True)
class Ptt:
    """State of one binding's gesture machine (section 5.2)."""

    state: str = IDLE
    since_ms: int = 0  # when this state began (press time, tap release time, or mic-open time)
    mic_open: bool = False


def _open(t_ms: int, state: str) -> tuple[Ptt, list[str]]:
    return Ptt(state, t_ms, True), ["barge", "mic_on"]


def _end(t_ms: int, s: Ptt, next_state: str) -> tuple[Ptt, list[str]]:
    """Recording ends: send it, or discard when shorter than 300 ms of audio."""
    action = "send" if t_ms - s.since_ms >= MIN_AUDIO_MS else "discard"
    return Ptt(next_state, t_ms, False), [action]


def step(state: Ptt, event: str, t_ms: int) -> tuple[Ptt, list[str]]:
    """Pure transition function with the clock passed in (section 5.2).

    A tap emits ["cancel_pending", "quiet"]: the caller skips "quiet" when a send was pending
    and got cancelled.
    """
    st = state.state
    if st == IDLE:
        if event == "down":
            return Ptt(PRESSED, t_ms), []
        return state, []
    if st == PRESSED:
        if event == "tick" and t_ms - state.since_ms >= HOLD_MS:
            return _open(t_ms, HELD)
        if event == "up":
            return Ptt(TAP_WAIT, t_ms), ["cancel_pending", "quiet"]
        if event in ("other", "esc"):
            return Ptt(DEAD, t_ms), []  # a shortcut: the mic never opened
        return state, []  # auto-repeat downs and early ticks
    if st == HELD:
        if event == "up":
            return _end(t_ms, state, IDLE)
        if event in ("other", "esc"):
            return Ptt(DEAD, t_ms), ["discard"]
        if event == "tick" and t_ms - state.since_ms >= MAX_REC_MS:
            return Ptt(DEAD, t_ms), ["send"]
        return state, []
    if st == TAP_WAIT:
        if event == "down" and t_ms - state.since_ms <= DOUBLE_TAP_MS:
            return _open(t_ms, LATCHED)
        if event == "down":
            return Ptt(PRESSED, t_ms), []
        if event in ("other", "esc") or (event == "tick" and t_ms - state.since_ms > DOUBLE_TAP_MS):
            return Ptt(), []
        return state, []
    if st == LATCHED:
        if event == "down":
            return _end(t_ms, state, IDLE)  # tap again to send
        if event == "esc":
            return Ptt(IDLE, t_ms), ["discard"]
        if event == "tick" and t_ms - state.since_ms >= MAX_REC_MS:
            return Ptt(IDLE, t_ms), ["send"]
        return state, []  # up, other keys: stay hands-free
    if st == DEAD:
        if event == "up":
            return Ptt(), []
        return state, []
    return Ptt(), []


def toggle(state: Ptt, t_ms: int) -> tuple[Ptt, list[str]]:
    """`relayvox ptt toggle`: behaves like a double-tap, then like the tap that sends."""
    if state.state == LATCHED:
        return step(state, "down", t_ms)
    if state.mic_open:
        return state, []
    return _open(t_ms, LATCHED)


def default_keys() -> dict[str, str]:
    """OS default talk and dictate keys (section 5.3)."""
    if sys.platform == "darwin":
        return {"talk": "right_option", "dictate": "right_cmd"}
    return {"talk": "right_ctrl", "dictate": "right_alt"}


def resolve_keys(cfg: Any) -> dict[str, str]:
    """keys section -> {"talk": name, "dictate": name, "mouse": name}; "" entries are dropped."""
    k = cfg["keys"]
    d = default_keys()
    out = {}
    for name in ("talk", "dictate"):
        v = k[name]
        v = d[name] if v == "auto" else v
        if v:
            out[name] = v
    if k.get("mouse"):
        out["mouse"] = k["mouse"]
    return out


_q: queue.Queue[tuple[str, str]] = queue.Queue()
_stop = threading.Event()
_machines: dict[str, Ptt] = {}


def _clock_ms() -> int:
    return int(time.monotonic() * 1000)


def sink(binding: str, is_down: bool) -> None:
    """Platform listener callback: reduce to (binding, event); never blocks (invariant 4)."""
    if binding == "other":
        _q.put(("*", "other" if is_down else "noop"))
    elif binding == "esc":
        _q.put(("*", "esc" if is_down else "noop"))
    else:
        _q.put((binding, "down" if is_down else "up"))


def inject(binding: str, command: str) -> None:
    """`relayvox ptt down|up|toggle`: same machine as the keyboard (section 5.4)."""
    if command in ("down", "up", "toggle"):
        _q.put((binding, command))


def _worker(on_action: Callable[[str, str], None], plat: Any) -> None:
    while not _stop.is_set():
        active = any(m.state != IDLE for m in _machines.values())
        try:
            binding, event = _q.get(timeout=0.02 if active else 0.5)
        except queue.Empty:
            binding, event = "*", "tick"
        t = _clock_ms()
        with contextlib.suppress(Exception):  # a bad callback must not kill the worker
            _handle(binding, event, t, on_action, plat)


def _handle(
    binding: str, event: str, t: int, on_action: Callable[[str, str], None], plat: Any
) -> None:
    if event == "noop":
        return
    if binding == "*":  # keys other than ours go to every active machine
        for b in list(_machines):
            if _machines[b].state != IDLE:
                _apply(b, event, t, on_action, plat)
        return
    _machines.setdefault(binding, Ptt())
    recording = next((b for b, m in _machines.items() if m.mic_open and b != binding), None)
    if recording is not None:  # only one binding records at a time
        if event in ("down", "toggle"):
            _apply(recording, "other", t, on_action, plat)
        return
    _apply(binding, event, t, on_action, plat)


def _apply(
    binding: str, event: str, t: int, on_action: Callable[[str, str], None], plat: Any
) -> None:
    prev = _machines[binding]
    if event == "toggle":
        new, actions = toggle(prev, t)
    else:
        new, actions = step(prev, event, t)
    _machines[binding] = new
    if prev.state == IDLE and new.state == PRESSED and plat is not None:
        with contextlib.suppress(Exception):
            plat.on_arm(binding)
    for a in actions:
        on_action(binding, a)


def start(cfg: Any, on_action: Callable[[str, str], None]) -> bool:
    """Start the platform key listener and the worker thread; False if none (section 5.4).

    on_action(binding, action) runs on the worker thread. The worker also serves inject().
    """
    plat = importlib.import_module("relayvox.platform")
    ok = bool(plat.start_key_listener(resolve_keys(cfg), sink))
    _stop.clear()
    _machines.clear()
    threading.Thread(target=_worker, args=(on_action, plat), name="ptt", daemon=True).start()
    return ok


def stop() -> None:
    """Stop the worker thread."""
    _stop.set()
