"""Platform layer for linux: the contract in DESIGN section 11 (details in section 3).

UNTESTED ON HARDWARE. Written and unit-tested (key map, tool discovery) on a Windows box only.

X11: global keys through the RECORD extension (python-xlib), typing with xdotool.
Wayland: there is no global key listener (`start_key_listener` returns False, bind
`relayvox ptt` in the compositor, section 5.4); typing works only with `wtype`.
External tools are found with shutil.which and degrade to False plus one clear log line.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("relayvox")

# Section 5.3 key names -> X keysyms. Right Alt is Alt_R on US layouts and
# ISO_Level3_Shift (AltGr) on many others, so it matches both.
_F1 = 0xFFBE
KEYSYMS: dict[str, tuple[int, ...]] = {
    "right_ctrl": (0xFFE4,),
    "left_ctrl": (0xFFE3,),
    "right_alt": (0xFFEA, 0xFE03),
    "left_alt": (0xFFE9,),
    "right_shift": (0xFFE2,),
    "right_cmd": (0xFFEC,),  # Super_R
    "right_option": (0xFFEA, 0xFE03),
    "pause": (0xFF13,),
    "scrolllock": (0xFF14,),
    "insert": (0xFF63,),
    **{f"f{n}": (_F1 + n - 1,) for n in range(1, 25)},
}
BUTTONS = {"x1": 8, "x2": 9}  # X side buttons (Back, Forward)
_ESCAPE = 0xFF1B
_AUTOREPEAT_WINDOW = 0.02  # s; X auto-repeat is a release+press pair with one timestamp
_TOOL_TIMEOUT = 5.0
_warned: set[str] = set()


def build_bindings(keys: dict[str, str]) -> tuple[dict[int, str], dict[int, str]]:
    """Return (keysym -> binding, button -> binding). Unknown names raise ValueError (5.3)."""
    by_sym: dict[int, str] = {}
    by_button: dict[int, str] = {}
    for binding, name in keys.items():
        name = name.strip().lower()
        if not name:
            continue
        if name in BUTTONS:
            by_button[BUTTONS[name]] = binding
        elif name in KEYSYMS:
            for sym in KEYSYMS[name]:
                by_sym[sym] = binding
        else:
            raise ValueError(f"unknown key name {name!r} for {binding!r}")
    return by_sym, by_button


def session_type() -> str:
    """'wayland', 'x11' or 'none' from the session environment."""
    kind = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if kind == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if kind == "x11" or os.environ.get("DISPLAY"):
        return "x11"
    return "none"


def find_tool(name: str) -> str | None:
    """Path of an external tool, or None."""
    return shutil.which(name)


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def _typing_tool() -> str | None:
    """wtype on Wayland, xdotool on X11, falling back to whichever exists."""
    order = ("wtype", "xdotool") if session_type() == "wayland" else ("xdotool", "wtype")
    for name in order:
        if find_tool(name):
            return name
    _warn_once("type", "typing needs xdotool (X11) or wtype (Wayland); install one")
    return None


def _clipboard_tool() -> str | None:
    for name in ("wl-copy", "xclip"):
        if find_tool(name):
            return name
    _warn_once("clip", "clipboard needs wl-copy (Wayland) or xclip (X11); install one")
    return None


def _run(argv: list[str], data: str | None = None, timeout: float = _TOOL_TIMEOUT) -> bool:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            input=data.encode() if data is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", argv[0], type(exc).__name__)
        return False
    return done.returncode == 0


class _Reducer:
    """Reduces raw events to (binding | "other" | "esc", is_down). Keeps no key codes (inv. 4).

    Drops auto-repeat: a bound key's release is held for a moment and cancelled if a press
    with the same timestamp follows.
    """

    def __init__(
        self,
        by_sym: dict[int, str],
        by_button: dict[int, str],
        sink: Callable[[str, bool], None],
    ) -> None:
        self._sym = by_sym
        self._btn = by_button
        self._sink = sink
        self._down: set[str] = set()
        self._lock = threading.Lock()
        self._pending: tuple[str, int, threading.Timer] | None = None

    def key(self, keysym: int, is_down: bool, stamp: int) -> None:
        name = self._sym.get(keysym)
        if name is None:
            self._emit("esc" if keysym == _ESCAPE else "other", is_down)
        else:
            self._bound(name, is_down, stamp)

    def button(self, detail: int, is_down: bool, stamp: int) -> None:
        name = self._btn.get(detail)
        if name is not None:
            self._bound(name, is_down, stamp)

    def _bound(self, name: str, is_down: bool, stamp: int) -> None:
        with self._lock:
            pending = self._pending
            if pending and pending[0] == name and pending[1] == stamp and is_down:
                pending[2].cancel()  # auto-repeat pair: swallow both halves
                self._pending = None
                return
            self._flush_locked()
            if is_down:
                if name in self._down:
                    return
                self._down.add(name)
                self._sink(name, True)
                return
            if name not in self._down:
                return
            timer = threading.Timer(_AUTOREPEAT_WINDOW, self._flush)
            timer.daemon = True
            self._pending = (name, stamp, timer)
            timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._pending is None:
            return
        name, _, timer = self._pending
        timer.cancel()
        self._pending = None
        self._down.discard(name)
        self._sink(name, False)

    def _emit(self, name: str, is_down: bool) -> None:
        with self._lock:
            self._flush_locked()
        self._sink(name, is_down)


def start_key_listener(keys: dict[str, str], sink: Callable[[str, bool], None]) -> bool:
    """Listen for keys {talk, dictate, mouse}; sink(binding|"other"|"esc", is_down) (3, 5)."""
    by_sym, by_button = build_bindings(keys)  # ValueError on a bad name, before anything else
    if session_type() != "x11":
        _warn_once(
            "keys",
            "no global key listener here (Wayland or no display): "
            "bind `relayvox ptt` in your compositor",
        )
        return False
    if importlib.util.find_spec("Xlib") is None:
        _warn_once("xlib", "python-xlib is not installed; no key listener")
        return False
    try:
        return _start_record(by_sym, by_button, sink)
    except Exception as exc:  # noqa: BLE001 - any X failure means no listener
        log.warning("key listener failed: %s", type(exc).__name__)
        return False


def _start_record(
    by_sym: dict[int, str], by_button: dict[int, str], sink: Callable[[str, bool], None]
) -> bool:
    from Xlib import X, display
    from Xlib.ext import record
    from Xlib.protocol import rq

    ctrl = display.Display()
    rec = display.Display()
    if not rec.has_extension("RECORD"):
        _warn_once("record", "the X server has no RECORD extension; no key listener")
        return False
    reducer = _Reducer(by_sym, by_button, sink)
    ctx = rec.record_create_context(
        0,
        [record.AllClients],
        [
            {
                "core_requests": (0, 0),
                "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0),
                "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.KeyPress, X.ButtonRelease),
                "errors": (0, 0),
                "client_started": False,
                "client_died": False,
            }
        ],
    )

    def handle(reply: Any) -> None:
        if reply.category != record.FromServer or reply.client_swapped or not reply.data:
            return
        data = reply.data
        while data:
            event, data = rq.EventField(None).parse_binary_value(data, rec.display, None, None)
            down = event.type in (X.KeyPress, X.ButtonPress)
            if event.type in (X.KeyPress, X.KeyRelease):
                reducer.key(ctrl.keycode_to_keysym(event.detail, 0), down, event.time)
            else:
                reducer.button(event.detail, down, event.time)

    threading.Thread(
        target=rec.record_enable_context, args=(ctx, handle), name="relay-xrecord", daemon=True
    ).start()
    return True


def on_arm(binding: str) -> None:
    """Windows: menu-mask key for Alt bindings; elsewhere no-op (section 5.3)."""


def claude_in_front() -> bool:
    """True if the Claude desktop app is the foreground app (section 3)."""
    return False  # there is no desktop app on Linux


def remember_front() -> object:
    """Handle of the foreground window/app (section 3)."""
    return None


def restore_front(handle: object) -> None:
    """Re-activate the window returned by remember_front (section 3)."""


def open_url(url: str) -> bool:
    """Open a claude:// URL; refuse anything else (section 3). Not used on Linux."""
    if not url.startswith("claude://"):
        return False
    opener = find_tool("xdg-open")
    return opener is not None and _run([opener, url])


