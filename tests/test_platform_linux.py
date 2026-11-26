"""Linux platform: key map, tool discovery, typing argv, auto-repeat. No X server needed."""

from __future__ import annotations

import time
from typing import Any

import pytest

from relayvox.platform import linux
from relayvox.tts import system


def test_key_names_map_to_keysyms() -> None:
    by_sym, by_btn = linux.build_bindings(
        {"talk": "right_ctrl", "dictate": "right_alt", "mouse": "x1"}
    )
    assert by_sym[0xFFE4] == "talk"
    assert by_sym[0xFFEA] == "dictate" and by_sym[0xFE03] == "dictate"  # Alt_R and AltGr
    assert by_btn == {8: "mouse"}


def test_function_keys_and_empty_mouse() -> None:
    by_sym, by_btn = linux.build_bindings({"talk": "f13", "dictate": "f24", "mouse": ""})
    assert by_sym[0xFFBE + 12] == "talk" and by_sym[0xFFBE + 23] == "dictate"
    assert by_btn == {}


def test_unknown_key_name_rejected() -> None:
    with pytest.raises(ValueError):
        linux.build_bindings({"talk": "hyper_z"})


def test_find_tool_uses_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        linux.shutil, "which", lambda n: "/usr/bin/" + n if n == "xdotool" else None
    )
    assert linux.find_tool("xdotool") == "/usr/bin/xdotool"
    assert linux.find_tool("wtype") is None


def _fake_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(argv: list[str], data: str | None = None, timeout: float = 0) -> bool:
        calls.append(argv)
        return True

    monkeypatch.setattr(linux, "_run", fake)
    return calls


def test_type_text_x11_and_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux.shutil, "which", lambda n: "/bin/" + n)
    calls = _fake_run(monkeypatch)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert linux.type_text("hi") and linux.press_enter()
    assert calls[0] == ["xdotool", "type", "--clearmodifiers", "--", "hi"]
    assert calls[1] == ["xdotool", "key", "--clearmodifiers", "Return"]
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    linux.type_text("-x")
    assert calls[2] == ["wtype", "--", "-x"]


def test_missing_tools_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux.shutil, "which", lambda n: None)
    assert not linux.type_text("x") and not linux.press_enter() and not linux.copy_text("x")
    assert "missing" in linux.permissions()["typing"]


def test_wayland_has_no_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert linux.start_key_listener({"talk": "right_ctrl"}, lambda n, d: None) is False


def test_open_url_refuses_other_schemes() -> None:
    assert linux.open_url("https://example.com") is False


def test_reducer_reduces_and_drops_autorepeat() -> None:
    got: list[tuple[str, bool]] = []
    r = linux._Reducer({0xFFE4: "talk"}, {8: "mouse"}, lambda n, d: got.append((n, d)))
    r.key(0xFFE4, True, 100)
    r.key(0xFFE4, False, 200)  # auto-repeat pair, same timestamp
    r.key(0xFFE4, True, 200)
    r.key(0x61, True, 210)  # some other key
    r.key(0xFF1B, True, 220)  # Escape
    r.key(0xFFE4, False, 300)  # real release
    time.sleep(0.1)
    assert got == [("talk", True), ("other", True), ("esc", True), ("talk", False)]


def test_espeak_wav_parsing() -> None:
    pcm = b"\x01\x00\x02\x00"
    wav = (
        b"RIFF\xff\xff\xff\xffWAVEfmt "
        + (16).to_bytes(4, "little")
        + b"\x01\x00\x01\x00"
        + (22050).to_bytes(4, "little")
        + (44100).to_bytes(4, "little")
        + b"\x02\x00\x10\x00data\xff\xff\xff\xff"
        + pcm
    )
    assert system._linux_wav_pcm(wav) == (pcm, 22050)
    with pytest.raises(RuntimeError):
        system._linux_wav_pcm(b"")


def test_espeak_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: None)
    cfg: Any = {"tts": {"speed": 1.0}}
    with pytest.raises(RuntimeError, match="espeak-ng"):
        list(system._linux("hi", cfg))
