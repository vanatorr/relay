"""macOS platform: only the pieces that need no Mac (keycode maps, chunking, permission words)."""

from __future__ import annotations

import pytest

from relayvox.platform import macos


def test_default_keys_resolve() -> None:
    by_code, by_button = macos.resolve_keys({"talk": "right_option", "dictate": "right_cmd"})
    assert by_code == {61: "talk", 54: "dictate"}
    assert by_button == {}


def test_empty_names_are_off_and_mouse_maps() -> None:
    by_code, by_button = macos.resolve_keys({"talk": "f8", "dictate": "", "mouse": "x2"})
    assert by_code == {100: "talk"}
    assert by_button == {4: "mouse"}


@pytest.mark.parametrize("bad", ["f24", "hyper", "enter"])
def test_unknown_key_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        macos.resolve_keys({"talk": bad})


def test_unknown_mouse_rejected() -> None:
    with pytest.raises(ValueError):
        macos.resolve_keys({"mouse": "x3"})


def test_modifier_keys_have_masks() -> None:
    for name in ("right_ctrl", "right_option", "right_cmd", "left_alt", "right_shift"):
        assert macos.MODIFIER_MASKS[macos.KEYCODES[name]] != 0


def test_chunking_limits_and_surrogates() -> None:
    text = "a" * 45 + "\U0001f600" * 12
    pieces = macos.chunk_utf16(text)
    assert "".join(pieces) == text
    for p in pieces:
        assert len(p.encode("utf-16-le")) // 2 <= macos.MAX_UNITS
    assert macos.chunk_utf16("") == []


def test_permission_words() -> None:
    assert macos.perm_word(True) == "ok"
    assert macos.perm_word(False) == "missing"
    assert macos.perm_word(None) == "unknown"
    assert macos.mic_word(3) == "ok"
    assert macos.mic_word(0) == "not_asked"
    assert macos.mic_word(2) == "missing"
    assert macos.mic_word(None) == "unknown"


def test_open_url_refuses_non_claude() -> None:
    assert macos.open_url("https://example.com") is False
    assert macos.open_url("claude://x y") is False
