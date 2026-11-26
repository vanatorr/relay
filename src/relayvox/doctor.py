"""Checks and the live key test (DESIGN section 12, 5.5). Read-only: changes nothing."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from typing import Any

OK, WARN, FAIL = "ok", "warn", "fail"
KEY_TEST_SEC = 10.0


def _line(level: str, name: str, detail: str, fix: str = "") -> str:
    tail = f" Fix: {fix}" if fix and level != OK else ""
    return f"{level:<4} {name}: {detail}.{tail}"


def _check(name: str, fn: Callable[[], tuple[str, str, str]]) -> tuple[str, str]:
    try:
        level, detail, fix = fn()
    except Exception as e:  # noqa: BLE001  a broken check is reported, never fatal
        level, detail, fix = WARN, f"check failed ({type(e).__name__})", ""
    return level, _line(level, name, detail, fix)


def _config() -> tuple[Any, tuple[str, str, str]]:
    from relayvox import config

    try:
        return config.load(), (OK, "valid", "")
    except ValueError as e:
        return config.Config(config.DEFAULTS), (FAIL, str(e), "edit the file `relayvox config`")


def _hooks() -> tuple[str, str, str]:
    from relayvox import install

    path = install.settings_path()
    data = install._load(path)  # read only; never writes or backs up
    if install._edit(data, True) == data:
        return OK, "installed for this Python", ""
    if any(install._is_ours(g) for v in (data.get("hooks") or {}).values() for g in v or []):
        return WARN, "old or other-Python hooks found", "relayvox install-hooks"
    return FAIL, "not installed", "relayvox install-hooks"


def _running() -> tuple[str, str, str]:
    from relayvox import app

    reply = app.request({"type": "status"})
    if reply is None:
        return WARN, "not running", "relayvox run"
    return OK, "running and answering", ""


def _mic(cfg: Any) -> tuple[str, str, str]:
    from relayvox import listen

    mics = listen.list_mics()
    if not mics:
        return FAIL, "no capture device", "plug in a microphone"
    want = str(cfg["audio"]["mic"]).lower()
    if want and not any(want in m.lower() for m in mics):
        return WARN, f"no device matches {want!r}", "relayvox mics, then set [audio] mic"
    return OK, f"{len(mics)} capture device(s)", ""


def _vad() -> tuple[str, str, str]:
    from relayvox import listen

    if os.path.exists(listen.vad_path()):
        return OK, "VAD asset present", ""
    return FAIL, "VAD asset missing", "reinstall faster-whisper"


def _voice(cfg: Any) -> tuple[str, str, str]:
    engine = str(cfg["tts"]["engine"])
    if engine == "kokoro":
        from relayvox.tts import kokoro

        d = kokoro.model_dir(cfg)
        onnx = "kokoro-v1.0.int8.onnx" if cfg["tts"].get("kokoro_small") else "kokoro-v1.0.onnx"
        missing = [n for n in (onnx, "voices-v1.0.bin") if not os.path.exists(os.path.join(d, n))]
        if missing:
            return WARN, f"Kokoro files missing in {d}", "they download on first use"
        return OK, f"Kokoro {cfg['tts']['voice']}", ""
    if engine in ("elevenlabs", "edge"):
        return WARN, f"{engine} is a cloud voice: spoken text leaves this machine", ""
    return OK, f"{engine} voice", ""


def _wake(cfg: Any) -> tuple[str, str, str]:
    if not cfg["wake"]["enabled"]:
        return OK, "off", ""
    from relayvox import wake

    if os.path.isdir(wake.wake_dir()) and os.listdir(wake.wake_dir()):
        return OK, f"on, models in {wake.wake_dir()}", ""
    return FAIL, "on but models missing", "relayvox models --wake"


def _platform() -> tuple[str, str, str]:
    from relayvox import platform

    perms = platform.permissions()
    bad = {k: v for k, v in perms.items() if v not in ("ok", "no", "not started")}
    detail = ", ".join(f"{k} {v}" for k, v in perms.items())
    if perms.get("claude_elevated") == "yes":
        return WARN, detail, "run Claude without admin rights, or Relay cannot type into it"
    return (WARN if bad else OK), detail, "grant the listed permissions"


def _delivery() -> tuple[str, str, str]:
    found = [n for n in ("claude", "tmux") if shutil.which(n)]
    if not found:
        return WARN, "neither claude nor tmux on PATH", "desktop app delivery still works"
    return OK, "found " + ", ".join(found), ""


def _brain(cfg: Any) -> tuple[str, str, str]:
    if not cfg["brain"]["enabled"]:
        return OK, "off", ""
    if shutil.which("claude") is None:
        return FAIL, "on but the claude CLI is missing", "install Claude Code or turn it off"
    return WARN, "on: recaps are sent to Claude when you ask Relay", ""


def _key_test(cfg: Any) -> None:
    from relayvox import hotkeys, platform

    keys = hotkeys.resolve_keys(cfg)
    names = ", ".join(f"{b}={k}" for b, k in keys.items())
    print(f"Key test for {KEY_TEST_SEC:.0f} s. Press: {names}")

    def sink(binding: str, is_down: bool) -> None:
        print(f"  {binding} {'down' if is_down else 'up'}", flush=True)

    if not platform.start_key_listener(keys, sink):
        print(_line(FAIL, "keys", "no key listener", "see the permissions line above"))
        return
    time.sleep(KEY_TEST_SEC)


def run(keys: bool = False) -> int:
    """Print one ok/warn/fail line per check with a fix; return exit code (section 12)."""
    cfg, cfg_result = _config()
    results = [(cfg_result[0], _line(cfg_result[0], "config", cfg_result[1], cfg_result[2]))]
    checks: list[tuple[str, Callable[[], tuple[str, str, str]]]] = [
        ("hooks", _hooks),
        ("relay", _running),
        ("mic", lambda: _mic(cfg)),
        ("vad", _vad),
        ("voice", lambda: _voice(cfg)),
        ("wake", lambda: _wake(cfg)),
        ("platform", _platform),
        ("delivery", _delivery),
        ("brain", lambda: _brain(cfg)),
    ]
    results += [_check(name, fn) for name, fn in checks]
    for _, text in results:
        print(text)
    if keys:
        _key_test(cfg)
    return 1 if any(level == FAIL for level, _ in results) else 0
