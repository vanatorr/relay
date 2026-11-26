import numpy as np

from fakes import FakeClock, FakeVad
from relayvox import wake

LOUD = (np.sin(np.arange(1280) / 5) * 9000).astype(np.float32)
QUIET = np.zeros(1280, dtype=np.float32)


def make(score=0.9, vad=None, clock=None):
    calls = {"mel": 0, "wake": 0}

    def mel(x):
        calls["mel"] += 1
        return np.zeros((8, 32))

    def wk(x):
        calls["wake"] += 1
        assert x.shape == (1, 16, 96)
        return np.array([[score]])

    det = wake.Detector(
        mel, lambda x: np.zeros(96), wk, vad or FakeVad([1.0]), 0.5, clock or FakeClock()
    )
    return det, calls


def run(det, chunks):
    return [i for i, c in enumerate(chunks) if det.feed(c)]


def test_silence_runs_no_models():
    det, calls = make()
    assert run(det, [QUIET] * 50) == []
    assert calls == {"mel": 0, "wake": 0}


def test_loud_but_not_speech_is_gated_by_vad():
    det, calls = make(vad=FakeVad([0.0]))
    assert run(det, [LOUD] * 50) == []
    assert calls["mel"] == 0


def test_fires_once_then_refractory():
    clock = FakeClock()
    det, _ = make(clock=clock)
    fired = run(det, [LOUD] * 60)
    assert len(fired) == 1
    clock.advance(2.0)
    assert len(run(det, [LOUD] * 5)) == 1


def test_needs_two_frames_in_a_row():
    scores = iter([0.9, 0.0] * 100)

    def wk(x):
        return np.array([[next(scores)]])

    det = wake.Detector(
        lambda x: np.zeros((8, 32)), lambda x: np.zeros(96), wk, FakeVad([1.0]), 0.5, FakeClock()
    )
    assert run(det, [LOUD] * 80) == []


def test_low_score_never_fires():
    det, _ = make(score=0.2)
    assert run(det, [LOUD] * 60) == []


def test_preroll_is_fed_on_open():
    det, calls = make()
    run(det, [QUIET] * 20)
    det.feed(LOUD)
    assert calls["mel"] == wake.PREROLL_CHUNKS + 1


def test_paused_ignores_audio_and_tail():
    clock = FakeClock()
    det, calls = make(clock=clock)
    det.pause()
    det.pause()
    assert run(det, [LOUD] * 60) == []
    det.resume()
    assert det.paused()  # still one hold
    det.resume()
    assert det.paused()  # 300 ms tail
    assert run(det, [LOUD] * 60) == [] and calls["mel"] == 0
    clock.advance(0.31)
    assert not det.paused()
    assert len(run(det, [LOUD] * 60)) == 1


def test_resume_never_goes_negative():
    det, _ = make()
    det.resume()
    det.resume()
    det.pause()
    assert det.paused()
