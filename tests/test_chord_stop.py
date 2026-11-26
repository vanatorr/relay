import pytest

from relayvox import config, hotkeys
from relayvox.platform import chord, linux, windows


def test_parse_chord():
    c = chord.parse("Ctrl+Alt+X")
    assert c is not None and c.mods == {"ctrl", "alt"} and c.key == "x"
    assert chord.parse("") is None
    assert chord.parse("control+option+f9") == chord.Chord(frozenset({"ctrl", "alt"}), "f9")
    for bad in ("x", "ctrl+", "ctrl+ctrl+x", "foo+x", "ctrl+alt+enter"):
        with pytest.raises(ValueError):
            chord.parse(bad)


def test_tracker_fires_once_per_press():
    c = chord.parse("ctrl+alt+x")
    assert c is not None
    t = chord.Tracker(c)
    assert not t.feed("x", True)  # no modifiers yet
    t.feed("x", False)
    t.feed("ctrl", True)
    t.feed("alt", True)
    assert t.feed("x", True)
    assert not t.feed("x", True)  # auto-repeat
    t.feed("x", False)
    assert t.feed("x", True)
    t.feed("alt", False)
    t.feed("x", False)
    assert not t.feed("x", True)


def test_platform_name_maps():
    assert windows.chord_name(0xA3) == "ctrl" and windows.chord_name(0x58) == "x"
    assert linux.chord_name(0xFFE9) == "alt" and linux.chord_name(0x58) == "x"
    assert linux.chord_name(0x78) == "x"


def test_linux_reducer_emits_stop():
    got = []
    c = chord.parse("ctrl+alt+x")
    r = linux._Reducer({}, {}, lambda b, d: got.append((b, d)), c)
    r.key(0xFFE3, True, 1)
    r.key(0xFFE9, True, 2)
    r.key(0x78, True, 3)
    assert ("stop", True) in got


def test_stop_chord_reaches_resolve_keys_and_action():
    cfg = {"keys": {"talk": "f8", "dictate": "", "mouse": "", "stop": "ctrl+alt+x"}}
    assert hotkeys.resolve_keys(cfg)["stop"] == "ctrl+alt+x"
    got = []
    hotkeys._machines.clear()
    hotkeys._handle("stop", "stop", 0, lambda b, a: got.append((b, a)), None)
    assert got == [("stop", "stop")]
    hotkeys._machines.clear()


def test_stop_discards_open_recording():
    got = []
    hotkeys._machines.clear()
    hotkeys._machines["talk"] = hotkeys.Ptt(hotkeys.HELD, 0, True)
    hotkeys._handle("stop", "stop", 500, lambda b, a: got.append((b, a)), None)
    assert got == [("talk", "discard"), ("stop", "stop")]
    hotkeys._machines.clear()


def test_config_stop_default_and_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_CONFIG_DIR", str(tmp_path))
    assert config.load()["keys"]["stop"] == "ctrl+alt+x"
    (tmp_path / "config.toml").write_text('[hotkeys]\nstop = "ctrl+shift+s"\n', encoding="utf-8")
    assert config.load()["keys"]["stop"] == "ctrl+shift+s"
    (tmp_path / "config.toml").write_text(
        '[keys]\nstop = "ctrl+alt+q"\n[hotkeys]\nstop = "ctrl+shift+s"\n', encoding="utf-8"
    )
    assert config.load()["keys"]["stop"] == "ctrl+alt+q"
    (tmp_path / "config.toml").write_text('[keys]\nstop = "banana"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        config.load()


def test_defaults_speak_with_claude_focused_and_use_kokoro():
    d = config.DEFAULTS
    assert d["tts"]["engine"] == "kokoro" and d["tts"]["voice"] == "bm_george"
    assert d["speech"]["speak_when_claude_focused"] is True
