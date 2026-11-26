"""System voices: SAPI worker (Windows), say (macOS), espeak-ng (Linux). DESIGN section 6.4."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from typing import Any


def _windows(text: str, cfg: Any) -> Iterator[tuple[bytes, int]]:
    """One long-running PowerShell SAPI process; base64 lines in, temp WAV path out (6.4)."""
    import base64
    import contextlib
    import os
    import tempfile
    import wave

    text = text.strip()
    if not text:
        return
    try:
        speed = float(cfg["tts"].get("speed", 1.0))
    except (KeyError, TypeError, ValueError):
        speed = 1.0
    rate = max(-10, min(10, round((speed - 1.0) * 10)))
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="relay-sapi-")
    os.close(fd)
    try:
        # Only "<rate>|<path>|<base64 text>" crosses the pipe; no user text is ever in a command.
        line = f"{rate}|{path}|{base64.b64encode(text.encode('utf-8')).decode('ascii')}\n"
        with _sapi_lock:
            proc = _sapi_proc()
            watchdog = threading.Timer(30, proc.kill)
            watchdog.start()
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
                reply = proc.stdout.readline().strip()
            except OSError:
                reply = ""
            finally:
                watchdog.cancel()
            if reply != "OK":
                proc.kill()  # the next call starts a fresh worker
                raise RuntimeError("Windows speech worker failed")
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise RuntimeError("unexpected SAPI audio format")
            yield w.readframes(w.getnframes()), w.getframerate()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


_SAPI_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, 16, 1)
[Console]::Out.WriteLine('READY'); [Console]::Out.Flush()
while ($null -ne ($line = [Console]::In.ReadLine())) {
  try {
    $p = $line.Split('|')
    $s.Rate = [int]$p[0]
    $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p[2]))
    $s.SetOutputToWaveFile($p[1], $fmt)
    $s.Speak($text)
    $s.SetOutputToNull()
    [Console]::Out.WriteLine('OK')
  } catch { [Console]::Out.WriteLine('ERR') }
  [Console]::Out.Flush()
}
"""
_sapi_lock = threading.Lock()
_sapi_atexit: list[bool] = []
_sapi: Any = None  # the warm powershell.exe Popen


def _sapi_proc() -> Any:
    """The warm SAPI worker, started on first use and again if it died (caller holds the lock)."""
    import atexit
    import base64
    import os
    import subprocess

    global _sapi
    if _sapi is not None and _sapi.poll() is None:
        return _sapi
    exe = os.path.join(
        os.environ.get("SYSTEMROOT", r"C:\Windows"),
        r"System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    encoded = base64.b64encode(_SAPI_SCRIPT.encode("utf-16-le")).decode("ascii")
    proc: Any = subprocess.Popen(  # noqa: S603 - fixed argv, fixed script
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="ascii",
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    watchdog = threading.Timer(30, proc.kill)
    watchdog.start()
    try:
        ready = proc.stdout.readline().strip()
    finally:
        watchdog.cancel()
    if ready != "READY":
        proc.kill()
        raise RuntimeError("Windows speech worker did not start")
    _sapi = proc
    if not _sapi_atexit:
        atexit.register(lambda: _sapi is not None and _sapi.kill())
        _sapi_atexit.append(True)
    return proc


def _macos(text: str, cfg: Any) -> Iterator[tuple[bytes, int]]:
    """`say -o tmp.wav --data-format=LEI16@22050 -f -` (section 6.4). Untested on hardware."""
    import contextlib
    import os
    import subprocess
    import tempfile
    import wave

    tts = cfg.get("tts", {}) if hasattr(cfg, "get") else {}
    wpm = max(90, min(400, int(175 * float(tts.get("speed", 1.0)))))
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="relay-say-")
    os.close(fd)
    try:
        # The text goes on stdin (-f -), never into argv.
        subprocess.run(  # noqa: S603
            ["say", "-r", str(wpm), "-o", path, "--data-format=LEI16@22050", "-f", "-"],  # noqa: S607
            input=text.encode("utf-8"),
            check=True,
            timeout=30,
            capture_output=True,
        )
        with wave.open(path, "rb") as w:
            yield w.readframes(w.getnframes()), w.getframerate()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _linux_wav_pcm(wav: bytes) -> tuple[bytes, int]:
    """PCM and sample rate from espeak-ng's WAV (its streamed header can carry a bogus size)."""
    if len(wav) < 44 or wav[:4] != b"RIFF":
        raise RuntimeError("espeak-ng returned no audio")
    rate = int.from_bytes(wav[24:28], "little")
    pos = 12
    while pos + 8 <= len(wav):
        size = int.from_bytes(wav[pos + 4 : pos + 8], "little")
        if wav[pos : pos + 4] == b"data":
            pcm = wav[pos + 8 :]
            return pcm[: len(pcm) // 2 * 2], rate
        pos += 8 + size + (size & 1)
    raise RuntimeError("espeak-ng returned no audio")


def _linux(text: str, cfg: Any) -> Iterator[tuple[bytes, int]]:
    """`espeak-ng --stdin --stdout` (section 6.4)."""
    import shutil
    import subprocess

    exe = shutil.which("espeak-ng")
    if exe is None:
        raise RuntimeError("espeak-ng not found; install it (apt install espeak-ng)")
    try:
        speed = float(cfg["tts"].get("speed", 1.0))
    except (KeyError, TypeError, ValueError):
        speed = 1.0
    argv = [exe, "--stdin", "--stdout", "-v", "en-gb", "-s", str(int(175 * speed))]
    done = subprocess.run(  # noqa: S603 - fixed argv; the text goes on stdin, not the command
        argv, input=text.encode(), capture_output=True, timeout=30, check=False
    )
    if done.returncode != 0:
        raise RuntimeError("espeak-ng failed")
    pcm, rate = _linux_wav_pcm(done.stdout)
    for i in range(0, len(pcm), 8192):
        yield pcm[i : i + 8192], rate


def synth(text: str, cfg: Any) -> Iterator[tuple[bytes, int]]:
    """Dispatch to the per-OS function (section 6.4)."""
    if sys.platform == "win32":
        return _windows(text, cfg)
    if sys.platform == "darwin":
        return _macos(text, cfg)
    return _linux(text, cfg)