def type_text(text: str) -> bool:
    """Type text into the focused window (section 3)."""
    tool = _typing_tool()
    if tool == "wtype":
        argv = ["wtype", "--", text]
    elif tool == "xdotool":
        argv = ["xdotool", "type", "--clearmodifiers", "--", text]
    else:
        return False
    return _run(argv, timeout=_TOOL_TIMEOUT + len(text) / 20)


def press_enter() -> bool:
    """Press Enter (section 3)."""
    tool = _typing_tool()
    if tool == "wtype":
        return _run(["wtype", "-k", "Return"])
    if tool == "xdotool":
        return _run(["xdotool", "key", "--clearmodifiers", "Return"])
    return False


def copy_text(text: str) -> bool:
    """Put text on the clipboard, dictation fallback only (section 3)."""
    tool = _clipboard_tool()
    if tool == "wl-copy":
        return _run(["wl-copy"], data=text)
    if tool == "xclip":
        return _run(["xclip", "-selection", "clipboard"], data=text)
    return False


def permissions() -> dict[str, str]:
    """Permission status for doctor, e.g. {"input_monitoring": "missing"} (section 3)."""
    kind = session_type()
    out = {"session": kind}
    if kind == "x11":
        out["key_listener"] = "ok" if importlib.util.find_spec("Xlib") else "missing python-xlib"
    else:
        out["key_listener"] = "unavailable (bind `relayvox ptt` in the compositor)"
    typing = [t for t in ("xdotool", "wtype") if find_tool(t)]
    out["typing"] = typing[0] if typing else "missing (install xdotool or wtype)"
    clip = [t for t in ("wl-copy", "xclip") if find_tool(t)]
    out["clipboard"] = clip[0] if clip else "missing (install wl-copy or xclip)"
    out["espeak_ng"] = "ok" if find_tool("espeak-ng") else "missing (system voice fallback)"
    return out
