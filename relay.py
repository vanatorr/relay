"""Relay: voice for Claude Code sessions (Windows).

- Sessions speak: Claude Code hooks POST to /event; the hub speaks a short status line,
  one at a time, prefixed with the session's name. Speaks even while Claude is in front (set speak_when_claude_focused to false for a chime).
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


def should_auto_switch(host, shown, enabled, only_in_front, in_front, recording, is_replay):
    """Open the speaking session's chat? Never for replays, a session already on screen, while you
    record, or (by default) when another app is in front."""
    if not enabled or not host or is_replay or recording:
        return False
    if host == shown:
        return False
    return in_front or not only_in_front


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
        self.replay = self.current = None
        self.current_host = self.replay_host = None
        self.replay_t = self.current_t = 0.0
        self.cutoff = {}
        self.finished = self.is_replay = False
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text, priority=False, host=None):
        text = clean_for_speech(text)
        if not text or self.muted:
            return
        self.q.put((text, host, time.time()))

    def drop_from(self, host):
        """You just answered this session: its queued, held or playing lines are stale, so drop them."""
        if not host:
            return
        # Lines that arrived while you were talking are new to you, so they still play.
        self.cutoff[host] = recorder.started or time.time()
        if self.replay_host == host:
            self.replay = self.replay_host = None
        if self.current_host == host and self.current_t < self.cutoff[host]:
            self.stop_flag.set()

    def say_now(self, text):
        """Speak immediately, blocking the caller (used for short confirmations)."""
        self._speak(clean_for_speech(text))

    def stop(self):
        """Stop speaking and drop everything queued (the stop key, "be quiet", mute)."""
        self.stop_flag.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def drop_held(self):
        """Forget the line that was cut off when you started talking (you answered it)."""
        self.replay = self.replay_host = None

    def hush(self):
        """Cut the current line because you started talking; it and the rest replay afterwards."""
        # Replay only a line genuinely cut mid-synth/playback, and only once (never a replay of a replay).
        if self.current and not self.finished and not self.is_replay and not self.replay:
            self.replay, self.replay_host, self.replay_t = self.current, self.current_host, self.current_t
        self.stop_flag.set()

    def _run(self):
        self.replay = self.current = None
        while True:
            text, host, t = (self.replay, self.replay_host, self.replay_t) if self.replay else self.q.get()
            self.is_replay = bool(self.replay)
            while recorder.active:  # never talk over you or into the mic: hold updates until you're done
                time.sleep(0.1)
            if self.is_replay and not self.replay:
                continue  # the held line was dropped because you answered that session meanwhile
            self.replay = self.replay_host = None
            if self.muted or (host and t < self.cutoff.get(host, 0)):
                continue  # muted, or you already answered this session after it was said
            self.finished = False
            self.current, self.current_host, self.current_t = text, host, t
            try:
                if host:
                    maybe_auto_switch(host, self.is_replay)
                self._speak(text)
            except Exception:
                log("speak error", traceback.format_exc())
            finally:
                self.finished = True
                self.current = self.current_host = None

    def _speak(self, text):
        if not text:
            return
        self.stop_flag.clear()
        self.speaking.set()
        try:
            play_sentences(text, self.stop_flag)
        finally:
            self.speaking.clear()


_ID_RES = [
    re.compile(r"\b(?:session|cse|cmsg|chan)_[A-Za-z0-9_-]+", re.I),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.I),  # hex hash; needs a digit so "defaced" survives
    re.compile(r"\b\d{6,}\b"),
]
_ABBREV = [(re.compile(r"\be\.g\.,?", re.I), "for example,"), (re.compile(r"\bi\.e\.,?", re.I), "that is,")]


def _tidy_line(line):
    """One line of markdown as plain words ending like a sentence, or "" if nothing speakable is left."""
    line = re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)]|>)\s+", "", line)
    line = re.sub(r"^\s*#{1,6}\s*", "", line)
    if "|" in line:  # table row: read the cells as a list
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        line = ", ".join(c for c in cells if c)
    line = re.sub(r"[*_#>|~]+", " ", line)
    line = re.sub(r"\s+", " ", line).strip()
    line = re.sub(r"\s+([.,;:!?])", r"\1", line)
    line = line.rstrip(",; ")
    if not re.search(r"\w", line):
        return ""
    return line if line[-1] in ".!?:" else line + "."


def _cut_at_sentence(text, limit):
    """Keep whole sentences up to limit characters. Only one over-long first sentence is cut, at a word."""
    if len(text) <= limit:
        return text
    out, used, full = [], 0, True
    for para in text.split("\n"):
        kept = []
        sents = re.split(r"(?<=[.!?])\s+", para)
        for sent in sents:
            if used + len(sent) + (1 if used else 0) > limit:
                break
            kept.append(sent)
            used += len(sent) + 1
        if kept:
            out.append(" ".join(kept))
        if len(kept) < len(sents):
            break
    if out:
        return "\n".join(out)
    cut = text.replace("\n", " ")[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return (cut or text[:limit]) + "."


def clean_for_speech(text, limit=900):
    """Plain, speakable text. Each line, bullet, heading and table row ends like a sentence, and a
    paragraph break becomes one newline. Cut to the limit at a sentence boundary, never mid-sentence."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"```.*", " ", t, flags=re.S)  # unterminated block
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[A-Za-z]:\\[^\s,;]+", "a file", t)
    t = re.sub(r"(?<!\w)(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.\w+", "a file", t)
    for rx in _ID_RES:
        t = rx.sub("", t)
    for rx, word in _ABBREV:
        t = rx.sub(word, t)
    t = re.sub(r"\s*(?:->|=>|→)\s*", " to ", t)
    t = re.sub(r"\s*&\s*", " and ", t)
    paras, cur = [], []
    for raw in t.splitlines():
        if not raw.strip() or re.fullmatch(r"\s*(?:[-=_*]{3,}|\|?[\s:|-]+\|[\s:|-]*)\s*", raw):
            # blank line, horizontal rule or table separator row
            if cur and (not raw.strip() or "|" not in raw):
                paras.append(" ".join(cur))
                cur = []
            continue
        line = _tidy_line(raw)
        if line:
            cur.append(line)
            if re.match(r"\s*#{1,6}\s", raw):  # a heading stands alone, with a longer pause after it
                paras.append(" ".join(cur))
                cur = []
    if cur:
        paras.append(" ".join(cur))
    return _cut_at_sentence("\n".join(p for p in paras if p), limit)


def split_sentences(text):
    """[(sentence, ends_paragraph)] from clean_for_speech output. Very short ones join the next."""
    out = []
    for para in (text or "").split("\n"):
        merged = []
        for p in (p for p in re.split(r"(?<=[.!?])\s+", para.strip()) if p):
            if merged and len(merged[-1]) < 14:
                merged[-1] += " " + p
            else:
                merged.append(p)
        out += [(m, False) for m in merged]
        if merged:
            out[-1] = (out[-1][0], True)
    if out:
        out[-1] = (out[-1][0], False)
    return out


def first_sentences(text, n=2, limit=300):
    t = clean_for_speech(text, limit=5000).replace("\n", " ")
    t = re.sub(r"(^|[\s:])\d{1,2}[.)]\s+", r"\1 ", t)  # drop numbered-list markers ("next: 1. I wait")
    t = re.sub(r"\s{2,}", " ", t).strip()
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


def decode_pcm(data):
    """Any audio bytes the engines return -> (int16 numpy samples, channels, sample rate)."""
    snd = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16)
    return np.frombuffer(snd.samples, dtype=np.int16), snd.nchannels, snd.sample_rate


def conform_pcm(arr, nch, sr, want_nch, want_sr):
    """Match a clip to the first clip's channels and rate so clips can be joined (engines are
    consistent, so this is only a guard)."""
    if nch != want_nch:
        frames = arr.reshape(-1, nch).astype(np.float32).mean(axis=1)
        arr = np.repeat(frames, want_nch).astype(np.int16)
    if sr != want_sr:
        n = int(len(arr) // want_nch * want_sr / sr)
        x = np.linspace(0, 1, n, endpoint=False)
        frames = arr.reshape(-1, want_nch).astype(np.float32)
        src = np.linspace(0, 1, len(frames), endpoint=False)
        arr = np.stack([np.interp(x, src, frames[:, c]) for c in range(want_nch)], axis=1)
        arr = arr.astype(np.int16).reshape(-1)
    return arr


def silence_pcm(ms, sr, nch):
    return np.zeros(int(sr * ms / 1000) * nch, dtype=np.int16)


def pause_after(i, n, ends_paragraph):
    """Silence (ms) after sentence i of n."""
    if i >= n - 1:
        return 0
    return int(CFG.get("paragraph_pause_ms", 600)) if ends_paragraph else int(CFG.get("sentence_pause_ms", 280))


def play_sentences(text, stop_flag):
    """Speak text one sentence at a time with silence between them. The next sentence is synthesized
    while the current one plays; one output device carries the whole line; stop_flag cuts it at once."""
    sents = split_sentences(text)
    if not sents:
        return
    vol = float(CFG.get("volume", 1.0))
    buf, lock = bytearray(), threading.Lock()
    fmt, ready, finished = {}, threading.Event(), threading.Event()
    t0 = time.time()

    def produce():
        try:
            for i, (s, para_end) in enumerate(sents):
                if stop_flag.is_set():
                    return
                while fmt and len(buf) > 8 * fmt["sr"] * fmt["nch"] * 2 and not stop_flag.is_set():
                    time.sleep(0.05)  # enough queued ahead; wait for playback to catch up
                audio = synth(s)
                if not audio or stop_flag.is_set():
                    continue
                arr, nch, sr = decode_pcm(audio)
                if not fmt:
                    fmt.update(nch=nch, sr=sr)
                    log(f"first sentence ready in {time.time() - t0:.1f}s")
                else:
                    arr = conform_pcm(arr, nch, sr, fmt["nch"], fmt["sr"])
                if vol != 1.0:
                    arr = np.clip(arr.astype(np.float32) * vol, -32768, 32767).astype(np.int16)
                gap = silence_pcm(pause_after(i, len(sents), para_end), fmt["sr"], fmt["nch"])
                with lock:
                    buf.extend(arr.tobytes() + gap.tobytes())
                ready.set()
        except Exception:
            log("synth error", traceback.format_exc())
        finally:
            finished.set()
            ready.set()

    threading.Thread(target=produce, daemon=True).start()
    while not ready.wait(0.05):
        if stop_flag.is_set():
            return
    if not fmt or stop_flag.is_set():
        return
    nch, sr = fmt["nch"], fmt["sr"]
    done = threading.Event()

    def gen():
        import array
        required = yield b""
        while not stop_flag.is_set():
            need = required * nch * 2
            fin = finished.is_set()  # read before the buffer so a last append is never missed
            with lock:
                chunk = bytes(buf[:need])
                del buf[:need]
            if fin and not chunk:
                break
            if len(chunk) < need:
                chunk += bytes(need - len(chunk))  # next sentence not ready yet: silence
            required = yield array.array("h", chunk)
        done.set()

    try:
        dev = miniaudio.PlaybackDevice(output_format=miniaudio.SampleFormat.SIGNED16,
                                       nchannels=nch, sample_rate=sr)
    except miniaudio.MiniaudioError:
        log("miniaudio device failed, using winsound")
        while not finished.wait(0.05) and not stop_flag.is_set():
            pass
        if not stop_flag.is_set():
            import array
            play_with_winsound(array.array("h", bytes(buf)), nch, sr, stop_flag)
        return
    log(f"playing on {dev.backend} / {getattr(dev, 'device_name', '?')}")
    g = gen()
    next(g)
    dev.start(g)
    deadline = time.time() + 300
    while not done.wait(0.05) and not stop_flag.is_set() and time.time() < deadline:
        pass
    time.sleep(0.15)
    dev.close()


# --------------------------------------------------------------------------- earcons
_EARCONS = {}


def _earcon_path(name, notes):
    """Soft sine notes with a quick fade, written once to a temp wav: rising = start, falling = end."""
    import wave
    if name in _EARCONS:
        return _EARCONS[name]
    sr, out = 44100, []
    for hz in notes:
        n = int(sr * 0.09)
        t = np.arange(n) / sr
        env = np.minimum(1.0, t / 0.005) * np.exp(-t * 28)
        out.append(np.sin(2 * np.pi * hz * t) * env)
    pcm = (np.concatenate(out) * 0.35 * float(CFG.get("earcon_volume", 1.0)) * 32767).astype(np.int16)
    path = os.path.join(tempfile.gettempdir(), f"relay_{name}_{os.getpid()}.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    _EARCONS[name] = path
    return path


def earcon(kind):
    """start: rising C-G, end: falling G-C. Async so the mic opens without delay."""
    notes = {"start": (523, 784), "end": (784, 523)}[kind]
    try:
        winsound.PlaySound(_earcon_path(kind, notes), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        winsound.Beep(880 if kind == "start" else 440, 80)


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
state = {"attached": None, "last_speaker": None, "last_sent": None, "pending": {}, "spoken_recap": {},
         "shown": None, "auto_switch": bool(CFG.get("auto_switch", True))}
state_lock = threading.Lock()


def chime():
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def maybe_auto_switch(host, is_replay=False):
    with state_lock:
        enabled, shown = state["auto_switch"], state.get("shown")
    only = bool(CFG.get("auto_switch_only_when_claude_in_front", True))
    if not should_auto_switch(host, shown, enabled, only, claude_in_front() if enabled else False,
                              recorder.active, is_replay):
        return
    with state_lock:
        state["shown"] = host
    log("auto switch to", host)
    os.startfile(f"claude://claude.ai/epitaxy/{host}")
    time.sleep(0.4)  # let the chat open before its reply starts


def announce(session_id, cwd, text):
    s = session_by_id(session_id)
    name = s["name"] if s else (os.path.basename(cwd or "") or "A session")
    host = s["host"] if s else None
    with state_lock:
        # Only say the name when a different session speaks than the one you're already with.
        same = host is not None and host in (state["last_speaker"], state["attached"])
        if host:
            state["last_speaker"] = host
    spoken = text if same else f"{' '.join(name.split()[:3])}. {text}"
    if claude_in_front() and not CFG.get("speak_when_claude_focused", True):
        chime()
        log("chime (Claude in front):", name, "|", text)
        return
    log("say:", name, "|", text)
    speaker.say(spoken, host=host)


def handle_event(ev):
    kind = ev.get("hook_event_name")
    sid = ev.get("session_id")
    cwd = ev.get("cwd")
    if ev.get("agent_id"):
        return  # subagent chatter stays silent
    if kind == "PostToolUse" and "wrap_up" in (ev.get("tool_name") or ""):
        ti = ev.get("tool_input") or {}
        text = ((ti.get("recap") or "") + " " + (ti.get("ask") or "")).strip()
        with state_lock:
            state["spoken_recap"][sid] = bool(text)
        if text:
            announce(sid, cwd, text)
        return
    if kind == "Stop":
        if ev.get("stop_hook_active"):
            return
        with state_lock:
            already = state["spoken_recap"].pop(sid, False)
        if already:
            return  # the wrap_up recap was already spoken this turn
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
        self.started = 0.0
        self.mode = None
        self.chunks = []
        self.dev = None

    def start(self, mode):
        speaker.hush()  # barge-in: talking cuts speech off, but queued updates are kept
        self.chunks, self.mode, self.active = [], mode, True
        self.started = time.time()  # kept after stop: it dates the message being sent

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
        earcon("start")
        log("recording", mode)

    def stop(self):
        if not self.active:
            return None, None
        time.sleep(0.25)  # keep the last word
        self.dev.close()
        self.active = False
        earcon("end")
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
        pcm, language=CFG.get("whisper_language"), beam_size=1, vad_filter=True,
        condition_on_previous_text=False, without_timestamps=True,
        initial_prompt=f"Claude Code sessions: {names}." if names else None)
    text = " ".join(s.text.strip() for s in segs).strip()
    text = FILLERS.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


ATTACH_RE = re.compile(r"^(?:talk to|switch to|go to|attach to|connect to)\s+(.+?)[.!]?$", re.I)
# Optional filler, then the verb. Speech-to-text often mishears "tell" (tel, tall, till ...).
TELL_RE = re.compile(
    r"^(?:(?:also|and|so|ok|okay|hey|um|uh|yeah|yes|right|alright|now|please|i['’]ll|i\s+will|can\s+you|could\s+you|go)[,\s]+)*(tell|ask|message|tel|tal|tall|till|tale|tail)[,\s]+(.+)$",
    re.I)
TELL_EXACT = ("tell", "ask", "message")
AUTO_SWITCH_RE = re.compile(
    r"^(?:(?:turn|switch)\s+(on|off)\s+auto[\s-]?switch(?:ing)?|auto[\s-]?switch(?:ing)?\s+(on|off))[.!]?$", re.I)
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


def parse_tell(text):
    """(session, message, missing_name). An exact verb (tell, ask, message) with no matching session
    is reported as missing_name. A misheard verb ("till tomorrow ...") with no matching session is not
    a command at all, so the whole text comes back unnamed."""
    m = TELL_RE.match(text)
    if not m:
        return None, text, False
    target, body = split_target(m.group(2))
    if not target:
        return None, text, m.group(1).lower() in TELL_EXACT
    return target, re.sub(r"^to\s+", "", body, flags=re.I), False  # "tell X to stop" -> "stop"


def deliver(session, text):
    """Open the session in the Claude app and type the message, as the user would."""
    speaker.drop_from(session.get("host"))  # anything it was still saying is answered now
    prev = fg_window()
    was_claude = window_proc(prev) == "claude.exe"
    os.startfile(f"claude://claude.ai/epitaxy/{session['host']}")
    with state_lock:
        state["shown"] = session["host"]
    t0 = time.time()
    while time.time() - t0 < 4 and not claude_in_front():
        time.sleep(0.1)
    if not claude_in_front():
        force_foreground(user32.FindWindowW(None, "Claude"))
        time.sleep(0.3)
    time.sleep(0.5)  # let the session view and its message box mount
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
    m = AUTO_SWITCH_RE.match(text)
    if m:  # runtime only; config.json is not written
        on = (m.group(1) or m.group(2)).lower() == "on"
        with state_lock:
            state["auto_switch"] = on
        speaker.say("Auto switch on." if on else "Auto switch off.")
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
    target, body, bad_name = parse_tell(text)
    if bad_name:
        names = [s["name"] for s in load_sessions() if s["host"]]
        speaker.say("I didn't catch a session name. Your sessions are: " + ", ".join(names) + ".")
        return
    if target is None:
        # Unnamed: the session you're looking at, else the one you attached to or last sent to.
        # Never the one that spoke last: that silently sent messages to the wrong chat.
        if claude_in_front():
            speaker.drop_held()  # the line you talked over is the one you just answered
            paste_text(text, press_enter=True)
            log("sent to on-screen session |", text)
            return
        with state_lock:
            hid = state["attached"] or state.get("last_sent")
        target = session_by_id(hid) if hid else None
    if not target or not target["host"]:
        speaker.say("Who should I send that to? Say tell, then a session name.")
        return
    with state_lock:
        changed = state.get("last_sent") != target["host"]
        state["last_sent"] = target["host"]
    if changed:  # say who it goes to whenever that changes, so a wrong target is heard at once
        speaker.say_now(f"To {' '.join(target['name'].split()[:3])}.")
    # Short spoken confirmation with a cancel window (Ctrl+Alt+X or the talk key cancels).
    cancel.clear()
    log("to", target["name"], "(press stop to cancel)")
    winsound.Beep(1100, 60)  # a tick instead of a spoken "To {name}": saves ~3 s
    if cancel.wait(CFG.get("cancel_window_sec", 0.6)):
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
