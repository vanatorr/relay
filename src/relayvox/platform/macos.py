"""Platform layer for macOS: the contract in DESIGN section 11 (details in section 3).

UNTESTED ON HARDWARE. Written on a Windows box; only the keycode maps, the text chunking and the
permission strings are unit-tested. Needs pyobjc (Quartz, Cocoa), imported lazily so the package
still imports on Windows and Linux.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from relayvox.platform import chord as chord_mod

# Bundle ids of the Claude desktop app (DESIGN section 3, config `desktop_app.bundle_ids`).
# The real id is unverified (section 3 "Not verifiable"); callers may extend this list.
BUNDLE_IDS: list[str] = ["com.anthropic.claudefordesktop"]

RETURN_KEYCODE = 36
ESCAPE_KEYCODE = 53
MAX_UNITS = 20  # CGEventKeyboardSetUnicodeString limit per event (UTF-16 units)

# Names from DESIGN 5.3 -> virtual keycodes. Right Option is 61 and Right Command is 54.
_F_CODES = [
    122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111,  # f1..f12
    105, 107, 113, 106, 64, 79, 80, 90,  # f13..f20
]  # fmt: skip
KEYCODES: dict[str, int] = {
    "right_ctrl": 62,
    "left_ctrl": 59,
    "right_alt": 61,
    "right_option": 61,
    "left_alt": 58,
    "right_shift": 60,
    "right_cmd": 54,
    "pause": 113,  # F15 sits where Pause/Break is on a PC board
    "scrolllock": 107,  # F14
    "insert": 114,  # Help
    **{f"f{i + 1}": c for i, c in enumerate(_F_CODES)},
}
# f21..f24 have no Mac keycodes; they are rejected at startup.

# Modifier keycode -> device-dependent flag bit in flagsChanged (IOLLEvent.h NX_DEVICE*KEYMASK).
MODIFIER_MASKS: dict[int, int] = {
    59: 0x1,  # left ctrl
    56: 0x2,  # left shift
    60: 0x4,  # right shift
    55: 0x8,  # left cmd
    54: 0x10,  # right cmd
    58: 0x20,  # left alt
    61: 0x40,  # right alt
    62: 0x2000,  # right ctrl
    57: 0,  # caps lock: a toggle, never a hold
    63: 0,  # fn / globe
}
# Virtual keycode -> canonical chord name (ANSI layout letters and digits, F-keys, modifiers).
_LETTER_CODES = (  # "_" marks punctuation and other keys that are not chord keys
    "a s d f h g z x c v _ b q w e r y t 1 2 3 4 6 5 _ 9 7 _ 8 0 _ o u _ i p _ "
    "l j _ k _ _ _ _ n m _"
)
CHORD_CODES: dict[int, str] = {
    56: "shift", 60: "shift", 59: "ctrl", 62: "ctrl", 58: "alt", 61: "alt", 55: "cmd", 54: "cmd",
    **{i: c for i, c in enumerate(_LETTER_CODES.split()) if c.isalnum()},
    **{c: f"f{i + 1}" for i, c in enumerate(_F_CODES)},
}  # fmt: skip
MOUSE_BUTTONS = {"x1": 3, "x2": 4}  # CGEvent "other" buttons: back, forward


def resolve_keys(keys: dict[str, str]) -> tuple[dict[int, str], dict[int, str]]:
    """Map {"talk": name, ...} to ({keycode: binding}, {mouse button: binding}).

    Empty names are off. Unknown names raise ValueError (DESIGN 5.3: rejected at startup).
    """
    by_code: dict[int, str] = {}
    by_button: dict[int, str] = {}
    for binding, name in keys.items():
        name = name.strip().lower()
        if not name:
            continue
        if binding == "mouse":
            if name not in MOUSE_BUTTONS:
                raise ValueError(f"unknown mouse button {name!r}")
            by_button[MOUSE_BUTTONS[name]] = binding
        elif name in KEYCODES:
            by_code[KEYCODES[name]] = binding
        else:
            raise ValueError(f"unknown key {name!r} for {binding} on macOS")
    return by_code, by_button


def chunk_utf16(text: str, limit: int = MAX_UNITS) -> list[str]:
    """Split text so each piece is at most `limit` UTF-16 units, never inside a surrogate pair."""
    out: list[str] = []
    cur: list[str] = []
    units = 0
    for ch in text:
        n = 2 if ord(ch) > 0xFFFF else 1
        if units + n > limit:
            out.append("".join(cur))
            cur, units = [], 0
        cur.append(ch)
        units += n
    if cur:
        out.append("".join(cur))
    return out


def perm_word(granted: bool | None) -> str:
    """True -> "ok", False -> "missing", None (cannot tell) -> "unknown"."""
    if granted is None:
        return "unknown"
    return "ok" if granted else "missing"


def mic_word(status: int | None) -> str:
    """AVAuthorizationStatus: 0 not asked, 1 restricted, 2 denied, 3 authorized."""
    return {3: "ok", 0: "not_asked", 1: "missing", 2: "missing"}.get(
        -1 if status is None else status, "unknown"
    )


def _quartz() -> Any:
    import Quartz

    return Quartz


def _ax_trusted() -> bool | None:
    try:
        import Quartz

        return bool(Quartz.AXIsProcessTrusted())
    except (ImportError, AttributeError):
        pass
    try:
        import ApplicationServices

        return bool(ApplicationServices.AXIsProcessTrusted())
    except (ImportError, AttributeError):
        return None


def start_key_listener(keys: dict[str, str], sink: Callable[[str, bool], None]) -> bool:
    """Listen for keys {talk, dictate, mouse}; sink(binding|"other"|"esc", is_down) (3, 5)."""
    chord = chord_mod.parse(keys.get("stop", ""))
    tracker = chord_mod.Tracker(chord) if chord else None
    by_code, by_button = resolve_keys({b: n for b, n in keys.items() if b != "stop"})
    try:
        Q = _quartz()
    except ImportError:
        return False

    if not Q.CGPreflightListenEventAccess():
        Q.CGRequestListenEventAccess()
        return False

    def handle(_proxy: Any, etype: int, event: Any, _refcon: Any) -> Any:
        if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            Q.CGEventTapEnable(tap, True)  # re-enable after the system disabled the tap
            return event
        if etype in (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp):
            button = Q.CGEventGetIntegerValueField(event, Q.kCGMouseEventButtonNumber)
            if button in by_button:
                sink(by_button[button], etype == Q.kCGEventOtherMouseDown)
                return None  # swallow the side button (needs the active tap)
            return event
        code = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
        if etype == Q.kCGEventFlagsChanged:
            mask = MODIFIER_MASKS.get(code)
            if mask is None:
                return event
            down = bool(mask and Q.CGEventGetFlags(event) & mask)
            if tracker is not None and code in CHORD_CODES:
                tracker.feed(CHORD_CODES[code], down)
            sink(by_code.get(code, "other"), down)
        elif etype in (Q.kCGEventKeyDown, Q.kCGEventKeyUp):
            if Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventAutorepeat):
                return event
            down = etype == Q.kCGEventKeyDown
            if (
                tracker is not None
                and code in CHORD_CODES
                and tracker.feed(CHORD_CODES[code], down)
            ):
                sink("stop", True)
            if code in by_code:
                sink(by_code[code], down)
            elif code == ESCAPE_KEYCODE:
                sink("esc", down)
            else:
                sink("other", down)
        return event  # observed, never swallowed (DESIGN 5.1)

    def bit(t: int) -> int:
        return int(Q.CGEventMaskBit(t))

    mask = bit(Q.kCGEventKeyDown) | bit(Q.kCGEventKeyUp) | bit(Q.kCGEventFlagsChanged)
    options = Q.kCGEventTapOptionListenOnly
    if by_button:
        mask |= bit(Q.kCGEventOtherMouseDown) | bit(Q.kCGEventOtherMouseUp)
        options = Q.kCGEventTapOptionDefault  # an active tap is needed to swallow the button

    tap = Q.CGEventTapCreate(
        Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, options, mask, handle, None
    )
    if tap is None and by_button:  # no Accessibility: fall back to keys only, listen-only
        mask = bit(Q.kCGEventKeyDown) | bit(Q.kCGEventKeyUp) | bit(Q.kCGEventFlagsChanged)
        tap = Q.CGEventTapCreate(
            Q.kCGSessionEventTap,
            Q.kCGHeadInsertEventTap,
            Q.kCGEventTapOptionListenOnly,
            mask,
            handle,
            None,
        )
    if tap is None:
        return False

    def run() -> None:
        source = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(tap, True)
        Q.CFRunLoopRun()

    threading.Thread(target=run, name="relay-keytap", daemon=True).start()
    return True


def on_arm(binding: str) -> None:
    """Windows only (menu-mask key for Alt bindings); no-op here (section 5.3)."""
    return None


def claude_in_front() -> bool:
    """True if the Claude desktop app is the foreground app (section 3)."""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return bool(app is not None and app.bundleIdentifier() in BUNDLE_IDS)
    except Exception:
        return False


def remember_front() -> object:
    """Handle of the foreground app (section 3)."""
    try:
        from AppKit import NSWorkspace

        return NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None


def restore_front(handle: object) -> None:
    """Re-activate the app returned by remember_front (section 3)."""
    if handle is None:
        return
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps

        handle.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)  # type: ignore[attr-defined]
    except Exception:
        return


def open_url(url: str) -> bool:
    """Open a claude:// URL; refuse anything else (section 3)."""
    if not url.startswith("claude://") or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        return subprocess.run(["open", url], timeout=5, check=False).returncode == 0  # noqa: S603, S607
    except (OSError, subprocess.SubprocessError):
        return False


