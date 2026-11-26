"""Relay: voice for Claude Code sessions (Windows).

- Sessions speak: Claude Code hooks POST to /event; the hub speaks a short status line,
  one at a time, prefixed with the session's name. Silent (chime) while Claude is in front.
- You talk back: a hotkey toggles recording; local Whisper (GPU) transcribes; the hub routes
  the text to a session by opening it in the Claude app and typing it as you would.
"""
import ctypes
import difflib
import glob
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import winsound
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

def load_config():
    """config.example.json holds the defaults; your own config.json (not in git) overrides them."""
    cfg = json.load(open(os.path.join(HERE, "config.example.json"), encoding="utf-8"))
    mine = os.path.join(HERE, "config.json")
    if os.path.exists(mine):
        user = json.load(open(mine, encoding="utf-8"))
        hk = {**cfg.get("hotkeys", {}), **user.pop("hotkeys", {})}
        cfg.update(user)
        cfg["hotkeys"] = hk
    return cfg


def load_dotenv():
    """KEY=value lines from .env next to relay.py (not in git). Real env vars win."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
CFG = load_config()
LOG = os.path.join(HERE, "relay.log")
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "sessions")


def add_cuda_dlls():
    """Make cuBLAS / cuDNN visible to faster-whisper: the configured folder, plus the
    nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip packages if they are installed."""
    dirs = [CFG.get("cuda_dll_dir")] if CFG.get("cuda_dll_dir") else []
    import site
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        dirs += glob.glob(os.path.join(sp, "nvidia", "*", "bin"))
    for d in dirs:
        if d and os.path.isdir(d):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + ";" + os.environ["PATH"]


add_cuda_dlls()

import miniaudio  # noqa: E402
import numpy as np  # noqa: E402

_log_lock = threading.Lock()


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    with _log_lock:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)]

VK_CONTROL, VK_MENU, VK_SHIFT, VK_RETURN, VK_V = 0x11, 0x12, 0x10, 0x0D, 0x56
KEYUP = 0x0002


def proc_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def pid_alive(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def fg_window():
    return user32.GetForegroundWindow()


def window_proc(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return proc_name(pid.value)


def claude_in_front():
    return window_proc(fg_window()) == "claude.exe"


def force_foreground(hwnd):
    if not hwnd:
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    # A tapped Alt lets a background process take the foreground.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYUP, 0)
    user32.SetForegroundWindow(hwnd)


def clip_get():
    text = None
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        h = user32.GetClipboardData(13)
        if h:
            p = kernel32.GlobalLock(h)
            if p:
                text = ctypes.wstring_at(p)
                kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    return text


def clip_set(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, h)
    finally:
        user32.CloseClipboard()
    return True


def release_modifiers():
    for vk in (VK_CONTROL, VK_MENU, VK_SHIFT):
        user32.keybd_event(vk, 0, KEYUP, 0)


def paste_text(text, press_enter):
    old = clip_get()
    clip_set(text)
    time.sleep(0.05)
    release_modifiers()
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
    time.sleep(0.25)
    if press_enter:
        user32.keybd_event(VK_RETURN, 0, 0, 0)
        user32.keybd_event(VK_RETURN, 0, KEYUP, 0)
        time.sleep(0.15)
    if old is not None:
        time.sleep(0.2)
        clip_set(old)


# --------------------------------------------------------------------------- sessions
def load_sessions():
    """Live Claude Code sessions from ~/.claude/sessions/<pid>.json."""
    out = []
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        pid = d.get("pid")
        if not pid or not pid_alive(pid):
            continue
        out.append({
            "id": d.get("sessionId"),
            "host": d.get("hostSessionId"),
            "name": d.get("name") or os.path.basename(d.get("cwd") or "") or "a session",
            "cwd": d.get("cwd") or "",
            "status": d.get("status"),
            "updated": d.get("updatedAt") or 0,
        })
    return out


def session_by_id(sid):
    for s in load_sessions():
        if s["id"] == sid or s["host"] == sid:
            return s
    return None


def norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def match_score(query, s):
    q = norm(query)
    if not q:
        return 0.0
    best = 0.0
    for cand in (norm(s["name"]), norm(os.path.basename(s["cwd"]))):
        if not cand:
            continue
        r = difflib.SequenceMatcher(None, q, cand).ratio()
        if q in cand or cand in q:
            r = max(r, 0.85)
        words = q.split()
        if words and all(any(difflib.SequenceMatcher(None, w, cw).ratio() > 0.8 for cw in cand.split())
                         for w in words):
            r = max(r, 0.9)
        best = max(best, r)
    return best


def find_session(query, deliverable=True):
    cands = [s for s in load_sessions() if (s["host"] or not deliverable)]
    scored = sorted(((match_score(query, s), s) for s in cands), key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.6:
        return scored[0][1], scored[0][0]
    return None, 0.0


# --------------------------------------------------------------------------- speech out
class Speaker:
    def __init__(self):
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.speaking = threading.Event()
        self.muted = False
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text, priority=False):
        text = clean_for_speech(text)
        if not text or self.muted:
            return
        self.q.put(text)

    def say_now(self, text):
        """Speak immediately, blocking the caller (used for short confirmations)."""
        self._speak(clean_for_speech(text))

    def stop(self):
        self.stop_flag.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def _run(self):
        while True:
            text = self.q.get()
            if self.muted:
                continue
            try:
                self._speak(text)
            except Exception:
                log("speak error", traceback.format_exc())

    def _speak(self, text):
        if not text:
            return
        self.stop_flag.clear()
        t0 = time.time()
        audio = synth(text)
        log(f"synth {len(audio or b'')} bytes in {time.time() - t0:.1f}s")
        if audio and not self.stop_flag.is_set():
            self.speaking.set()
            try:
                play_audio(audio, self.stop_flag)
            finally:
                self.speaking.clear()


def clean_for_speech(text):
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[A-Za-z]:\\[^\s,;]+", "a file", t)
    t = re.sub(r"(?<!\w)(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.\w+", "a file", t)
    t = re.sub(r"[*_#>|~]+", " ", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:900]


def first_sentences(text, n=2, limit=300):
    t = clean_for_speech(text)
    parts = re.split(r"(?<=[.!?])\s+", t)
    out = " ".join(parts[:n])
    return out[:limit]


def synth(text):
    eng = CFG.get("tts", "windows")
    try:
        if eng == "elevenlabs" and os.environ.get("ELEVENLABS_API_KEY"):
            return synth_elevenlabs(text)
        if eng == "edge":
            return synth_edge(text)
        if eng == "kokoro":
            return synth_kokoro(text)
    except Exception:
        log("tts engine failed, falling back to windows:", traceback.format_exc())
    return synth_windows(text)


def synth_windows(text):
    tmpdir = tempfile.mkdtemp(prefix="relay_")
    tin, tout = os.path.join(tmpdir, "in.txt"), os.path.join(tmpdir, "out.wav")
    with open(tin, "w", encoding="utf-8") as f:
        f.write(text)
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.Rate={int(CFG.get('windows_rate', 0))};"
        f"$s.SetOutputToWaveFile('{tout}');"
        f"$s.Speak([IO.File]::ReadAllText('{tin}'));$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=0x08000000, timeout=60)
    try:
        return open(tout, "rb").read()
    finally:
        for p in (tin, tout):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def synth_edge(text):
    import asyncio

    import edge_tts

    async def go():
        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, CFG.get("edge_voice")).stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)

    return asyncio.run(go())


_kokoro = {"k": None}


def synth_kokoro(text):
    """Local neural voice (Kokoro-82M, Apache 2.0). Model files live in models/ next to relay.py."""
    import io
    import wave

    if _kokoro["k"] is None:
        from kokoro_onnx import Kokoro
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        _kokoro["k"] = Kokoro(os.path.join(d, "kokoro-v1.0.onnx"), os.path.join(d, "voices-v1.0.bin"))
    voice = CFG.get("kokoro_voice", "bm_george")
    samples, sr = _kokoro["k"].create(text, voice=voice, speed=float(CFG.get("kokoro_speed", 1.0)),
                                      lang="en-gb" if voice[:1] == "b" else "en-us")
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def synth_elevenlabs(text):
    vid = CFG.get("elevenlabs_voice_id")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128"
    body = json.dumps({"text": text, "model_id": CFG.get("elevenlabs_model")}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=30).read()


def play_audio(data, stop_flag):
    snd = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16)
    samples, nch, sr = snd.samples, snd.nchannels, snd.sample_rate
    vol = float(CFG.get("volume", 1.0))
    if vol != 1.0:
        import array
        scaled = (np.frombuffer(samples, dtype=np.int16).astype(np.float32) * vol)
        samples = array.array("h", np.clip(scaled, -32768, 32767).astype(np.int16).tobytes())
    done = threading.Event()

    def gen():
        pos = 0
        required = yield b""
        while pos < len(samples) and not stop_flag.is_set():
            n = required * nch
            chunk = samples[pos:pos + n]
            pos += n
            required = yield chunk
        done.set()

    try:
        dev = miniaudio.PlaybackDevice(output_format=miniaudio.SampleFormat.SIGNED16,
                                       nchannels=nch, sample_rate=sr)
    except miniaudio.MiniaudioError:
        log("miniaudio device failed, using winsound")
        play_with_winsound(samples, nch, sr, stop_flag)
        return
    log(f"playing {len(samples) / (sr * nch):.1f}s on {dev.backend} / {getattr(dev, 'device_name', '?')}")
    g = gen()
    next(g)
    dev.start(g)
    deadline = time.time() + len(samples) / (sr * nch) + 1.0
    while not done.is_set() and not stop_flag.is_set() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.15)
    dev.close()


def play_with_winsound(samples, nch, sr, stop_flag):
    import wave
    path = os.path.join(tempfile.gettempdir(), f"relay_{os.getpid()}.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    end = time.time() + len(samples) / (sr * nch) + 0.3
    while time.time() < end and not stop_flag.is_set():
        time.sleep(0.05)
    winsound.PlaySound(None, winsound.SND_PURGE)


# --------------------------------------------------------------------------- hub state
speaker = Speaker()
state = {"attached": None, "last_speaker": None, "pending": {}}
state_lock = threading.Lock()


def chime():
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def announce(session_id, cwd, text):
    s = session_by_id(session_id)
    name = s["name"] if s else (os.path.basename(cwd or "") or "A session")
    if s and s["host"]:
        with state_lock:
            state["last_speaker"] = s["host"]
    if claude_in_front() and not CFG.get("speak_when_claude_focused"):
        chime()
        log("chime (Claude in front):", name, "|", text)
        return
    log("say:", name, "|", text)
    speaker.say(f"{name}. {text}")


def handle_event(ev):
    kind = ev.get("hook_event_name")
    sid = ev.get("session_id")
    cwd = ev.get("cwd")
    if ev.get("agent_id"):
        return  # subagent chatter stays silent
    if kind == "PostToolUse" and "wrap_up" in (ev.get("tool_name") or ""):
        ti = ev.get("tool_input") or {}
        with state_lock:
            state["pending"][sid] = {"recap": ti.get("recap") or "", "ask": ti.get("ask") or ""}
        return
    if kind == "Stop":
        if ev.get("stop_hook_active"):
            return
        with state_lock:
            p = state["pending"].pop(sid, None)
        if p and (p["recap"] or p["ask"]):
            text = (p["recap"] + " " + p["ask"]).strip()
        else:
            text = first_sentences(ev.get("last_assistant_message") or "")
        if text:
            announce(sid, cwd, text)
        return
    if kind == "Notification":
        nt = ev.get("notification_type") or ""
        if nt == "permission_prompt":
            announce(sid, cwd, "needs your approval.")
        elif nt in ("agent_needs_input", "elicitation_dialog"):
            announce(sid, cwd, "has a question. " + first_sentences(ev.get("message") or "", 1))
        return


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        self.send_response(204)
        self.end_headers()
        try:
            ev = json.loads(body or b"{}")
        except Exception:
            return
        if self.path == "/event":
            threading.Thread(target=self._safe, args=(handle_event, ev), daemon=True).start()
        elif self.path == "/say":
            speaker.say(ev.get("text") or "")

    def do_GET(self):
        body = json.dumps({"attached": state["attached"], "last_speaker": state["last_speaker"],
                           "muted": speaker.muted, "recording": recorder.active,
                           "sessions": load_sessions()}, indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _safe(fn, ev):
        try:
            fn(ev)
        except Exception:
            log("event error", traceback.format_exc())


# --------------------------------------------------------------------------- speech in
class Recorder:
    def __init__(self):
        self.active = False
        self.mode = None
        self.chunks = []
        self.dev = None

    def start(self, mode):
        speaker.stop()  # barge-in: talking cuts the hub off
        self.chunks, self.mode, self.active = [], mode, True

        def gen():
            _ = yield
            while True:
                data = yield
                if data:
                    self.chunks.append(bytes(data))

        dev_id, want = None, (CFG.get("mic") or "").lower()
        if want:
            for d in miniaudio.Devices().get_captures():
                if want in d["name"].lower():
                    dev_id = d["id"]
                    break
            else:
                log(f"mic '{want}' not found, using the Windows default")
        self.dev = miniaudio.CaptureDevice(input_format=miniaudio.SampleFormat.SIGNED16,
                                           nchannels=1, sample_rate=16000, buffersize_msec=200,
                                           device_id=dev_id)
        g = gen()
        next(g)
        self.dev.start(g)
        winsound.Beep(880, 80)
        log("recording", mode)

    def stop(self):
        if not self.active:
            return None, None
        time.sleep(0.25)  # keep the last word
        self.dev.close()
        self.active = False
        winsound.Beep(660, 80)
        pcm = np.frombuffer(b"".join(self.chunks), dtype=np.int16).astype(np.float32) / 32768.0
        peak = float(np.abs(pcm).max()) if len(pcm) else 0.0
        log(f"recorded {len(pcm) / 16000:.1f}s, peak {peak:.3f}")
        if peak < 0.005:
            log("the mic is silent: set \"mic\" in config.json to a microphone that works")
        elif peak < 0.5:
            pcm = pcm * min(0.5 / peak, 30.0)  # quiet mic: bring speech up to a level Whisper likes
        return pcm, self.mode


recorder = Recorder()
_model = {"m": None}
_model_ready = threading.Event()


def load_model():
    try:
        from faster_whisper import WhisperModel
        try:
            _model["m"] = WhisperModel(CFG["whisper_model"], device="cuda", compute_type="float16")
            log("whisper ready on GPU")
        except Exception:
            log("GPU failed, using CPU:", traceback.format_exc().splitlines()[-1])
            _model["m"] = WhisperModel(CFG["whisper_model"], device="cpu", compute_type="int8")
    finally:
        _model_ready.set()


FILLERS = re.compile(r"\b(?:um+|uh+|uhm+|erm+|hmm+)\b[,.]?\s*", re.I)


def transcribe(pcm):
    _model_ready.wait(60)
    if _model["m"] is None or len(pcm) < 16000 * 0.4:
        return ""
    names = ", ".join(sorted({s["name"] for s in load_sessions()}))
    segs, _ = _model["m"].transcribe(
        pcm, language=CFG.get("whisper_language"), beam_size=5, vad_filter=True,
        initial_prompt=f"Claude Code sessions: {names}." if names else None)
    text = " ".join(s.text.strip() for s in segs).strip()
    text = FILLERS.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


ATTACH_RE = re.compile(r"^(?:talk to|switch to|go to|attach to|connect to)\s+(.+?)[.!]?$", re.I)
TELL_RE = re.compile(r"^(?:tell|ask|message)\s+(.+)$", re.I)
STOP_RE = re.compile(r"^(?:stop|be quiet|shut up|quiet|cancel)[.!]?$", re.I)
LIST_RE = re.compile(r"^(?:list|what are|which are)(?: my| the)? sessions\??[.!]?$", re.I)


def split_target(rest):
    """'Payments, yes commit it' -> (session, 'yes commit it')."""
    m = re.match(r"^(.{1,60}?)[,:;]\s*(.+)$", rest)
    if m:
        s, score = find_session(m.group(1))
        if s:
            return s, m.group(2)
    words = rest.split()
    best = (None, 0.0, "")
    for i in range(1, min(6, len(words))):
        s, score = find_session(" ".join(words[:i]))
        if s and score > best[1]:
            best = (s, score, " ".join(words[i:]))
    return best[0], best[2]


def deliver(session, text):
    """Open the session in the Claude app and type the message, as the user would."""
    prev = fg_window()
    was_claude = window_proc(prev) == "claude.exe"
    os.startfile(f"claude://claude.ai/epitaxy/{session['host']}")
    t0 = time.time()
    while time.time() - t0 < 4 and not claude_in_front():
        time.sleep(0.1)
    if not claude_in_front():
        force_foreground(user32.FindWindowW(None, "Claude"))
        time.sleep(0.3)
    time.sleep(0.9)  # let the session view and its message box mount
    paste_text(text, press_enter=True)
    log("sent to", session["name"], "|", text)
    if CFG.get("return_focus_after_send") and not was_claude:
        time.sleep(0.4)
        force_foreground(prev)


def handle_talk(text):
    if not text:
        speaker.say("I didn't catch that.")
        return
    log("heard:", text)
    if STOP_RE.match(text):
        speaker.stop()
        return
    if LIST_RE.match(text):
        names = [s["name"] for s in load_sessions() if s["host"]]
        speaker.say("Your sessions are: " + ", ".join(names) + "." if names else "No sessions are open.")
        return
    m = ATTACH_RE.match(text)
    if m:
        s, _ = find_session(m.group(1))
        if s:
            with state_lock:
                state["attached"] = s["host"]
            speaker.say(f"Now talking to {s['name']}.")
        else:
            speaker.say(f"I couldn't find a session called {m.group(1)}.")
        return
    target, body = None, text
    m = TELL_RE.match(text)
    if m:
        target, body = split_target(m.group(1))
        if not target:
            target, body = None, text
    if target is None:
        with state_lock:
            hid = state["attached"] or state["last_speaker"]
        if not hid and claude_in_front():
            paste_text(text, press_enter=True)  # the session on screen
            log("sent to on-screen session |", text)
            return
        target = session_by_id(hid) if hid else None
    if not target or not target["host"]:
        speaker.say("Who should I send that to? Say talk to, then a session name.")
        return
    # Short spoken confirmation with a cancel window (Ctrl+Alt+X or the talk key cancels).
    cancel.clear()
    speaker.say_now(f"To {target['name']}.")
    if cancel.wait(CFG.get("cancel_window_sec", 1.5)):
        speaker.say("Cancelled.")
        return
    deliver(target, body)


cancel = threading.Event()


def on_hotkey(name):
    if name in ("talk", "dictate"):
        if recorder.active:
            pcm, mode = recorder.stop()
            threading.Thread(target=process_audio, args=(pcm, mode), daemon=True).start()
        else:
            cancel.set()  # pressing talk during the cancel window cancels the send
            recorder.start(name)
    elif name == "stop":
        cancel.set()
        speaker.stop()
        if recorder.active:
            recorder.stop()
    elif name == "mute":
        speaker.muted = not speaker.muted
        speaker.stop()
        winsound.Beep(440 if speaker.muted else 990, 120)
        log("muted" if speaker.muted else "unmuted")


def process_audio(pcm, mode):
    try:
        text = transcribe(pcm)
        if mode == "dictate":
            if text:
                paste_text(text, press_enter=False)
                log("dictated:", text)
        else:
            handle_talk(text)
    except Exception:
        log("audio error", traceback.format_exc())


MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B}


def parse_hotkey(s):
    mods, vk = 0x4000, 0  # MOD_NOREPEAT
    for part in s.lower().split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYS:
            vk = KEYS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
    return mods, vk


def hotkey_loop():
    names = {}
    for i, (name, combo) in enumerate(CFG["hotkeys"].items(), start=1):
        mods, vk = parse_hotkey(combo)
        if user32.RegisterHotKey(None, i, mods, vk):
            names[i] = name
        else:
            log(f"could not register hotkey {combo} ({name}); is another app using it?")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312 and msg.wParam in names:
            try:
                on_hotkey(names[msg.wParam])
            except Exception:
                log("hotkey error", traceback.format_exc())


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", CFG["port"]), Handler)
    except OSError:
        print("Relay is already running.")
        return
    threading.Thread(target=load_model, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("relay up on port", CFG["port"], "| tts:", CFG.get("tts"))
    if "--quiet" not in sys.argv:
        speaker.say("Relay is on.")
    hotkey_loop()


if __name__ == "__main__":
    main()
