import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from fakes import FakeVad
from relayvox import config, listen
from relayvox.listen import Endpointer


def run_ep(ep, probs):
    out = []
    for i, p in enumerate(probs):
        e = ep.push(p)
        if e:
            out.append((i, e))
            if e in ("end", "timeout"):
                break
    return out


def test_vad_asset_input_names_and_behaviour():
    v = listen.Vad()
    assert v.input_names() == {"input", "h", "c"}
    assert v.prob(np.zeros(512, dtype=np.float32)) < 0.2


def test_endpoint_silence_mode_timing():
    ep = Endpointer("silence", 1000)
    ev = run_ep(ep, [1.0] * 12 + [0.0] * 40)
    kinds = [e for _, e in ev]
    assert kinds == ["speculate", "end"]
    spec_at, end_at = ev[0][0] - 11, ev[1][0] - 11  # frames of silence
    assert 600 <= spec_at * 32 < 640
    assert 1000 <= end_at * 32 < 1040


def test_speech_resumes_cancels_speculation():
    ep = Endpointer("silence", 1000)
    ev = run_ep(ep, [1.0] * 12 + [0.0] * 20 + [1.0] * 5 + [0.0] * 40)
    assert [e for _, e in ev] == ["speculate", "unspeculate", "speculate", "end"]


def test_no_speech_times_out():
    ev = run_ep(Endpointer(), [0.0] * 200)
    assert [e for _, e in ev] == ["timeout"]


def test_short_blip_is_not_speech():
    ev = run_ep(Endpointer(), [1.0] * 3 + [0.0] * 200)
    assert [e for _, e in ev] == ["timeout"]


def test_word_mode_checks_each_pause_and_ends_after_4s():
    ep = Endpointer("word")
    ev = run_ep(ep, [1.0] * 12 + [0.0] * 25 + [1.0] * 5 + [0.0] * 140)
    assert [e for _, e in ev] == ["check", "check", "end"]


def test_cancel_and_end_words():
    assert listen.is_cancel("Cancel.")
    assert listen.is_cancel("tell infra to restart, never mind")
    assert not listen.is_cancel("cancel the subscription flow please")
    assert listen.strip_end_word("restart nginx, send it.") == ("restart nginx", True)
    assert listen.strip_end_word("Over") == ("", True)
    assert listen.strip_end_word("moreover") == ("moreover", False)
    assert listen.strip_end_word("hello there") == ("hello there", False)


def test_auto_gain():
    sine = np.sin(np.arange(16000) / 7).astype(np.float32)
    quiet = sine * 0.05
    out = listen.apply_gain(quiet, 20)
    assert out is not None and abs(float(np.abs(out).max()) - 0.9) < 0.01
    capped = listen.apply_gain(sine * 0.01, 20)  # 20x cap: peak only reaches 0.2
    assert capped is not None and float(np.abs(capped).max()) == pytest.approx(0.2, abs=0.01)
    loud = sine * 0.8
    assert listen.apply_gain(loud) is loud
    assert listen.apply_gain(sine[:3000]) is None  # under 0.4 s
    assert listen.apply_gain(np.zeros(16000, dtype=np.float32)) is None
    assert listen.apply_gain(sine * 0.00005, 20) is None  # still below RMS floor


class FakeModel:
    def __init__(self, lang="fr"):
        self.calls = []
        self.lang = lang

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        return [SimpleNamespace(text=" euh bonjour Anthropic ")], SimpleNamespace(
            language=self.lang
        )


@pytest.fixture
def fake_whisper(monkeypatch):
    cfg = {k: dict(v) for k, v in config.DEFAULTS.items()}
    monkeypatch.setattr(listen, "_cfg", cfg)
    m = FakeModel()
    monkeypatch.setitem(listen._model, "m", m)
    listen._model_done.set()
    listen._cache.clear()
    return m


def test_transcribe_language_autodetect_and_vocab_prompt(fake_whisper):
    audio = np.ones(16000, dtype=np.float32) * 0.1
    assert listen.transcribe(audio, ["infra", "Relay router"]) == "bonjour Anthropic"
    kw = fake_whisper.calls[0]
    assert kw["language"] is None and kw["beam_size"] == 1
    p = kw["initial_prompt"]
    assert "Anthropic" in p and "Claude" in p and "Haiku" in p and "Opus" in p
    assert "infra" in p and "Relay router" in p
    assert listen.last_language() == "fr"


def test_vocabulary_is_configurable(fake_whisper, monkeypatch):
    listen._cfg["stt"]["vocabulary"] = ["Zorblax"]
    listen.transcribe(np.ones(16000, dtype=np.float32) * 0.1, [])
    p = fake_whisper.calls[0]["initial_prompt"]
    assert "Zorblax" in p and "Anthropic" not in p


def test_short_audio_is_not_transcribed(fake_whisper):
    assert listen.transcribe(np.ones(1000, dtype=np.float32), []) == ""
    assert fake_whisper.calls == []


def test_mic_pick_by_substring(monkeypatch):
    import miniaudio

    devs = [
        {"name": "Virtual Cable", "id": "a"},
        {"name": "Microphone (HyperX QuadCast)", "id": "b"},
    ]
    monkeypatch.setattr(miniaudio, "Devices", lambda: SimpleNamespace(get_captures=lambda: devs))
    said = []
    listen.set_notifier(said.append)
    assert listen.pick_mic("hyperx") == "b"
    assert listen.pick_mic("") is None
    assert listen.pick_mic("nonexistent") is None
    assert said == ["I couldn't find that microphone."]
    listen.set_notifier(None)


class FakeCapture:
    """Feeds scripted int16 chunks from a thread, like the audio callback would."""

    def __init__(self, cb):
        self.closed = False
        loud = (np.sin(np.arange(512) / 4) * 12000).astype(np.int16).tobytes()
        quiet = np.zeros(512, dtype=np.int16).tobytes()

        def feed():
            for b in [loud] * 14 + [quiet] * 60:
                if self.closed:
                    return
                cb(b)
                time.sleep(0.001)

        threading.Thread(target=feed, daemon=True).start()

    def close(self):
        self.closed = True


def test_hands_free_endpoint_fires_with_audio_and_uses_speculation(fake_whisper, monkeypatch):
    monkeypatch.setattr(listen, "_make_capture", FakeCapture)
    monkeypatch.setattr(listen, "Vad", lambda: FakeVad([1.0] * 14 + [0.0]))
    got = []
    done = threading.Event()
    listen.on_endpoint(lambda a: (got.append(a), done.set()))
    listen.start_recording("silence")
    assert done.wait(5)
    audio = got[0]
    assert audio is not None and len(audio) >= 14 * 512
    time.sleep(0.2)  # the speculative transcription ran on exactly this audio
    n = len(fake_whisper.calls)
    assert listen.transcribe(audio, []) == "bonjour Anthropic"
    assert len(fake_whisper.calls) == n  # cache hit, no second Whisper run
