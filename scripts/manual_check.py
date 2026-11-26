"""Manual hardware checks per OS (DESIGN section 15). Run: python scripts/manual_check.py macos."""

from __future__ import annotations

import sys
import time


# --- macOS part (untested on hardware) ---------------------------------------------------------
def check_macos() -> int:
    """Permissions, a 5 s live key test for the default keys, typing into the focused window."""
    from relayvox.platform import macos

    print("permissions:", macos.permissions())
    seen: list[str] = []
    ok = macos.start_key_listener(
        {"talk": "right_option", "dictate": "right_cmd", "mouse": ""},
        lambda b, down: seen.append(f"{b} {'down' if down else 'up'}"),
    )
    print("listener started:", ok)
    print("Hold Right Option, then Right Command, now (5 s)...")
    time.sleep(5)
    print("events:", seen or "none (missing Input Monitoring, or no key press)")
    print("front is Claude:", macos.claude_in_front())
    print("Focus a text field; typing 'relay ok' in 3 s...")
    time.sleep(3)
    print("typed:", macos.type_text("relay ok"))
    return 0 if ok else 1


# ---- Windows part (verified on a Windows box) ----------------------------------------------
def check_windows() -> int:
    """Windows manual check; types only into its own tk window. Returns the failure count."""
    import os
    import tkinter

    from relayvox.platform import windows as w
    from relayvox.tts import system

    fails = 0

    def check(label: str, ok: bool, extra: object = "") -> None:
        nonlocal fails
        fails += 0 if ok else 1
        print(f"windows {'ok  ' if ok else 'FAIL'} {label} {extra}".rstrip())

    # Pure classification (the live hook skips injected keys, so these use fixtures).
    by_vk = {0xA3: "talk", 0xA5: "dictate"}
    check("rctrl down", w.classify_key(0xA3, 0x1D, 0, True, by_vk) == ("talk", True))
    check("rctrl+c is a shortcut", w.classify_key(0x43, 0x2E, 0, True, by_vk) == ("other", True))
    check("altgr fake ctrl ignored", w.classify_key(0xA2, 0x21D, 0, True, by_vk) is None)
    check("injected ignored", w.classify_key(0xA3, 0x1D, 0x10, True, by_vk) is None)
    check("esc", w.classify_key(0x1B, 1, 0, True, by_vk) == ("esc", True))
    check("emoji not split", all(len(p.encode("utf-16-le")) <= 64 for p in w.chunk_text("😀" * 40)))
    try:
        w.resolve_keys({"talk": "nope"})
        check("unknown key rejected", False)
    except ValueError:
        check("unknown key rejected", True)

    # Listener: an injected Right Ctrl tap must produce no sink call (injected events skipped).
    seen: list[tuple[str, bool]] = []
    started = w.start_key_listener(
        {"talk": "right_ctrl", "dictate": "right_alt", "mouse": ""},
        lambda b, d: seen.append((b, d)),
    )
    check("listener started", started)

    root = tkinter.Tk()
    root.geometry("500x200+200+200")
    text = tkinter.Text(root)
    text.pack(fill="both", expand=True)

    def pump(sec: float = 0.3) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            root.update()
            time.sleep(0.01)

    def mine() -> bool:
        pump(0.1)
        return w._pid_of(w._u32.GetForegroundWindow()) == os.getpid()

    root.attributes("-topmost", True)
    pump()
    root.focus_force()
    text.focus_force()
    pump()
    check("test window is foreground", mine())
    if mine():
        w._send([(0xA3, 0x1D, 1), (0xA3, 0x1D, 1 | 2)])
        pump()
        check("injected rctrl tap -> no sink call", not seen, seen)
        check("claude not in front", not w.claude_in_front())

        sample = "relay \u00e9\u20ac \U0001f600 " + "x" * 40 + " end\nline"
        check("type_text", w.type_text(sample))
        pump()
        got = text.get("1.0", "end-1c")
        check("typed text matches", got == sample.replace("\n", " "), repr(got))
        check("press_enter", w.press_enter())
        pump()
        check("enter inserted newline", text.get("1.0", "end-1c").endswith("\n"))

        # Focus round trip between two of our own windows.
        h = w.remember_front()
        other = tkinter.Toplevel(root)
        other.geometry("200x100+750+200")
        pump()
        other.focus_force()
        pump()
        w.restore_front(h)
        pump(0.4)
        check("restore_front returns focus", w._u32.GetForegroundWindow() == h)
        other.destroy()

        # Clipboard: text only, restored afterwards.
        try:
            old: str | None = root.clipboard_get()
        except tkinter.TclError:
            old = None
        if old is None:
            print("windows skip clipboard check (empty or non-text clipboard)")
        else:
            check("copy_text", w.copy_text("relay-check"))
            pump(0.1)
            check("clipboard holds text", root.clipboard_get() == "relay-check")
            root.clipboard_clear()
            root.clipboard_append(old)
            root.update()
    root.destroy()

    # open_url: refusals for real; the positive path with startfile stubbed (never opens Claude).
    check("open_url refuses https", not w.open_url("https://example.com"))
    check("open_url refuses spaces", not w.open_url("claude://a b"))
    called: list[str] = []
    real = os.startfile  # type: ignore[attr-defined]
    os.startfile = called.append  # type: ignore[attr-defined,assignment]
    try:
        check("open_url accepts claude://", w.open_url("claude://claude.ai/x") and called != [])
    finally:
        os.startfile = real  # type: ignore[attr-defined]

    print("windows claude_in_front (read-only):", w.claude_in_front())
    print("windows permissions:", w.permissions())

    # SAPI worker: cold start versus warm call.
    for label in ("cold", "warm"):
        t0 = time.monotonic()
        chunks = list(system._windows("Relay check, café.", {"tts": {"speed": 1.0}}))
        ms = round((time.monotonic() - t0) * 1000)
        pcm = b"".join(c for c, _ in chunks)
        check(f"sapi {label}", bool(pcm) and chunks[0][1] == 22050, f"{ms} ms, {len(pcm)} bytes")
    return fails


# ---- Linux part (untested on hardware) -----------------------------------------------------
def check_linux() -> int:
    """Linux manual check: session, tools and a live key listener. Returns the failure count."""
    import time

    from relayvox.platform import linux

    fails = 0
    for name, value in linux.permissions().items():
        print(f"linux {name}: {value}")
    seen: list[tuple[str, bool]] = []
    ok = linux.start_key_listener(
        {"talk": "right_ctrl", "dictate": "right_alt", "mouse": ""},
        lambda name, down: seen.append((name, down)),
    )
    if ok:
        print("Hold Right Ctrl for a second now.")
        time.sleep(5)
        talk = [e for e in seen if e[0] == "talk"]
        print(f"talk events seen: {len(talk)}")
        fails += 0 if talk else 1
    else:
        print("no key listener (Wayland or missing python-xlib): bind `relayvox ptt`")
    print("espeak-ng:", "ok" if linux.find_tool("espeak-ng") else "missing")
    return fails


# --- shared entry -------------------------------------------------------------------------------
if __name__ == "__main__":
    _which = sys.argv[1] if sys.argv[1:] else sys.platform
    if _which in ("macos", "darwin"):
        sys.exit(check_macos())
    if _which in ("windows", "win32"):
        sys.exit(check_windows())
    if _which.startswith("linux"):
        sys.exit(check_linux())
    print("usage: python scripts/manual_check.py [windows|macos|linux]")
    sys.exit(2)
