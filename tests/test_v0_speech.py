"""Unit tests for relay.py (v0): speech cleaning, sentence splitting, pacing helpers, tell routing."""
import os
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("relay.py (v0) is Windows only", allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
relay = pytest.importorskip("relay")
clean = relay.clean_for_speech


def test_bullets_and_headings_become_sentences():
    md = "## Status\n\n- fixed the build\n* ran the tests\n1. pushed it\n"
    assert clean(md) == "Status.\nfixed the build. ran the tests. pushed it."


def test_paragraph_break_is_one_newline():
    out = clean("First part.\n\nSecond part")
    assert out == "First part.\nSecond part."
    assert relay.split_sentences(out) == [("First part.", True), ("Second part.", False)]


def test_existing_punctuation_kept():
    assert clean("Done!\nReady?\nNext:\n- one") == "Done! Ready? Next: one."


def test_table_rows_read_as_lists():
    out = clean("| Name | State |\n|---|---|\n| Relay | busy |")
    assert out == "Name, State. Relay, busy."


def test_ids_removed():
    out = clean("Session session_01AbCd and cse_9xYz plus cmsg_12 chan_abc "
                "id 123e4567-e89b-12d3-a456-426614174000 commit 3f2a9c1d in 1234567 words")
    for bad in ("session_", "cse_", "cmsg_", "chan_", "123e4567", "3f2a9c1d", "1234567"):
        assert bad not in out
    assert "words" in out


def test_plain_words_and_short_numbers_survive():
    out = clean("The defaced page took 25 seconds")
    assert "defaced" in out and "25" in out


def test_paths_and_links():
    out = clean(r"Edited C:\code\relay\relay.py and src/app/main.py, see https://example.com/x")
    assert "relay.py" not in out and "main.py" not in out and "example.com" not in out
    assert "a file" in out and "a link" in out


def test_symbols_and_abbreviations():
    out = clean("Build -> test \u2192 ship & e.g. fix, i.e. done")
    assert "to test" in out and "to ship" in out and "and" in out
    assert "for example" in out and "that is" in out
    assert "->" not in out and "&" not in out and "e.g" not in out


def test_limit_cuts_at_sentence_boundary():
    text = " ".join(f"This is sentence number {i}." for i in range(100))
    out = clean(text, limit=100)
    assert len(out) <= 100
    assert out.endswith(".")
    assert out == " ".join(f"This is sentence number {i}." for i in range(len(out.split(".")) - 1))


def test_limit_single_long_sentence_cut_at_word():
    out = clean("word " * 400, limit=50)
    assert len(out) <= 51 and out.endswith(".") and not out.endswith("wor.")


def test_short_text_unchanged_by_limit():
    assert clean("Short one.") == "Short one."


def test_empty():
    assert clean("") == "" and clean(None) == "" and clean("```\ncode\n```") == ""


def test_split_merges_tiny_sentences():
    assert relay.split_sentences("Yes. It worked fine today.") == [("Yes. It worked fine today.", False)]


def test_first_sentences_still_works():
    text = "- one thing is done\n- another is done\n- third"
    assert relay.first_sentences(text, 2) == "one thing is done. another is done."
    assert relay.first_sentences("Next: 1. I wait. Then more.", 1) == "Next: I wait."


def test_pause_lengths(monkeypatch):
    monkeypatch.setitem(relay.CFG, "sentence_pause_ms", 280)
    monkeypatch.setitem(relay.CFG, "paragraph_pause_ms", 600)
    assert relay.pause_after(0, 3, False) == 280
    assert relay.pause_after(1, 3, True) == 600
    assert relay.pause_after(2, 3, True) == 0


def test_silence_and_conform():
    import numpy as np
    assert len(relay.silence_pcm(500, 24000, 1)) == 12000
    stereo = np.ones(200, dtype=np.int16)
    mono = relay.conform_pcm(stereo, 2, 24000, 1, 24000)
    assert len(mono) == 100
    assert len(relay.conform_pcm(mono, 1, 12000, 1, 24000)) == 200


# ---- tell routing

SESSIONS = [{"id": "a", "host": "a", "name": "Relay", "cwd": "C:/code/relay"},
            {"id": "b", "host": "b", "name": "Payments fix", "cwd": "C:/code/pay"}]


@pytest.fixture
def sessions(monkeypatch):
    monkeypatch.setattr(relay, "load_sessions", lambda: SESSIONS)


def test_tell_exact(sessions):
    t, body, bad = relay.parse_tell("Tell payments fix, commit it")
    assert t["name"] == "Payments fix" and body == "commit it" and not bad


def test_misheard_tel(sessions):
    t, body, bad = relay.parse_tell("Tel Relay, yes it opened")
    assert t["name"] == "Relay" and body == "yes it opened" and not bad


def test_filler_and_fuzzy_name(sessions):
    t, body, bad = relay.parse_tell("Also tell really, there's an issue")
    assert t["name"] == "Relay" and body == "there's an issue"


def test_misheard_verb_without_session_is_unnamed(sessions):
    t, body, bad = relay.parse_tell("Till tomorrow we wait")
    assert t is None and body == "Till tomorrow we wait" and not bad


def test_exact_verb_without_session_reports_missing(sessions):
    t, body, bad = relay.parse_tell("Tell nobody about it")
    assert t is None and bad


def test_plain_text_not_a_command(sessions):
    assert relay.parse_tell("yes go ahead") == (None, "yes go ahead", False)


# ---- auto switch gating

def _gate(**kw):
    args = dict(host="b", shown="a", enabled=True, only_in_front=True, in_front=True,
                recording=False, is_replay=False)
    args.update(kw)
    return relay.should_auto_switch(**args)


def test_auto_switch_allowed():
    assert _gate()


@pytest.mark.parametrize("kw", [
    {"enabled": False}, {"host": None}, {"is_replay": True}, {"recording": True},
    {"host": "a"}, {"in_front": False},
])
def test_auto_switch_blocked(kw):
    assert not _gate(**kw)


def test_auto_switch_other_app_allowed_when_not_restricted():
    assert _gate(in_front=False, only_in_front=False)


@pytest.mark.parametrize("text,on", [("auto switch on", True), ("Auto switch off.", False),
                                     ("Turn on auto switch", True), ("turn off auto-switch.", False)])
def test_auto_switch_voice_command(text, on):
    m = relay.AUTO_SWITCH_RE.match(text)
    assert m and ((m.group(1) or m.group(2)).lower() == "on") == on
