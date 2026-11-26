"""Platform layer for windows: the contract in DESIGN section 11 (details in section 3).

Keys: a WH_KEYBOARD_LL hook (plus WH_MOUSE_LL when a side button is bound) on its own thread.
The callback only classifies the event and calls the sink; every key is passed on unchanged, so
Right Ctrl+C still copies and AltGr letters still type. Only the bound mouse side button is
swallowed. Events we inject ourselves (typing, the menu-mask key) are flagged and skipped.
Typing uses SendInput with KEYEVENTF_UNICODE, so delivery never touches the clipboard.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

# Section 5.3 names -> virtual-key codes. The low-level hook reports left/right variants.
_VK: dict[str, int] = {
    "right_ctrl": 0xA3,
    "left_ctrl": 0xA2,
    "right_alt": 0xA5,
    "left_alt": 0xA4,
    "right_shift": 0xA1,
    "right_cmd": 0x5C,  # Right Windows key
    "right_option": 0xA5,
    "pause": 0x13,
    "scrolllock": 0x91,
    "insert": 0x2D,
    **{f"f{n}": 0x70 + n - 1 for n in range(1, 25)},
}
_BUTTONS = {"x1": 1, "x2": 2}  # XBUTTON1 (Back), XBUTTON2 (Forward)
_ALT_NAMES = ("right_alt", "left_alt", "right_option")
_VK_ESC = 0x1B
_VK_MASK = 0xE8  # unassigned key: injected after Alt so a lone Alt release opens no menu
_FAKE_ALTGR_SCAN = 0x21D  # Windows sends a fake Left Ctrl with this scancode along with AltGr
_LLKHF_INJECTED = 0x10
_LLMHF_INJECTED = 0x01
_MAX_UNITS = 32  # UTF-16 units per SendInput batch (section 3)

_armed_alt: set[str] = set()  # bindings whose key is an Alt key (set by start_key_listener)


def resolve_keys(keys: dict[str, str]) -> tuple[dict[int, str], dict[int, str]]:
    """Return (vk -> binding, xbutton -> binding). Empty is off; unknown names raise (5.3)."""
    by_vk: dict[int, str] = {}
    by_button: dict[int, str] = {}
    for binding, raw in keys.items():
        name = raw.strip().lower()
        if not name:
            continue
        if name in _BUTTONS:
            by_button[_BUTTONS[name]] = binding
        elif name in _VK:
            by_vk[_VK[name]] = binding
        else:
            raise ValueError(f"unknown key name for keys.{binding}: {raw!r}")
    return by_vk, by_button


def classify_key(
    vk: int, scan: int, flags: int, down: bool, by_vk: dict[int, str]
) -> tuple[str, bool] | None:
    """Map one low-level key event to a sink call, or None to ignore it (5.2, 5.3)."""
    if flags & _LLKHF_INJECTED:
        return None
    if vk == 0xA2 and scan == _FAKE_ALTGR_SCAN:
        return None
    binding = by_vk.get(vk)
    if binding is not None:
        return binding, down
    if vk == _VK_ESC:
        return "esc", down
    return ("other", True) if down else None


def classify_mouse(
    msg: int, data: int, flags: int, by_button: dict[int, str]
) -> tuple[str, bool] | None:
    """Map one low-level mouse event to a sink call for a bound side button, else None."""
    if flags & _LLMHF_INJECTED or msg not in (0x20B, 0x20C):
        return None
    binding = by_button.get((data >> 16) & 0xFFFF)
    return None if binding is None else (binding, msg == 0x20B)


def chunk_text(text: str, limit: int = _MAX_UNITS) -> list[str]:
    """Split into pieces of at most `limit` UTF-16 units, never inside a surrogate pair."""
    pieces: list[str] = []
    cur: list[str] = []
    units = 0
    for ch in text:
        n = 2 if ord(ch) > 0xFFFF else 1
        if units + n > limit:
            pieces.append("".join(cur))
            cur, units = [], 0
        cur.append(ch)
        units += n
    if cur:
        pieces.append("".join(cur))
    return pieces


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LRESULT = ctypes.c_ssize_t
    _HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    _u32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
    _u32.SetWindowsHookExW.restype = ctypes.c_void_p
    _u32.CallNextHookEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _u32.CallNextHookEx.restype = _LRESULT
    _u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _u32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _u32.GetForegroundWindow.restype = ctypes.c_void_p
    _u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _u32.IsWindow.argtypes = [ctypes.c_void_p]
    _u32.IsIconic.argtypes = [ctypes.c_void_p]
    _u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    _u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    _u32.SetClipboardData.restype = ctypes.c_void_p
    _u32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t]
    _k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _k32.GetModuleHandleW.restype = ctypes.c_void_p
    _k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _k32.GlobalAlloc.restype = ctypes.c_void_p
    _k32.GlobalLock.argtypes = [ctypes.c_void_p]
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _k32.GlobalFree.argtypes = [ctypes.c_void_p]
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]

    class _KbdLL(ctypes.Structure):
        _fields_ = [
            ("vk", wintypes.DWORD),
            ("scan", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class _MouseLL(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("data", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class _KeyInput(ctypes.Structure):
        _fields_ = [
            ("vk", wintypes.WORD),
            ("scan", wintypes.WORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class _MouseInput(ctypes.Structure):  # only here to give the union its real size
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("data", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class _InputUnion(ctypes.Union):
        _fields_ = [("ki", _KeyInput), ("mi", _MouseInput)]

    class _Input(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]

    _u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    _u32.SendInput.restype = wintypes.UINT

    _KEYUP, _UNICODE = 0x0002, 0x0004
    _hook_lock = threading.Lock()
    _hook_state: dict[str, object] = {}  # keeps the callbacks alive; "sink" is swappable

    def _send(items: list[tuple[int, int, int]]) -> bool:
        """SendInput a list of (vk, scan, flags) key events; True if all were accepted."""
        arr = (_Input * len(items))()
        for i, (vk, scan, flags) in enumerate(items):
            arr[i].type = 1
            arr[i].ki = _KeyInput(vk, scan, flags, 0, 0)
        return int(_u32.SendInput(len(items), arr, ctypes.sizeof(_Input))) == len(items)

    def _emit(result: tuple[str, bool] | None) -> None:
        sink = _hook_state.get("sink")
        if result is not None and callable(sink):
            sink(*result)

    def _kb_proc(code: int, wparam: int, lparam: int) -> int:
        try:
            if code == 0 and wparam in (0x100, 0x101, 0x104, 0x105):
                ev = _KbdLL.from_address(lparam)
                by_vk: dict[int, str] = _hook_state["by_vk"]  # type: ignore[assignment]
                _emit(classify_key(ev.vk, ev.scan, ev.flags, wparam in (0x100, 0x104), by_vk))
        except Exception:  # noqa: S110 - a slow or raising hook gets removed by Windows
            pass
        return int(_u32.CallNextHookEx(None, code, wparam, lparam))

    def _mouse_proc(code: int, wparam: int, lparam: int) -> int:
        try:
            if code == 0 and wparam in (0x20B, 0x20C):
                ev = _MouseLL.from_address(lparam)
                by_button: dict[int, str] = _hook_state["by_button"]  # type: ignore[assignment]
                hit = classify_mouse(wparam, ev.data, ev.flags, by_button)
                if hit is not None:
                    _emit(hit)
                    return 1  # swallow the bound side button (down and up) so it is not Back
        except Exception:  # noqa: S110
            pass
        return int(_u32.CallNextHookEx(None, code, wparam, lparam))

    def _hook_thread(use_mouse: bool, ready: threading.Event, ok: list[bool]) -> None:
        kb_cb, mouse_cb = _HOOKPROC(_kb_proc), _HOOKPROC(_mouse_proc)
        _hook_state["callbacks"] = (kb_cb, mouse_cb)
        module = _k32.GetModuleHandleW(None)
        kb = _u32.SetWindowsHookExW(13, kb_cb, module, 0)
        ms = _u32.SetWindowsHookExW(14, mouse_cb, module, 0) if use_mouse else None
        ok.append(bool(kb) and (not use_mouse or bool(ms)))
        ready.set()
        if not ok[0]:
            for h in (kb, ms):
                if h:
                    _u32.UnhookWindowsHookEx(h)
            return
        msg = wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass

    def start_key_listener(keys: dict[str, str], sink: Callable[[str, bool], None]) -> bool:
        """Listen for keys {talk, dictate, mouse}; sink(binding|"other"|"esc", is_down) (3, 5)."""
        by_vk, by_button = resolve_keys(keys)
        with _hook_lock:
            _hook_state.update(by_vk=by_vk, by_button=by_button, sink=sink)
            _armed_alt.clear()
            _armed_alt.update(b for b, n in keys.items() if n.strip().lower() in _ALT_NAMES)
            if _hook_state.get("running"):
                return True
            ready, ok = threading.Event(), list[bool]()
            threading.Thread(
                target=_hook_thread, args=(bool(by_button), ready, ok), daemon=True
            ).start()
            ready.wait(5)
            if ok and ok[0]:
                _hook_state["running"] = True
                return True
            return False

    def on_arm(binding: str) -> None:
        """Windows: menu-mask key for Alt bindings; elsewhere no-op (section 5.3)."""
        if binding in _armed_alt:
            _send([(_VK_MASK, 0, 0), (_VK_MASK, 0, _KEYUP)])

    def _pid_of(hwnd: int | None) -> int:
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _proc_name(pid: int) -> str:
        h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            _k32.CloseHandle(h)

    def claude_in_front() -> bool:
        """True if the Claude desktop app is the foreground app (section 3)."""
        hwnd = _u32.GetForegroundWindow()
        return bool(hwnd) and _proc_name(_pid_of(hwnd)) == "claude.exe"

    def remember_front() -> object:
        """Handle of the foreground window/app (section 3)."""
        return _u32.GetForegroundWindow() or 0

    def restore_front(handle: object) -> None:
        """Re-activate the window returned by remember_front (section 3)."""
        if not isinstance(handle, int) or not handle or not _u32.IsWindow(handle):
            return
        if _u32.IsIconic(handle):
            _u32.ShowWindow(handle, 9)  # SW_RESTORE
        # A tapped Alt lets a background process take the foreground; the mask key keeps the
        # window's menu bar from opening.
        _send([(0xA4, 0, 0), (_VK_MASK, 0, 0), (_VK_MASK, 0, _KEYUP), (0xA4, 0, _KEYUP)])
        _u32.SetForegroundWindow(handle)

    def open_url(url: str) -> bool:
        """Open a claude:// URL; refuse anything else (section 3)."""
        if not url.startswith("claude://") or any(c <= " " or c == "\x7f" for c in url):
            return False
        try:
            os.startfile(url)  # noqa: S606 - scheme checked above, handled by the Claude app
        except OSError:
            return False
        return True

    def _modifiers_down() -> bool:
        return any(
            _u32.GetAsyncKeyState(vk) & 0x8000 for vk in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)
        )

    def type_text(text: str) -> bool:
        """Type text into the focused window (section 3)."""
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if not text:
            return True
        # Held modifiers would turn the characters into shortcuts; give them a moment to lift.
        deadline = time.monotonic() + 0.5
        while _modifiers_down() and time.monotonic() < deadline:
            time.sleep(0.01)
        for piece in chunk_text(text):
            data = piece.encode("utf-16-le")
            units = [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]
            events = [(0, u, _UNICODE | f) for u in units for f in (0, _KEYUP)]
            if not _send(events):
                return False
        return True

    def press_enter() -> bool:
        """Press Enter (section 3)."""
        return _send([(0x0D, 0, 0), (0x0D, 0, _KEYUP)])

    def copy_text(text: str) -> bool:
        """Put text on the clipboard, dictation fallback only (section 3)."""
        data = text.encode("utf-16-le") + b"\x00\x00"
        for _ in range(20):
            if _u32.OpenClipboard(None):
                break
            time.sleep(0.03)
        else:
            return False
        try:
            _u32.EmptyClipboard()
            h = _k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            if not h:
                return False
            p = _k32.GlobalLock(h)
            if not p:
                _k32.GlobalFree(h)
                return False
            ctypes.memmove(p, data, len(data))
            _k32.GlobalUnlock(h)
            if not _u32.SetClipboardData(13, h):  # CF_UNICODETEXT; the system owns h on success
                _k32.GlobalFree(h)
                return False
            return True
        finally:
            _u32.CloseClipboard()

    def _claude_elevated() -> str:
        """'yes' if a claude.exe runs elevated while Relay does not (UIPI blocks typing)."""
        import psutil  # imported here: only doctor needs it

        if ctypes.windll.shell32.IsUserAnAdmin():
            return "no"
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenProcessToken.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        adv.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        found = False
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info["name"] or "").lower() != "claude.exe":
                continue
            found = True
            proc = _k32.OpenProcess(0x1000, False, p.info["pid"])
            if not proc:
                return "yes"
            tok = ctypes.c_void_p()
            try:
                if not adv.OpenProcessToken(proc, 0x8, ctypes.byref(tok)):
                    return "yes"  # access denied to a same-user process means it is elevated
                elev, ret = wintypes.DWORD(), wintypes.DWORD()
                adv.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(ret))
                if elev.value:
                    return "yes"
            finally:
                if tok.value:
                    _k32.CloseHandle(tok)
                _k32.CloseHandle(proc)
        return "no" if found else "n/a"

    def permissions() -> dict[str, str]:
        """Permission status for doctor, e.g. {"input_monitoring": "missing"} (section 3)."""
        try:
            elevated = _claude_elevated()
        except Exception:
            elevated = "unknown"
        return {
            "input_monitoring": "ok" if _hook_state.get("running") else "not started",
            "typing": "ok",
            "claude_elevated": elevated,
        }