def _post_key(Q: Any, keycode: int, text: str | None) -> None:
    for down in (True, False):
        ev = Q.CGEventCreateKeyboardEvent(None, keycode, down)
        if text is not None:
            Q.CGEventKeyboardSetUnicodeString(ev, len(text.encode("utf-16-le")) // 2, text)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)


def type_text(text: str) -> bool:
    """Type text into the focused window, at most 20 UTF-16 units per event (section 3)."""
    if _ax_trusted() is False:
        return False
    try:
        Q = _quartz()
        for piece in chunk_utf16(text):
            _post_key(Q, 0, piece)
    except Exception:
        return False
    return True


def press_enter() -> bool:
    """Press Return (keycode 36) (section 3)."""
    if _ax_trusted() is False:
        return False
    try:
        _post_key(_quartz(), RETURN_KEYCODE, None)
    except Exception:
        return False
    return True


def copy_text(text: str) -> bool:
    """Put text on the clipboard, dictation fallback only (section 3)."""
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString

        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.setString_forType_(text, NSPasteboardTypeString))
    except Exception:
        return False


def _mic_status() -> int | None:
    try:
        import AVFoundation

        return int(
            AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                AVFoundation.AVMediaTypeAudio
            )
        )
    except Exception:
        return None


def permissions() -> dict[str, str]:
    """Permission status for doctor; "binary" names the app the permissions attach to (3)."""
    try:
        listen: bool | None = bool(_quartz().CGPreflightListenEventAccess())
    except Exception:
        listen = None
    return {
        "input_monitoring": perm_word(listen),
        "accessibility": perm_word(_ax_trusted()),
        "microphone": mic_word(_mic_status()),
        "binary": sys.executable,
    }
