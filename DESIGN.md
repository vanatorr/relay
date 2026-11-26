# Relay v1 design

Status: approved for build. Audience: the engineers building v1. The current single-file `relay.py` (Windows only) is the reference for behaviour that already works. This document says what to keep, what to change and where each piece lives.

Relay is a local voice layer for Claude Code. Sessions speak short status lines; the user holds a key, speaks, and Relay delivers the words to a session as real user input. **Relay never acts on its own.** It only speaks what sessions report and delivers what the user said (see Invariants).

---

## 0. Decisions at a glance

| Topic | Decision |
|---|---|
| Distribution name | `relayvox` (import package `relayvox`, CLI `relayvox`). Fallback if taken: `relay-voice-cc`. Product name in docs stays "Relay". No "Claude" in the name; description "A local voice layer for Claude Code". |
| Python | `>=3.11` (stdlib `tomllib`). CI on 3.11 and 3.13. |
| Build | `hatchling`, src layout, `__version__` in `src/relayvox/__init__.py`, SemVer starting at `0.1.0`. |
| IPC | No HTTP. Local stream socket (Unix socket on macOS/Linux, loopback TCP on an ephemeral port on Windows), length-prefixed JSON frames, mutual HMAC challenge with a per-run 32-byte secret stored in a 0600 file. |
| Input | Push-to-talk on a low-level keyboard hook (press and release). Hold to talk, release to send; double-tap to latch hands-free, tap to stop. Observe-only for keys (never suppressed). |
| Default keys | Talk: Right Ctrl (Windows, Linux), Right Option (macOS). Dictate: Right Alt (Windows, Linux), Right Command (macOS). Mouse side button optional, off by default. |
| Delivery | Pluggable transports: `desktop` (Claude desktop app deep link + synthetic typing, Windows and macOS), `tmux` (all POSIX; `send-keys -l` to the verified pane), else `none` (speak-only, with a clear spoken reason). |
| TTS | Offline by default: SAPI (persistent worker) on Windows, `say` on macOS, `espeak-ng` on Linux, `piper` optional. Cloud (ElevenLabs, edge-tts) opt-in only, with streaming playback. |
| STT | faster-whisper, lazy-loaded on first key press, auto model by hardware. Optional mlx-whisper on Apple Silicon. |
| Speech length | Default 2 sentences total per announcement (`speech.max_sentences = 2`, `speech.max_chars = 240`). |
| Telemetry | None. Hugging Face telemetry disabled; offline mode once the model is cached. |

---

## 1. Invariants (tests and review enforce these)

