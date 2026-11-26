"""Desktop app and tmux delivery, sanitizing (DESIGN section 4). Called only from app.py.

send() owns the 0.6 s cancel window (section 8.6): the app plays the send tick, then calls
send(), and must NOT wait again. cancel() (tap, Esc, cancel word) aborts a window that is open.
Extras beyond the contract: configure(cfg), cancel(), CANCELLED.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from typing import Any

from relayvox import config as _config
from relayvox.route import Session

log = logging.getLogger("relayvox.deliver")

CANCELLED = "Cancelled."
MAX_CHARS = 2000
CHUNK = 32
_HOST_ID = re.compile(r"local_[A-Za-z0-9_-]{1,128}")
_PANE_CMDS = {"claude", "node"}

_cfg: dict[str, Any] = {k: dict(v) for k, v in _config.DEFAULTS.items()}
_sleep: Callable[[float], None] = time.sleep
_now: Callable[[], float] = time.monotonic
_lock = threading.Lock()
_cancel_evt = threading.Event()
_window_open = False


def configure(cfg: dict[str, Any]) -> None:
    global _cfg
    _cfg = cfg


def _platform() -> Any:
    from relayvox import platform

    return platform


def _desktop_os() -> bool:
    return sys.platform in ("win32", "darwin")


def sanitize(text: str) -> str:
    """NFC-normalize, replace control chars with spaces, collapse spaces, cap at 2000 (sec 4)."""
    t = unicodedata.normalize("NFC", text)
    # Cc controls (ESC, newlines), Cf (bidi, zero-width), Zl/Zp (U+2028/2029) all become spaces
    t = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in t)
    return re.sub(r" {2,}", " ", t).strip()[:MAX_CHARS]


def cancel() -> None:
    """Abort a send that is still inside its cancel window; a no-op otherwise."""
    with _lock:
        if _window_open:
            _cancel_evt.set()


def _cancel_window() -> bool:
    """Wait routing.cancel_sec; True if cancelled during it."""
    global _window_open
    with _lock:
        _cancel_evt.clear()
        _window_open = True
    try:
        return _cancel_evt.wait(float(_cfg["routing"]["cancel_sec"]))
    finally:
        with _lock:
            _window_open = False


def send(session: Session, text: str) -> str | None:
    """Deliver text; None on success, else a short spoken reason (section 4)."""
    text = sanitize(text)
    if not text:
        return "Nothing to send."
    if _cancel_window():
        return CANCELLED
    if session.host_id and _HOST_ID.fullmatch(session.host_id) and _desktop_os():
        return _send_desktop(session.host_id, text)
    pane = _tmux_pane(session)
    if pane is not None:
        return _send_tmux(session, pane, text)
    return f"{session.name} is in a terminal I can't type into. Start it inside tmux to talk to it."


# --------------------------------------------------------------------------- desktop


def _wait_front(p: Any, seconds: float) -> bool:
    end = _now() + seconds
    while True:
        if p.claude_in_front():
            return True
        if _now() >= end:
            return False
        _sleep(0.1)


def _send_desktop(host_id: str, text: str) -> str | None:
    p = _platform()
    was_claude = bool(p.claude_in_front())
    handle = p.remember_front()
    if not p.open_url(f"claude://claude.ai/epitaxy/{host_id}"):
        return "I couldn't open Claude."
    if not _wait_front(p, 4.0):
        return "Claude didn't come to the front."
    _sleep(0.9)  # let the session view load
    for i in range(0, len(text), CHUNK):
        if not p.claude_in_front():
            return "Focus moved, not sent."
        if not p.type_text(text[i : i + CHUNK]):
            return "I couldn't type that."
    if not p.claude_in_front():
        return "Focus moved, not sent."
    if not p.press_enter():
        return "I couldn't press enter."
    if not was_claude:
        p.restore_front(handle)
    return None


# --------------------------------------------------------------------------- tmux


def _tmux(args: list[str]) -> str | None:
    exe = shutil.which("tmux")
    if not exe:
        return None
    try:
        r = subprocess.run(  # noqa: S603 - argument list, executable from which()
            [exe, *args], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _pid_chain(pid: int) -> set[int]:
    import psutil

    try:
        return {pid, *(p.pid for p in psutil.Process(pid).parents())}
    except psutil.Error:
        return {pid}


def _panes(session: Session) -> list[tuple[str, str]]:
    out = _tmux(["list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_current_command}"])
    if not out:
        return []
    chain = _pid_chain(session.pid)
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) in chain:
            found.append((parts[0], parts[2]))
    return found


def _tmux_pane(session: Session) -> str | None:
    """The one pane that runs this session, or None (none or ambiguous)."""
    panes = _panes(session)
    return panes[0][0] if len(panes) == 1 else None


def _send_tmux(session: Session, pane: str, text: str) -> str | None:
    again = _panes(session)  # just before sending: still the same pane, still running claude
    if len(again) != 1 or again[0][0] != pane or again[0][1] not in _PANE_CMDS:
        return "That pane changed, not sent."
    if _tmux(["send-keys", "-t", pane, "-l", "--", text]) is None:
        return "tmux didn't take it."
    if _tmux(["send-keys", "-t", pane, "Enter"]) is None:
        return "tmux didn't take it."
    return None