1. **No autonomous action.** `Transport.deliver()` is called from exactly one place: `hub.Hub._deliver()`, which only runs for an `Utterance` whose `source == "mic"` (the user's own voice, captured while the user held or latched the key). No IPC message, hook payload, spoken text or CLI command can trigger delivery. There is no `relayvox send` command.
2. **Session text is never parsed as a command.** Text from hook payloads goes only to the speaker. The router only sees STT output.
3. **The hook never affects Claude.** `relayvox.hook` writes nothing to stdout or stderr, always exits 0, and finishes in under 1 s in the worst case (target p95 under 150 ms including interpreter start). Stdout from a Stop hook can be read by Claude Code as a decision, so silence is a correctness requirement, not style.
4. **Keystrokes are not recorded.** The keyboard hook reduces every event to `(binding_id | OTHER, is_down)` inside the callback. Key codes of non-bound keys never leave the callback, are never logged and never stored.
5. **Never type into the wrong place.** Desktop delivery aborts (no Enter) if the foreground is not the Claude app before typing or changes during typing. tmux delivery aborts if the pane is no longer running the session's process.
6. **No secrets in logs.** API keys, the IPC secret, clipboard contents and (by default) transcripts and spoken text are never logged.

---

## 2. Package layout

```
relay/                         (repo root, GitHub vanatorr/relay)
  pyproject.toml
  README.md  CHANGELOG.md  CONTRIBUTING.md  SECURITY.md  LICENSE  DESIGN.md
  .github/workflows/ci.yml
  src/relayvox/
    __init__.py            __version__ only. Imports nothing (the hook imports this package).
    __main__.py            `python -m relayvox` -> cli.main()
    cli.py                 argparse subcommands (section 9)
    paths.py               per-OS dirs, Claude config dir
    config.py              schema (dataclasses), load, merge, validate, env overrides
    logsetup.py            rotating file log, redaction filter
    secrets.py             ElevenLabs key lookup (env, keyring, .env)
    ipc/
      __init__.py          empty
      protocol.py          framing + HMAC handshake. STDLIB ONLY (imported by hook).
      client.py            send(msg) for hook and CLI. STDLIB ONLY.
      server.py            threaded accept loop, auth, limits, dispatch to Hub
    hook.py                Claude Code hook entry. STDLIB ONLY.
    hooks_install.py       install / uninstall / migrate hooks in settings.json
    events.py              validate hook payload -> Announcement | None
    speech_text.py         clean_for_speech, redact_secrets, summarize
    sessions.py            discover sessions, cache, fuzzy match
    router.py              parse utterance -> Command
    ptt.py                 push-to-talk gesture state machine (pure)
    speaker.py             announcement queue: coalesce, rate limit, barge-in, mute
    hub.py                 wires everything; owns state; the only caller of deliver()
    doctor.py              checks and report
    autostart.py           Run key / LaunchAgent / XDG autostart
    audio/
      tones.py             beeps and chime synthesized with numpy (no asset files)
      playback.py          miniaudio streaming player (PCM chunk source, stop flag, volume)
      capture.py           miniaudio 16 kHz mono capture, max length, RMS gate
    tts/
      base.py              TTSEngine protocol
      sapi.py  macsay.py  espeak.py  piper.py  edge.py  elevenlabs.py
      __init__.py          make_engine(cfg) with fallback chain
    stt/
      base.py              STTEngine protocol
      faster.py            faster-whisper engine
      mlx.py               mlx-whisper engine (optional extra)
      cuda.py              CUDA DLL discovery (Windows) and capability probe
      __init__.py          make_engine(cfg): auto model selection
    platform/
      base.py              protocols: KeyListener, Desktop, Clipboard, Typer
      __init__.py          get_platform() -> Platform bundle for this OS/session
      windows.py           ctypes: WH_KEYBOARD_LL/WH_MOUSE_LL, SendInput, foreground, clipboard, deep link
      macos.py             pyobjc Quartz/AppKit: CGEventTap, CGEventPost, NSWorkspace, NSPasteboard
      linux_x11.py         python-xlib RECORD listener, _NET_ACTIVE_WINDOW
      linux_evdev.py       evdev listener (optional extra; X11 and Wayland)
      linux_tools.py       wl-copy/wl-paste/xclip/xsel/wtype/xdotool via argv subprocess
    transports/
      base.py              Transport protocol, DeliveryResult
      desktop.py           Claude desktop app (Windows, macOS)
      tmux.py              tmux pane delivery (POSIX; also WSL is out of scope)
      __init__.py          select_transport(session, platform)
  tests/
    ... (section 11)
```

Rules: `hook.py`, `ipc/protocol.py`, `ipc/client.py` and `__init__.py` import only the standard library. Heavy imports (`numpy`, `miniaudio`, `faster_whisper`, pyobjc) happen inside functions or in modules that the hook never imports.

### pyproject essentials

```toml
[project]
name = "relayvox"
description = "A local voice layer for Claude Code: sessions speak, you talk back."
requires-python = ">=3.11"
license = "MIT"
dependencies = [
  "faster-whisper>=1.1.0",
  "miniaudio>=1.61",
  "numpy>=1.26",
  "platformdirs>=4.2",
  "psutil>=5.9.8",
  "pyobjc-framework-Quartz>=10.3; sys_platform == 'darwin'",
  "pyobjc-framework-Cocoa>=10.3; sys_platform == 'darwin'",
  "python-xlib>=0.33; sys_platform == 'linux'",
]
[project.optional-dependencies]
cloud  = ["edge-tts>=6.1.12"]            # ElevenLabs uses stdlib urllib, no extra
cuda   = ["nvidia-cublas-cu12>=12.4; sys_platform != 'darwin'",
          "nvidia-cudnn-cu12>=9,<10; sys_platform != 'darwin'"]
mlx    = ["mlx-whisper>=0.4; sys_platform == 'darwin' and platform_machine == 'arm64'"]
evdev  = ["evdev>=1.7; sys_platform == 'linux'"]
keyring = ["keyring>=25"]
piper  = ["piper-tts>=1.2"]
dev    = ["pytest>=8", "ruff>=0.6", "mypy>=1.11"]
[project.scripts]
relayvox = "relayvox.cli:main"
[project.gui-scripts]
relayvox-bg = "relayvox.cli:main_background"   # pythonw-style, no console on Windows
```

### Why each dependency

| Package | Why | Alternative rejected |
|---|---|---|
| faster-whisper | Best local accuracy/speed, CUDA and CPU int8, bundled Silero VAD (brings ctranslate2, onnxruntime, tokenizers, huggingface_hub). | whisper.cpp bindings: less mature Python packaging. |
| miniaudio | One wheel, playback and capture on all three OSes, no PortAudio system lib, streaming generator API. Already proven here. | sounddevice: needs PortAudio on Linux. |
| numpy | PCM math, volume scaling, tone synthesis; faster-whisper needs it anyway. | none |
| platformdirs | Correct per-OS config/log/runtime dirs; tiny, ubiquitous. | hand-rolled: easy to get macOS/XDG wrong. |
| psutil | Cross-platform pid liveness and process ancestry (tmux pane matching), replaces hand-rolled OpenProcess and `ps` parsing. | `/proc` + `ps` + ctypes: three code paths. |
| pyobjc Quartz + Cocoa (macOS) | CGEventTap, CGEventPost, NSWorkspace, NSPasteboard, AXIsProcessTrusted. The only sane way to these APIs from Python. | ctypes on CoreFoundation run loops: fragile. pynput: hides permission state and suppression control. |
| python-xlib (Linux) | Pure Python; X11 RECORD for global key press/release, active window. | pynput: wraps the same, less control. |
| evdev (extra) | Only path to press/release on Wayland; needs C build and `input` group, so opt-in. | none on Wayland |

Not used, deliberately: pynput (no control over suppression and injected-event filtering; we already have the Windows ctypes code), keyboard/mouse packages (root on Linux), requests (urllib suffices), any GUI or tray toolkit (out of scope for v1).

---

## 3. Platform abstraction

`platform/base.py` is a frozen contract (section 12). One `Platform` bundle per process:

```python
@dataclass
class Platform:
    name: str                    # "windows" | "macos" | "linux-x11" | "linux-wayland"
    keys: KeyListener | None     # None when no global key source is available
    desktop: Desktop             # foreground app, focus, deep links
    clipboard: Clipboard | None
    typer: Typer | None          # synthetic text + Enter into the focused window
    capabilities: dict[str, str] # human-readable status per concern, for doctor
```

### Per-concern implementation

| Concern | Windows | macOS | Linux X11 | Linux Wayland |
|---|---|---|---|---|
| Key press/release | `WH_KEYBOARD_LL` (+ `WH_MOUSE_LL` if a mouse binding is set) on a dedicated thread with a `GetMessageW` loop. Ignore events with `LLKHF_INJECTED`. | `CGEventTapCreate(kCGSessionEventTap, head, ListenOnly, keyDown|keyUp|flagsChanged)` on a dedicated `CFRunLoop` thread. Right Option = keycode 61, mask `0x40`; Right Command = keycode 54, mask `0x10` (`NX_DEVICER*KEYMASK`), read from `flagsChanged`. Re-enable the tap on `kCGEventTapDisabledByTimeout`. Active (non-listen) tap only when a mouse binding needs suppression. | X11 RECORD extension (python-xlib), `KeyPress/KeyRelease/ButtonPress/ButtonRelease`. | evdev (extra, user in `input` group) reading keyboards from `/dev/input/event*` (no grab). Otherwise no listener: the compositor binds `relayvox ptt down/up/toggle` (section 5.4). |
| Foreground app | `GetForegroundWindow` + process image name (`claude.exe`). | `NSWorkspace.sharedWorkspace().frontmostApplication()`: bundle id, matched against `desktop_app.bundle_ids` (default `["com.anthropic.claudefordesktop"]`, unverified, doctor prints the live value). | `_NET_ACTIVE_WINDOW` + `WM_CLASS`. | Unknown (returns `None`). |
| Focus / restore | `SetForegroundWindow` with the existing Alt-tap unlock, `ShowWindow(SW_RESTORE)` if iconic. | `NSRunningApplication.activateWithOptions_`. | `_NET_ACTIVE_WINDOW` client message. | Not available. |
| Deep link | `os.startfile(url)` after validation. | `subprocess.run(["open", url])`. | Not used (no desktop app). | Not used. |
| Text typing | `SendInput` with `KEYEVENTF_UNICODE`, batched in 32-char chunks; Enter as `VK_RETURN`. Release stray modifiers first. | `CGEventCreateKeyboardEvent` + `CGEventKeyboardSetUnicodeString` (max 20 UTF-16 units per event), Return keycode 36, `CGEventPost(kCGHIDEventTap)`. | `xdotool type --clearmodifiers --` if present; else clipboard + XTEST Ctrl+V; else clipboard-only. | `wtype --` (wlroots) if present; else clipboard-only (spoken "Copied. Paste it."). |
| Clipboard | Win32 `CF_UNICODETEXT` (existing code). | `NSPasteboard`. | `xclip`/`xsel` argv. | `wl-copy`/`wl-paste` argv. |
| Offline TTS | SAPI via one persistent PowerShell worker (5.2). | `say -o <tmp>.wav --file-format=WAVE --data-format=LEI16@22050 -f -` (text on stdin). | `espeak-ng --stdin --stdout` (WAV on stdout); `piper` if configured. | same |
| Chime / beeps | `audio/tones.py` synthesizes short sine tones played through miniaudio on every OS (drops `winsound`). | same | same | same |

**Typing over clipboard (change from v0).** On Windows and macOS, delivery and dictation type text with Unicode key events, so the clipboard is not touched at all. `typing.method = "clipboard"` is a config fallback for apps that mishandle Unicode events (then: save text, set, Ctrl/Cmd+V, restore after 300 ms, only if the previous content was text; if it held non-text, refuse clipboard mode for that send and type instead). Linux dictation uses tools above. Text is single-line: newlines and control characters are stripped before typing (section 6.6).

### Permissions and honest degradation

**macOS** (permissions attach to the process that runs Relay: Terminal/iTerm when started from a shell, the Python binary when started by the LaunchAgent):
- Input Monitoring: needed for the listen-only event tap. Check with `CGPreflightListenEventAccess()`; `doctor --fix` calls `CGRequestListenEventAccess()`.
- Accessibility: needed for `CGEventPost` (typing) and for an active tap (mouse suppression). Check with `AXIsProcessTrusted()`; prompt with `AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})`.
- Microphone: the TCC prompt appears on first capture. Doctor records 300 ms and reports "silent (likely denied)" if all samples are zero.
- Without Input Monitoring: no key listener; `relayvox ptt toggle` still works (bind it in Shortcuts or a launcher). Without Accessibility: speak-only, delivery and dictation report "needs Accessibility".

**Linux X11**: everything except deep links works. Delivery only via tmux (there is no Linux desktop app).

**Linux Wayland**: no global key listener without evdev; no foreground detection; dictation only with `wtype` (wlroots compositors: sway, Hyprland, river). GNOME/KDE Wayland: dictation falls back to clipboard. evdev requires membership in the `input` group, which lets any of the user's processes read every keyboard. Doctor states this plainly and does not add the user to the group.

**Windows**: a low-level hook does not see input aimed at elevated windows and `SendInput` cannot type into them (UIPI). Doctor checks whether the Claude process is elevated (`OpenProcessToken`/`TokenElevation`) and warns. Some antivirus tools flag global keyboard hooks; README says why Relay needs one and that it discards non-bound keys inside the callback.

**Cannot be verified from the Windows dev box**: everything in the macOS and Linux columns except pure logic. CI runs unit tests on all three OSes; the event tap, typing, permissions, `say`, espeak, xdotool/wtype, and the macOS deep link are marked "untested on hardware" in README until someone runs `relayvox doctor --keys` and a manual checklist (CONTRIBUTING.md) on a real Mac and Linux box. The macOS deep-link scheme and Claude bundle id are assumptions taken from the Windows build.

---

## 4. Delivery transports

```python
class Transport(Protocol):
    name: str
    def available(self, session: Session) -> Availability   # ok / reason string
    def deliver(self, session: Session, text: str) -> DeliveryResult
```

`DeliveryResult(ok: bool, reason: str | None)` where `reason` is a short spoken phrase ("Claude didn't come to the front", "focus moved, not sent").

`transports.select_transport(session, platform)`: first `available` of `[desktop, tmux]`, else `none`. `none.deliver` returns `ok=False, reason="{name} is in a terminal I can't type into. Start it inside tmux to talk to it."`.

### 4.1 desktop (Windows, macOS)

Available when: `session.host_id` matches `^local_[A-Za-z0-9_-]{1,128}$`, platform is Windows or macOS, `platform.typer` exists, and a Claude app process is running.

Steps:
1. Remember the foreground app/window.
2. Open `claude://claude.ai/epitaxy/{host_id}` (string built only from the validated id).
3. Wait up to `desktop_app.focus_timeout_sec` (4.0) for the Claude app to be foreground; if not, try one explicit focus; if still not, **abort** (`ok=False`). v0 pasted anyway; v1 must not.
4. Sleep `desktop_app.mount_delay_sec` (0.9) for the session view to mount.
5. Type text in 32-char chunks; before each chunk and before Enter, confirm the foreground is still Claude. On change: stop, no Enter, `reason="focus moved, not sent"`.
6. Enter. Wait 0.15 s. If `return_focus` and the previous app was not Claude, restore it.

Known limit (unverifiable): if the deep link is ignored while Claude is already on another session, text lands in that session. See risk 1.

### 4.2 tmux (macOS, Linux; Windows only under a POSIX tmux, out of scope)

Available when `tmux` is on PATH (`shutil.which`), the session's `pid` is alive, and a pane is found:
1. `tmux list-panes -a -F "#{pane_id}\t#{pane_pid}\t#{pane_current_command}"` (argv list, 2 s timeout).
2. A pane matches when `pane_pid` is in `{session.pid} ∪ psutil.Process(session.pid).parents()` pids.
3. Exactly one match required; zero or several → unavailable.

Deliver: re-run the match (pane still owns the pid, `pane_current_command` in `{"claude", "node"}` (configurable)), then `tmux send-keys -t <pane_id> -l -- <text>` followed by `tmux send-keys -t <pane_id> Enter`. Text has already been sanitized (no control characters, no ESC), so it cannot inject terminal escape sequences or a newline.

Future transports (not v1): WezTerm `wezterm cli send-text`, kitty `kitty @ send-text`. The protocol allows adding them without touching the hub.

### 4.3 "Real user input" semantics

Both transports put text into the same input box the user types into and press Enter, so Claude Code treats it exactly as typed input (permissions, hooks, history). Relay never writes session files, never calls Claude APIs, and never uses the `messagingSocketPath` or `.key` in session files.

---

## 5. Voice input: push-to-talk

### 5.1 Gestures (per binding)

| Gesture | Effect |
|---|---|
| Hold the key alone ≥ `arm_ms` (200) | Beep, mic opens. Release: beep, transcribe, route (talk) or type (dictate). |
| Release before `arm_ms` (a tap) | Talk key: stops current speech and cancels a pending send (the "To X." window). Dictate key: nothing. |
| Two taps within `double_tap_ms` (350) | Latch: beep, mic open hands-free. |
| Tap while latched | Stop and send. |
| Esc while latched or holding | Discard (Esc still reaches the app; keys are never suppressed). |
| Any other key or button pressed while the bound key is down | Chord: if not yet armed, nothing happens; if armed, discard with a low tone. This keeps Right Ctrl+C, AltGr+E, Option+L and so on working normally. |
| `max_record_sec` (120) reached | Stop and send. |

The mic opens only at arm time, so ordinary chords never open the mic (no macOS mic indicator flicker). The beep is the "go" signal.

### 5.2 State machine (`ptt.py`, pure, clock injected)

```python
class Gesture(Enum): TAP, HOLD_START, HOLD_END, LATCH_START, LATCH_END, ABORT
class PttMachine:
    def __init__(self, arm_ms=200, double_tap_ms=350, max_record_ms=120_000, min_speech_ms=300): ...
    def on_bound(self, is_down: bool, t_ms: int) -> list[Gesture]
    def on_other(self, is_down: bool, t_ms: int) -> list[Gesture]   # any non-bound key/button
    def on_escape(self, t_ms: int) -> list[Gesture]
    def on_tick(self, t_ms: int) -> list[Gesture]                    # arm timer, max length
```

States: `IDLE → PRESSED → (ARMED | IDLE via tap) → IDLE`, `TAP_WAIT` after one tap, `LATCHED`. Auto-repeat downs while already down are ignored. `HOLD_END` with duration < `min_speech_ms` becomes `ABORT`. One machine per binding (talk, dictate, mouse_talk); only one may be recording at a time (a second binding arming while one records is ignored). The listener thread only enqueues `(binding|OTHER|ESC, is_down, t_ms)`; a worker thread runs the machines with a 20 ms `queue.get` timeout for ticks.

### 5.3 Default keys and why

| | Talk (hold) | Dictate (hold) |
|---|---|---|
| Windows | Right Ctrl | Right Alt |
| Linux | Right Ctrl | Right Alt |
| macOS | Right Option | Right Command |

- These keys do nothing when pressed alone, and chords cancel, so the key keeps its normal function. No combination shortcuts: v0's Ctrl+Shift+Space collided with the Claude app's own global quick-entry box.
- Right Alt on Windows: a lone Alt tap normally activates the menu bar on release. When Right Alt arms, the Windows listener injects one unassigned virtual key (`VK 0xE8`, the "menu mask" technique AutoHotkey uses) so the release does not open menus. The injected event carries `LLKHF_INJECTED` and is ignored by our own hook. On AltGr layouts Windows sends a synthetic Left Ctrl (scancode `0x21D`) before Right Alt; the listener ignores that synthetic Left Ctrl so it is not seen as a chord.
- macOS Fn/Globe is rejected: by default it opens the emoji picker or switches input source, and double-press is a common Dictation shortcut. Right Option is rejected for dictate because Option chords type characters on many layouts; holding it alone is still harmless, so it is fine for talk. Right Command for dictate: a lone Right Command does nothing. Known possible collision: macOS Dictation can be set to "Press Right Command Key Twice" (doctor checks, 5.5).
- Laptops without Right Ctrl (many 2024+ Windows laptops have a Copilot key there): pick `f13`–`f24`, `pause`, `scrolllock`, or a mouse button. Doctor's live key test makes this obvious.
- Mouse: `bindings.mouse_talk = "x1" | "x2" | null` (default `null`). Side buttons mean Back/Forward in browsers, so when bound they are suppressed (Windows `WH_MOUSE_LL` returns 1 for that button only; macOS needs an active tap and Accessibility). Linux cannot suppress without grabbing the device, so on Linux a bound side button still also goes Back; doctor warns.

Key names accepted in config: `right_ctrl, left_ctrl, right_alt, left_alt, right_shift, left_shift, right_cmd, left_cmd, right_option, left_option, f1..f24, pause, scrolllock, capslock, insert, menu`, plus `x1, x2` for mouse. Each platform module maps names to native codes and rejects unsupported names at startup with a clear message.

Optional chords (default none): `bindings.chords = {"mute": "ctrl+alt+n"}`. Matched by the same listener (no `RegisterHotKey`), also observe-only. Voice commands cover mute/unmute too.

### 5.4 No-listener fallback (Wayland without evdev, macOS without Input Monitoring)

`relayvox ptt down | up | toggle | cancel [--dictate]` sends a control frame over IPC that feeds the same machine (`down/up` → `on_bound`; `toggle` → latch start or latch end). Docs give examples: sway `bindsym Control_R exec relayvox ptt down` + `bindsym --release Control_R exec relayvox ptt up`; Hyprland `bind`/`bindr`; GNOME custom shortcut with `toggle` (GNOME fires on press only). Start latency is one interpreter launch (~60 ms), acceptable for toggle.

### 5.5 Collision checks in doctor

- Windows: for any chord binding, try `RegisterHotKey`/`UnregisterHotKey` to see if another app owns it. For lone keys, report the keyboard layout (AltGr or not) and warn if the user picked Shift (holding Shift 8 s opens Filter Keys when its shortcut is on: `SystemParametersInfo(SPI_GETFILTERKEYS)` `FKF_HOTKEYACTIVE`).
- macOS: read the Dictation shortcut from `defaults read com.apple.HIToolbox` / `com.apple.speech.recognition.AppleAssistant` (key names unverified; report "unknown" if absent) and flag a Right Command double-press; flag Fn if chosen.
- Linux: `gsettings get` for `org.gnome.desktop.wm.keybindings` and `org.gnome.settings-daemon.plugins.media-keys` when on GNOME, for chord bindings.
- A built-in table of known global shortcuts (Claude desktop quick entry as observed on Windows, Win+H, Alt+Space PowerToys Run, Cmd+Space, Option+Space) that chord bindings are compared against.
- `relayvox doctor --keys`: live test. "Hold your talk key... released after 1.2 s, seen alone: yes." It also prints what the platform reported for the key so the user sees whether events arrive at all (catches missing Right Ctrl, permission problems, elevated windows).

---

## 6. Speech out

### 6.1 Announcements from hook events (`events.py`)

`parse_event(payload: dict) -> Announcement | PendingRecap | None` after validation (6.5).

| Event | Result |
|---|---|
| `PostToolUse`, `tool_name` ends with `wrap_up` | `PendingRecap(session_id, recap, ask)` stored by the hub until that session's Stop. |
| `Stop`, `stop_hook_active` false | If a pending recap exists: summary of recap + ask; else summary of `last_assistant_message`. |
| `Notification` `permission_prompt` | "needs your approval." |
| `Notification` `agent_needs_input` / `elicitation_dialog` | "has a question. " + first sentence of `message`. |
| `Notification` `idle_prompt` | Ignored by default (`speech.announce_idle = false`). |
| any event with `agent_id` | Ignored (subagents). |

### 6.2 Summary length (`speech_text.summarize`)

`summarize(recap: str, ask: str, max_sentences=2, max_chars=240) -> str`:
- With an ask: `max_sentences - 1` sentences of the recap, then the ask's first sentence in full. With `max_sentences = 1`, the ask alone.
- Without an ask: the first `max_sentences` sentences.
- Cut to `max_chars` at the last word boundary. The ask is never cut mid-sentence unless it alone exceeds `max_chars`.
- Final line: `"{session name}. {summary}"`.
Config: `speech.max_sentences` (default 2, range 1–6), `speech.max_chars` (default 240, 80–1000).

### 6.3 Cleaning (`clean_for_speech`, keep v0 rules, add redaction)

Keep v0: drop fenced code, unwrap inline code and markdown links, URLs → "a link", Windows and POSIX paths → "a file", strip markdown symbols and bullets, collapse whitespace. Add `redact_secrets`: tokens that look like keys (`sk-…`, `xi-…`, `ghp_…`, `AKIA…`, any run of 32+ base64/hex chars) → "a key". This matters doubly when a cloud voice is on, because the text leaves the machine.

### 6.4 Queue, rate limits, barge-in (`speaker.py`)

- Single playback worker. Queue holds at most `speech.queue_max` (6) items. Per-session coalescing: a new announcement from a session replaces that session's queued one. When full, drop the oldest non-priority item.
- Per-session rate limit: identical text from the same session within 30 s is dropped; at most 6 announcements per session per minute.
- Priority items (`say_now`: "To X.", "Cancelled.", errors) jump the queue.
- Barge-in: any talk-key TAP or HOLD_START stops playback within 50 ms (stop flag checked per audio chunk) and clears non-priority items.
- Mute: drops new announcements; priority confirmations still play at reduced volume? No: mute silences everything except the arm/stop beeps.
- Focus rule: if the Claude app is foreground and `speech.speak_when_claude_focused` is false, play the chime instead (v0 behaviour). On platforms where foreground is unknown, speak.

### 6.5 Hook payload validation

- Top level must be an object. Accept only known `hook_event_name` values.
- `session_id`: string matching `^[A-Za-z0-9_-]{1,128}$`, else drop.
- Strings truncated: `last_assistant_message` 4000, `message` 1000, `recap` 2000, `ask` 500. `cwd` used only via `basename`.
- Non-string types where strings are expected → field treated as empty.

### 6.6 Utterance sanitizing (before typing anywhere)

`sanitize_for_typing(text)`: NFC normalize, remove C0/C1 control characters including ESC and newlines (replace with space), collapse spaces, cap at 2000 chars.

### 6.7 TTS engines

```python
class TTSEngine(Protocol):
    name: str
    is_cloud: bool
    def synthesize(self, text: str) -> Iterator[PcmChunk]   # PcmChunk(data: bytes int16, rate: int, channels: int)
    def close(self) -> None
```

| Engine | How | Streaming |
|---|---|---|
| `sapi` (Windows default) | One persistent `powershell -NoProfile -NonInteractive -Command -` child started at first use, running a fixed script that loops: read a line (base64 of UTF-8 text), synthesize to a temp WAV path we generate, print "done". No user text ever enters the script source. Removes v0's ~0.5–1 s PowerShell start per line. | Whole utterance (fast) |
| `say` (macOS default) | `say -o <tmp>.wav --file-format=WAVE --data-format=LEI16@22050 -f -`, text on stdin. Optional `voice`, `rate`. | Whole |
| `espeak` (Linux default) | `espeak-ng --stdin --stdout -v <voice> -s <rate>`. | Whole |
| `piper` (opt-in local) | `piper --model <path> --output_raw`, text on stdin; read raw 16-bit PCM from stdout in 4 KB chunks. | Yes |
| `elevenlabs` (opt-in cloud) | `POST /v1/text-to-speech/{voice_id}/stream?output_format=pcm_22050` via urllib, `xi-api-key` header, stream raw PCM in 4 KB reads. | Yes, play starts on first chunk |
| `edge` (opt-in cloud, extra) | edge-tts MP3 chunks pushed into a queue-backed `miniaudio.StreamableSource`, decoded with `miniaudio.stream_any`. If that proves unreliable, buffer and decode whole. | Yes |

Fallback chain: configured engine → OS default offline engine → chime. Errors log one line (no text). Volume scaling in the player on int16 numpy arrays. All subprocesses: argv lists, `shell=False`, `timeout`, `CREATE_NO_WINDOW` on Windows.

---

## 7. Speech to text

```python
class STTEngine(Protocol):
    def load(self) -> None                         # idempotent, thread-safe
    def transcribe(self, pcm: np.ndarray, prompt: str | None, language: str | None) -> str
    def unload(self) -> None
```

- **Auto model** (`stt.model = "auto"`): CUDA available (`ctranslate2.get_cuda_device_count() > 0` after `stt/cuda.py` adds the nvidia pip DLL dirs on Windows) → `large-v3-turbo`, `float16`. Apple Silicon with the `mlx` extra → mlx-whisper `large-v3-turbo`. Otherwise CPU → `small`, `int8` (`base` if fewer than 4 physical cores). Any explicit model name overrides.
- **Lazy load**: loading starts in a background thread at the first HOLD_START or LATCH_START, while the user speaks, and `transcribe` waits for it (up to 60 s). `stt.preload = false` by default; `true` loads at startup. `stt.idle_unload_min = 30` frees the model after inactivity (0 = never).
- **Offline**: set `HF_HUB_DISABLE_TELEMETRY=1` always; set `HF_HUB_OFFLINE=1` when the model is already in the cache. First download only through `relayvox models download` or the first use, which says "Downloading the speech model, about N megabytes" once.
- **Decode**: `beam_size=5` on GPU, `1` on CPU; `vad_filter=True`; `initial_prompt` = "Claude Code sessions: {names}." (names capped at 30, 400 chars); `condition_on_previous_text=False`. Capture skips transcription when duration < 0.4 s or RMS below `stt.min_rms` (0.005).
- Strip fillers (v0 regex) and leading/trailing punctuation noise.

---

## 8. Routing (`router.py`, pure)

`parse(text: str) -> Command` with `Command` one of `Stop`, `ListSessions`, `Mute(bool)`, `Attach(query)`, `Tell(query_and_body)`, `Message(body)`. Keep v0's regexes and add `mute|unmute|be quiet for now`. `resolve(command, sessions, state, foreground_session) -> Target | Ask` uses `sessions.match`:

- `Tell`: split target and body as v0 (`"X, body"` first, then best prefix of up to 5 words), threshold 0.6, plus a margin rule: if the top two scores are within 0.05 and both ≥ 0.6, ask "Did you mean A or B?" instead of guessing.
- `Message`: attached → last speaker (only if it spoke within `routing.last_speaker_ttl_sec`, 600) → session on screen (desktop app foreground and exactly one deliverable session is on screen is unknowable, so: only when exactly one session is deliverable) → ask "Who should I send that to?".
  - Change from v0: v0 typed into "whatever Claude window is in front" without knowing the session. v1 drops that blind path.
- Before delivery: `say_now("To {name}.")`, then a cancel window `routing.cancel_window_sec` (1.5). A talk-key TAP, Esc, or the word "cancel" in a new utterance cancels.

`sessions.py`: read `<claude_dir>/sessions/*.json` (`claude_dir` = `$CLAUDE_CONFIG_DIR` or `~/.claude`), ignore files > 64 KB, keep `sessionId, hostSessionId, name, cwd, status, updatedAt, pid`, drop dead pids (`psutil.pid_exists`). Cache keyed on directory listing + file mtimes; rescan at most every 2 s, only when asked. Match on name and cwd basename as v0.

---

## 9. CLI

| Command | Does |
|---|---|
| `relayvox run [--quiet]` | Start in the foreground (logs to console too). Single instance via a lock file. |
| `relayvox-bg run` | Same without a console window (Windows pythonw); used by autostart. |
| `relayvox stop` | Authenticated `shutdown` frame. |
| `relayvox status` | Running?, transport per session, listener backend, TTS/STT engines, muted. |
| `relayvox install-hooks [--dry-run]` / `uninstall-hooks` | Section 10.4. |
| `relayvox doctor [--keys] [--fix]` | Section 13. Exit code 0 ok, 1 warnings, 2 blocking. |
| `relayvox say TEXT` | Speak through the running hub (tests the voice). |
| `relayvox voices` | List voices for the configured engine (SAPI installed voices, `say -v ?`, `espeak-ng --voices`, ElevenLabs `/v1/voices` only if cloud is configured). |
| `relayvox ptt down\|up\|toggle\|cancel [--dictate]` | Section 5.4. |
| `relayvox mute [on\|off\|toggle]` | |
| `relayvox autostart on\|off\|status` | Windows `HKCU\...\Run` value via `winreg` → `"<relayvox-bg>" run --quiet`; macOS `~/Library/LaunchAgents/io.github.vanatorr.relayvox.plist` via `plistlib` + `launchctl bootstrap gui/<uid>`; Linux `~/.config/autostart/relayvox.desktop` (XDG, so the graphical session env is present). |
| `relayvox config init\|path\|show` | Write a commented template, print paths, print the merged config with secrets masked. |
| `relayvox models download` | Fetch the STT model for the selected profile. |

---

## 10. Security

### 10.1 Threat model

Assets: what sessions say (may contain code, secrets), what the user says, the ability to type into Claude sessions (a Claude session with permissive settings can run commands, so injected input there equals code execution), the ElevenLabs key, the clipboard.

| Attacker | Capability | Mitigation |
|---|---|---|
| Web page in the user's browser | `fetch`/`no-cors` POST to localhost, DNS rebinding | No HTTP server. The socket speaks a binary framed protocol that starts with a server challenge; an HTTP request fails the magic check and the connection is closed. No HTTP response is ever produced. |
| Other local user | Connect to loopback TCP; read world-readable files; squat a port | Secret in a 0600 file in a 0700 per-user dir; mutual HMAC (client proves the secret, server proves it back before the client sends the payload, so a squatter on a stale port learns nothing); ephemeral port written at startup; Unix socket in a 0700 runtime dir on POSIX. |
| Same-user process | Everything the user can do | Out of scope (it can already type and read files). We still make delivery impossible over IPC (Invariant 1), so a compromised low-privilege helper cannot use Relay to type into Claude. |
| Malicious repository the user opens | Files in the project cwd where hooks run | Hook command is `"<abs venv python>" -I -m relayvox.hook`. `-I` drops cwd and user site from `sys.path` and ignores `PYTHON*` env vars, so a planted `relayvox.py` in the project cannot run. |
| Content in a Claude session (prompt injection) | Text that says "tell payments to delete everything" | Session text only reaches the speaker; it is never routed (Invariant 2). The mic is open only while the user holds or latched the key, and playback is stopped on arm, so Relay does not hear itself. |
| Flood of hook events | Many sessions or a loop | IPC limits (10.2) + speaker queue caps and per-session rate limits (6.4). |
| Accidental delivery to the wrong window | Focus race | Invariant 5, desktop checks per chunk, tmux pane re-verification. |

### 10.2 IPC protocol (`ipc/protocol.py`)

- Endpoint file `<state_dir>/endpoint.json`, mode 0600 (POSIX: `os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)` then `os.replace`; parent dir 0700, verified owned by the user and not group/world writable, else refuse to start). Windows: under `%LOCALAPPDATA%\relayvox` (per-user ACL inherited from the profile); doctor verifies the dir is inside the user profile. Content: `{"v":1, "transport":"unix"|"tcp", "path"|"port":…, "secret":"<64 hex>"}`. The secret is regenerated at every `run`. The file is deleted on clean shutdown.
- POSIX transport: `AF_UNIX` socket at `<runtime_dir>/relayvox.sock` (platformdirs `user_runtime_dir`; on macOS a 0700 dir under the user cache dir; path kept under 100 bytes). Windows: `AF_INET` bound to `127.0.0.1`, port 0 (ephemeral).
- Handshake (all within 1 s, else close):
  1. Server → `b"RLY1"` + 32-byte nonce `Ns`.
  2. Client → 32-byte nonce `Nc` + `HMAC-SHA256(secret, b"c" + Ns + Nc)`.
  3. Server verifies with `hmac.compare_digest`; on failure closes. Server → `HMAC-SHA256(secret, b"s" + Nc + Ns)`.
  4. Client verifies, then sends frames: 4-byte big-endian length + UTF-8 JSON, max 64 KiB. Server replies one frame per request.
- Message types: `event` (hook payload, already whitelisted by the hook), `say` (`text` ≤ 1000), `status`, `ptt` (`action`, `binding`), `mute`, `shutdown`. Unknown type → error frame. **No message type delivers text to a session.**
- Server limits: 8 concurrent connections, 32 new connections per second (excess closed immediately), 2 s idle timeout, one request per connection for `event`.

### 10.3 Hook (`hook.py`)

1. `data = sys.stdin.buffer.read(1_048_576)`; parse JSON; on any error exit 0.
2. Whitelist fields: `hook_event_name, session_id, cwd, agent_id, stop_hook_active, last_assistant_message[:4000], notification_type, message[:1000], tool_name`, and for `tool_name` ending in `wrap_up` only `tool_input.recap[:2000]` and `tool_input.ask[:500]`. Drops `transcript_path`, other tool inputs and responses.
3. Read the endpoint file; if absent, exit 0 (Relay not running costs ~1 ms beyond interpreter start).
4. Connect with a 300 ms timeout, handshake, send, read the 1-frame ack with a 300 ms timeout; any error is swallowed.
5. `os._exit(0)`. Nothing printed, ever.
Budget: interpreter start ~40–70 ms (Windows) / ~20–40 ms (POSIX) + < 5 ms work. Hook `timeout` in settings: 3 s.

### 10.4 Hook installer (`hooks_install.py`)

- Target: `<claude_dir>/settings.json`. If missing, create with only our hooks. If it is invalid JSON, abort with a message; never overwrite.
- Our entries: `Stop`, `Notification`, `PostToolUse` with matcher `mcp__ccd_turn__wrap_up` (desktop app only; harmless elsewhere). Command: `"<sys.executable>" -I -m relayvox.hook` with forward slashes on Windows; marker for ownership = command contains `-m relayvox.hook`. Also removes legacy entries whose command contains `relay/hook.py` (v0 migration).
- Algorithm: load → remove our groups (and legacy) from each event list → (install) append ours → drop empty lists/`hooks` → if the result equals the input, print "already installed" and do not write.
- Backup: copy to `settings.json.relayvox-<YYYYmmdd-HHMMSS>.bak` before the first change of a run; keep the 3 newest.
- Atomic write: temp file in the same directory, same mode as the original, `json.dump(indent=2)`, flush + `fsync`, `os.replace`. Before replace, re-stat the original; if its mtime changed since we read it, re-read and redo once, else abort.
- `--dry-run` prints a unified diff. Unit-tested for idempotency, other hooks preserved, legacy removal, invalid JSON abort.

### 10.5 Secrets

`secrets.get("ELEVENLABS_API_KEY")`: env var → OS keyring (`keyring` extra, service `relayvox`) → `<config_dir>/.env`. On POSIX the `.env` is ignored with a warning unless mode is 0600 (or stricter) and owned by the user. `relayvox config set-key elevenlabs` stores via keyring when available. Keys are never logged, never shown (`config show` and doctor print "present (env|keyring|.env)").

### 10.6 Logs

- `platformdirs.user_log_dir("relayvox")/relay.log`, `RotatingFileHandler(1 MB, backupCount=3)`, file 0600 on POSIX.
- Default INFO lines: event kind, session name, lengths, engine timings, delivery result. No spoken text, transcripts, clipboard or key events.
- `logging.transcripts = true` (opt-in) adds heard text and spoken text.
- A redaction filter applies `redact_secrets` to every record as a second line of defence.

### 10.7 Process hygiene

No `shell=True` anywhere (ruff rule `S602/S604` enforced). Every subprocess: argv list, absolute or `shutil.which`-resolved executable, timeout, no user text in argv except tmux `send-keys -l --` and `wtype --`/`xdotool type --` (after the `--`, sanitized). Deep-link URLs built only from validated ids. Temp files via `tempfile.mkstemp` in a 0700 per-run dir, removed after use.

---

## 11. Config

- Location: `platformdirs.user_config_dir("relayvox")/config.toml` (Windows `%APPDATA%\relayvox`, macOS `~/Library/Application Support/relayvox`, Linux `~/.config/relayvox`). State (endpoint, lock): `user_state_dir`. Logs: `user_log_dir`.
- Format TOML (comments for users; `tomllib` reads; `config init` writes a static template string).
- Schema: nested frozen dataclasses with defaults in `config.py`; `load()` = defaults ← file ← env overrides `RELAYVOX_<SECTION>__<KEY>` (e.g. `RELAYVOX_SPEECH__MAX_SENTENCES=1`, `RELAYVOX_TTS__ENGINE=say`). Validation: unknown keys → warning (typo help with `difflib.get_close_matches`), wrong types/ranges → error naming the key and showing the default.
- Migration: if a v0 `config.json` path is passed (`relayvox config init --from <path>`), map old keys.

```toml
[bindings]
talk = "auto"            # auto = right_ctrl (Win/Linux), right_option (macOS)
dictate = "auto"         # auto = right_alt (Win/Linux), right_cmd (macOS); "" disables
mouse_talk = ""          # "x1" | "x2" | ""
arm_ms = 200
double_tap_ms = 350
max_record_sec = 120
[bindings.chords]        # optional, e.g. mute = "ctrl+alt+n"

[speech]
max_sentences = 2
max_chars = 240
announce_idle = false
speak_when_claude_focused = false
queue_max = 6
volume = 0.6

[tts]
engine = "auto"          # auto | sapi | say | espeak | piper | edge | elevenlabs
voice = ""               # engine-specific; empty = system default
rate = 0
piper_model = ""
elevenlabs_voice_id = "21m00Tcm4TlvDq8ikWAM"
elevenlabs_model = "eleven_flash_v2_5"
edge_voice = "en-US-AvaMultilingualNeural"

[stt]
model = "auto"
language = ""            # "" = detect; "en", "fr", ...
preload = false
idle_unload_min = 30
min_rms = 0.005
cuda_dll_dir = ""

[routing]
cancel_window_sec = 1.5
last_speaker_ttl_sec = 600

[desktop_app]
focus_timeout_sec = 4.0
mount_delay_sec = 0.9
return_focus = true
bundle_ids = ["com.anthropic.claudefordesktop"]   # macOS, unverified
process_names = ["claude.exe"]                     # Windows

[typing]
method = "type"          # type | clipboard

[tmux]
commands = ["claude", "node"]

[logging]
level = "INFO"
transcripts = false
```

---

## 12. Interface contracts (frozen before parallel work)

Written in step 0, then not changed without both chunk owners agreeing.

```python
# platform/base.py
KeyEvent = tuple[str, bool, int]      # (binding name | "OTHER" | "ESC", is_down, t_ms monotonic)
class KeyListener(Protocol):
    backend: str                      # "win-ll" | "mac-tap" | "x11-record" | "evdev"
    def start(self, bindings: dict[str, str], sink: Callable[[KeyEvent], None]) -> None
        # bindings: {"talk": "right_ctrl", "dictate": "right_alt", "mouse_talk": "x1", "chord:mute": "ctrl+alt+n"}
        # sink is called from the listener thread; must be non-blocking (queue.put_nowait).
        # Chords are reported as (f"chord:{name}", True, t) on full match.
    def stop(self) -> None
    def on_arm(self, binding: str) -> None   # Windows: menu-mask injection for Alt keys; others no-op
class Desktop(Protocol):
    def foreground(self) -> AppRef | None    # AppRef(pid, app_id: process name or bundle id, handle: object)
    def is_claude(self, app: AppRef | None) -> bool
    def focus(self, app: AppRef) -> bool
    def open_url(self, url: str) -> bool     # caller validates; impl refuses anything not starting with "claude://"
    def claude_running(self) -> bool
class Clipboard(Protocol):
    def get_text(self) -> str | None
    def has_non_text(self) -> bool
    def set_text(self, text: str) -> bool
class Typer(Protocol):
    def type_text(self, text: str) -> bool   # no newline handling; text is pre-sanitized single line
    def press_enter(self) -> bool
    def paste(self) -> bool                  # Ctrl/Cmd+V, for typing.method = "clipboard"
def get_platform(cfg: Config) -> Platform

# tts/base.py, stt/base.py: as in sections 6.7 and 7.
# audio/playback.py
class Player:
    def play(self, chunks: Iterable[PcmChunk], volume: float, stop: threading.Event) -> None
# audio/capture.py
class Capture:
    def start(self) -> None
    def stop(self) -> np.ndarray             # float32 mono 16 kHz
# audio/tones.py
def tone(name: Literal["arm", "stop", "abort", "chime", "mute", "unmute"]) -> PcmChunk

# transports/base.py: as in section 4.
# sessions.py (chunk A) exposes to chunk B's desktop transport:
@dataclass(frozen=True)
class Session: id: str; host_id: str | None; name: str; cwd: str; pid: int; status: str | None; updated: float
```

---

## 13. Doctor

Each check returns `Check(name, status: ok|warn|fail, detail, fix_hint)`. Checks: Python and package version; config parse; dirs and permissions (endpoint dir 0700, `.env` 0600); hooks installed and pointing at this interpreter (stale path after reinstall = fail); Relay running and IPC round trip; key listener backend and permissions (macOS Input Monitoring, Accessibility; Linux session type, evdev access, X11 RECORD); collisions (5.5); microphone capture level; TTS engine available + 1 short synthesis timing; STT device (CUDA count, DLLs found, model cached, chosen profile); transports (Claude app running, bundle id seen, tmux present, per-session transport); cloud engines flagged "text leaves your machine"; Windows elevation mismatch. `--keys` runs the live key test; `--fix` only does safe things (request macOS permissions, create dirs, chmod `.env`), never edits shell profiles or groups.

---

## 14. Performance targets

| Path | Target |
|---|---|
| Hook end to end | p95 < 150 ms, hard cap 1 s (timeouts), 0 ms if Relay not running beyond interpreter start |
| Hook event → speech start (offline TTS) | < 600 ms SAPI worker warm, < 400 ms `say`/espeak |
| Hook event → speech start (ElevenLabs stream) | < 700 ms on a normal connection |
| Key release → delivery start, GPU turbo, 5 s utterance | < 1.0 s + 1.5 s cancel window |
| Same on CPU `small` int8 | < 2.5 s |
| Barge-in | < 50 ms |
| Idle CPU | ~0 % (all threads block on queues, sockets or OS loops; no polling except the 20 ms PTT tick while a key is down or recording; idle tick interval 1 s) |
| Idle RAM | < 120 MB without a model loaded; model memory released after `idle_unload_min` |

---

## 15. Quality

- **Style**: ruff (`E,F,I,B,UP,S,SIM,RUF`), line length 100; type hints everywhere; mypy `--strict` on everything except `platform/*`, `tts/*`, `stt/*`, `audio/*` (OS/third-party heavy; checked non-strict).
- **Logging**: `logging` module only, logger per module, configured in `logsetup.py`.
- **Tests (pytest, no audio, no GPU, no OS hooks)**:
  - `test_speech_text.py`: cleaning, redaction, `summarize` sentence/char limits, ask handling.
  - `test_events.py`: each payload kind → Announcement; validation (bad ids, wrong types, oversize, subagent).
  - `test_router.py`: command parsing, split target, fuzzy match thresholds, ambiguity margin, fallback order, last-speaker TTL.
  - `test_sessions.py`: tmp claude dir fixtures, dead pids filtered (monkeypatched), cache invalidation.
  - `test_ptt.py`: full gesture table with a fake clock (hold, short hold abort, tap, double-tap latch, latch tap end, chord before and after arm, Esc, max length, auto-repeat, two bindings).
  - `test_speaker.py`: queue cap, coalescing, rate limit, barge-in, mute, with fake engine and player.
  - `test_config.py`: defaults, file merge, env overrides, validation errors, unknown-key suggestions.
  - `test_hooks_install.py`: idempotency, preserves other hooks, legacy removal, invalid JSON abort, backup rotation, dry-run diff, concurrent-change retry.
  - `test_ipc.py`: real socket on each OS: handshake ok, wrong secret rejected, HTTP request rejected without any HTTP response, oversize frame rejected, connection rate limit, server-proof check (squatter test), no delivery message type exists.
  - `test_hook.py`: run `python -I -m relayvox.hook` as a subprocess with Relay absent and present (stub server): exit 0, empty stdout/stderr, field whitelist, < 1 s.
  - `test_tmux.py`: pane matching with faked `tmux` output and process tree; sanitized argv; abort on pane change.
  - `test_invariants.py`: grep-style test that `deliver(` is called only in `hub.py`; no `shell=True` in the package.
- **CI** (`.github/workflows/ci.yml`): matrix `os: [windows-latest, macos-latest, ubuntu-latest]` × `python: ["3.11", "3.13"]`; steps: `pip install -e .[dev]` (faster-whisper installs but no model download; tests never import it), `ruff check`, `ruff format --check`, `mypy` (ubuntu 3.13 only), `pytest -q`. Plus a build job: `python -m build` and `twine check`. Release on tag `v*` via PyPI trusted publishing.
- **Docs**: README (what it is, 30-second demo GIF placeholder, install with pipx/uv, first run, keys table, who your message goes to, settings, privacy (offline by default, what cloud voices send), platform support matrix with "verified / untested" column, troubleshooting = `relayvox doctor`, uninstall); CONTRIBUTING (dev setup, tests, the manual hardware checklist per OS); SECURITY.md (threat model summary, how to report privately, supported versions); CHANGELOG (Keep a Changelog, starting 0.1.0).

---

## 16. Build plan

### Step 0: scaffold (one engineer, small, before the split)

Owns and freezes: `pyproject.toml`, `src/relayvox/__init__.py`, `__main__.py`, all `base.py` contract files (`platform/base.py`, `tts/base.py`, `stt/base.py`, `transports/base.py`), `sessions.Session` dataclass stub, empty `__init__.py` files, `.github/workflows/ci.yml`, `tests/conftest.py`. Move v0 files to `legacy/` (deleted at integration).

### Chunk A: core logic, IPC, hooks, routing (pure Python, fully unit-tested on all OSes)

Owns:
`cli.py`, `paths.py`, `config.py`, `logsetup.py`, `secrets.py`, `ipc/protocol.py`, `ipc/client.py`, `ipc/server.py`, `hook.py`, `hooks_install.py`, `events.py`, `speech_text.py`, `sessions.py` (beyond the stub), `router.py`, `ptt.py`, `speaker.py`, `transports/__init__.py`, `transports/tmux.py`, `tests/test_speech_text.py, test_events.py, test_router.py, test_sessions.py, test_ptt.py, test_speaker.py, test_config.py, test_hooks_install.py, test_ipc.py, test_hook.py, test_tmux.py`.

`speaker.py` is written against `TTSEngine` and `Player` protocols only (fakes in tests). `cli.py` dispatches `run` to `hub.main(cfg)` imported lazily (exists after integration).

### Chunk B: platform, audio, TTS, STT, desktop transport, autostart

Owns:
`platform/__init__.py, windows.py, macos.py, linux_x11.py, linux_evdev.py, linux_tools.py`, `audio/tones.py, playback.py, capture.py`, `tts/__init__.py, sapi.py, macsay.py, espeak.py, piper.py, edge.py, elevenlabs.py`, `stt/__init__.py, faster.py, mlx.py, cuda.py`, `transports/desktop.py`, `autostart.py`, `tests/test_platform_smoke.py` (import-only per OS, key-name mapping tables), `tests/test_tones.py`, `tests/test_tts_args.py` (argv construction only), `scripts/manual_check.py` (interactive per-OS hardware check used by CONTRIBUTING).

B reads `config.Config` through the frozen dataclass shape agreed in step 0 (section 11 is the schema; A implements loading). Where B needs a value it reads `cfg.<section>.<key>`.

### Integration (one engineer, after A and B)

Owns: `hub.py`, `doctor.py`, `tests/test_invariants.py`, README.md, CONTRIBUTING.md, SECURITY.md, CHANGELOG.md, removal of `legacy/`.
`hub.py`: builds Platform, engines, Speaker, IPC server, PTT worker; implements `on_gesture`, `on_ipc`, `_deliver` (the only `deliver()` caller), lazy STT load, idle unload, single-instance lock. Verify on Windows end to end (owner's machine): hold-to-talk, latch, dictate, chord passthrough (Right Ctrl+C copies; AltGr characters type), desktop delivery, focus-change abort, Claude-focused chime, 2-sentence recaps, `doctor --keys`. Mark macOS/Linux rows "untested on hardware" in README.

---

## 17. Risks

1. **Desktop delivery depends on undocumented app behaviour.** The `claude://…/epitaxy/<id>` deep link, focus timing and composer focus are observed on Windows only. If the link is ignored, text could land in the session already on screen. Mitigations: foreground checks, per-chunk checks, the spoken "To X." window; README states it is unofficial. Check: test on macOS with the real app before claiming support.
2. **Global keyboard hooks across OSes.** macOS TCC permissions attach to the host binary (confusing under LaunchAgent), Wayland has no listener without evdev, Windows UIPI and antivirus can block hooks, and the Right Alt menu-mask trick and AltGr fake-Ctrl handling need real-keyboard testing. Mitigations: `doctor --keys`, `relayvox ptt` fallback, honest support matrix.
3. **Unicode synthetic typing vs clipboard.** Typing avoids clipboard leaks but some apps or IMEs may drop or reorder Unicode key events, and long texts take longer to type (focus-race window). Mitigation: chunked typing with focus checks, `typing.method = "clipboard"` fallback with restore.

Lesser: faster-whisper wheel/CUDA mismatch on new Python versions; first-run model download size; ElevenLabs `pcm_22050` availability per plan tier (fallback `mp3` buffered).
