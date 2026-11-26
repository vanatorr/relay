import threading
import time

import pytest

from fakes import FakeClock, FakePlayer, FakeSynth
from relayvox import config, speech


class BlockingPlayer(FakePlayer):
    """The first play() blocks until stop(), like a line cut mid-playback."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self._stop_evt = threading.Event()
        self._first = True

    def play(self, pcm, rate):
        self.played.append(pcm)
        if self._first:
            self._first = False
            self.started.set()
            self._stop_evt.wait(3)
        return True

    def stop(self):
        super().stop()
        self._stop_evt.set()


class FakeWake:
    def __init__(self):
        self.log = []

    def pause(self):
        self.log.append("pause")

    def resume(self):
        self.log.append("resume")


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def make(player=None, clock=None, front=None, **speech_cfg):
    cfg = {k: dict(v) for k, v in config.DEFAULTS.items()}
    cfg["speech"].update(speech_cfg)
    synth = FakeSynth()
    p = player or FakePlayer()
    sp = speech.Speaker(
        synth,
        p,
        cfg,
        tone_player=FakePlayer(),
        clock=clock or time.monotonic,
        wake=FakeWake(),
        claude_in_front=front,
    )
    return sp, synth, p


def settle(sp):
    assert wait_for(lambda: sp.pending() == 0)
    time.sleep(0.03)


def test_updates_held_during_recording_play_after_in_order():
    sp, synth, _ = make()
    sp.barge()
    sp.say("First update from one.", session="Infra", session_id="s1")
    sp.say("Second update from the same one.", session="Infra", session_id="s1")  # kept
    sp.say("Third update from another one.", session="Web", session_id="s2")
    time.sleep(0.1)
    assert synth.texts == []
    sp.release()
    settle(sp)
    assert synth.texts == [
        "Infra. First update from one.",
        "Second update from the same one.",
        "Web. Third update from another one.",
    ]


def test_needs_you_plays_first_after_release():
    sp, synth, _ = make()
    sp.barge()
    sp.say("A status line to hear.", session="A")
    sp.say("needs your approval.", priority="needs_you", session="B")
    sp.release()
    settle(sp)
    assert synth.texts == ["B. Needs your approval.", "A. A status line to hear."]


def test_name_only_when_speaker_changes():
    sp, synth, _ = make()
    sp.say("Done with the build step.", session="Infra")
    settle(sp)
    sp.say("Tests are passing now ok.", session="Infra")
    settle(sp)
    sp.say("Needs your approval.", session="Web app builder helper")
    settle(sp)
    assert synth.texts == [
        "Infra. Done with the build step.",
        "Tests are passing now ok.",
        "Web app builder. Needs your approval.",
    ]


def test_barge_replays_cut_line_once_and_keeps_queue():
    p = BlockingPlayer()
    sp, synth, _ = make(player=p)
    sp.say("The long line being read out.", session="Infra")
    sp.say("A newer queued update here.", session="Infra")
    assert p.started.wait(3)
    sp.barge()  # cut mid-playback
    time.sleep(0.1)
    sp.release()
    settle(sp)
    assert synth.texts == [
        "Infra. The long line being read out.",
        "Infra. The long line being read out.",  # replayed from its start, same text
        "A newer queued update here.",
    ]


def test_idle_barge_is_noop_and_double_barge_replays_once():
    sp, synth, _ = make()
    sp.say("Short line that finished.", session="A")
    settle(sp)
    sp.barge()  # nothing playing: nothing requeued
    sp.barge()
    sp.release()
    time.sleep(0.1)
    assert synth.texts == ["A. Short line that finished."]

    p = BlockingPlayer()
    sp, synth, _ = make(player=p)
    sp.say("A line cut twice over.", session="A")
    assert p.started.wait(3)
    sp.barge()
    sp.barge()  # idempotent
    sp.release()
    settle(sp)
    assert synth.texts.count("A. A line cut twice over.") == 2


def test_quiet_clears_queue_and_stops():
    sp, synth, p = make()
    sp.barge()
    sp.say("One queued update line.", session="A")
    sp.say("Two queued update line.", session="A")
    sp.quiet()
    sp.release()
    time.sleep(0.1)
    assert synth.texts == [] and p.stopped >= 1


def test_needs_you_cuts_status_which_replays_after():
    p = BlockingPlayer()
    sp, synth, _ = make(player=p)
    sp.say("A status being spoken now.", session="A")
    assert p.started.wait(3)
    sp.say("has a question.", priority="needs_you", session="B")
    settle(sp)
    assert synth.texts == [
        "A. A status being spoken now.",
        "B. Has a question.",
        "A. A status being spoken now.",
    ]


def test_answers_play_before_held_updates():
    sp, synth, _ = make()
    sp.barge()
    sp.say("Held update one here.", session="A")
    sp.say("Held needs you here.", priority="needs_you", session="B")
    sp.answer("Infra, busy. Working on it.")
    sp.release()
    settle(sp)
    assert synth.texts[0] == "Infra, busy. Working on it."
    assert synth.texts[1].startswith("B.")
    assert sp.last_spoken() == "A. Held update one here."


def test_fold_over_eight_pending_is_spoken_not_silent():
    sp, synth, _ = make()
    sp.barge()
    for i in range(11):
        sp.say(f"Update number {i} is ready.", session=f"S{i}", session_id=f"id{i}")
    sp.release()
    settle(sp)
    assert len(synth.texts) == 8
    assert any("more updates. Say status to hear them." in t for t in synth.texts)
    assert "S10. Update number 10 is ready." in synth.texts


def test_mute_holds_but_tones_still_play():
    sp, synth, _ = make()
    tp = sp.tone_player
    sp.set_mute(True)
    sp.say("Held while muted line.", session="A")
    sp.tone("tick")
    assert wait_for(lambda: len(tp.played) == 1)
    time.sleep(0.05)
    assert synth.texts == []
    sp.set_mute(False)
    settle(sp)
    assert len(synth.texts) == 1


def test_claude_focused_false_gives_chime_only():
    sp, synth, _ = make(front=lambda: True, speak_when_claude_focused=False)
    sp.say("Silent because focused.", session="A")
    settle(sp)
    assert synth.texts == [] and len(sp.tone_player.played) == 1


def test_claude_in_front_is_ignored_by_default():
    calls = []
    sp, synth, _ = make(front=lambda: calls.append(1) or True)
    sp.say("Spoken even when focused.", session="A")
    settle(sp)
    assert len(synth.texts) == 1 and calls == []


def test_lines_are_kept_short():
    sp, synth, _ = make()
    sp.say("One. Two. Three. Four.", session="A")
    settle(sp)
    assert synth.texts == ["A. One. Two."]


def test_needs_you_repeats_once_after_two_minutes_unless_cleared():
    clock = FakeClock()
    sp, synth, _ = make(clock=clock)
    sp.say("needs your approval.", priority="needs_you", session="A", session_id="s1")
    settle(sp)
    clock.advance(121)
    with sp._cv:
        sp._cv.notify_all()
    assert wait_for(lambda: len(synth.texts) == 2)

    sp2, synth2, _ = make(clock=clock)
    sp2.say("needs your approval.", priority="needs_you", session="A", session_id="s1")
    settle(sp2)
    sp2.clear_repeat("A")
    clock.advance(121)
    with sp2._cv:
        sp2._cv.notify_all()
    time.sleep(0.15)
    assert len(synth2.texts) == 1


def test_wake_paused_while_speaking():
    sp, _, _ = make()
    sp.say("Wake should pause now.", session="A")
    settle(sp)
    assert wait_for(lambda: sp.wake.log == ["pause", "resume"])


def test_drop_from_removes_only_that_sessions_lines_incl_replay():
    p = BlockingPlayer()
    sp, synth, _ = make(player=p)
    sp.say("Cut line from session one.", session="One", session_id="s1")
    assert p.started.wait(3)
    sp.barge()  # s1's line is now a held replay in the queue
    sp.say("Another from session one.", session="One", session_id="s1")
    sp.say("A line from session two.", session="Two", session_id="s2")
    sp.say("needs your approval.", priority="needs_you", session="One", session_id="s1")
    sp.drop_from("s1")
    sp.release()
    settle(sp)
    assert synth.texts[1:] == ["Two. A line from session two."]


def test_drop_from_cutoff_keeps_lines_that_arrived_while_recording():
    clock = FakeClock(1000.0)
    sp, synth, _ = make(clock=clock)
    sp.barge()  # hold the queue, as while recording
    sp.say("Old line from session one.", session="One", session_id="s1")
    sp.say("Old question from one.", priority="needs_you", session="One", session_id="s1")
    sp.say("Line from session two.", session="Two", session_id="s2")
    clock.advance(1.0)
    cutoff = clock()  # the recording of the delivered message started here
    clock.advance(2.0)
    sp.say("New line from session one.", session="One", session_id="s1")
    sp.drop_from("s1", before=cutoff)
    sp.release()
    settle(sp)
    assert synth.texts == ["Two. Line from session two.", "One. New line from session one."]
    clock.advance(speech.REPEAT_AFTER_S + 1)
    sp.say("Wake the queue.", session="Two", session_id="s2")
    settle(sp)
    assert not any("Old question" in t for t in synth.texts)  # its repeat went too


def test_drop_from_cuts_current_line_of_that_session_only():
    p = BlockingPlayer()
    sp, synth, _ = make(player=p)
    sp.say("Playing line from one.", session="One", session_id="s1")
    sp.say("Queued line from two.", session="Two", session_id="s2")
    assert p.started.wait(3)
    sp.drop_from("s1")  # stops playback of its current line, no replay
    settle(sp)
    assert synth.texts == ["One. Playing line from one.", "Two. Queued line from two."]

    p2 = BlockingPlayer()
    sp2, _, _ = make(player=p2)
    sp2.say("Playing line from one.", session="One", session_id="s1")
    assert p2.started.wait(3)
    sp2.drop_from("other")  # a different session: current line untouched
    time.sleep(0.05)
    assert p2.stopped == 0
    p2.stop()


# ---- tts layer ----


def _cfg():
    return {k: dict(v) for k, v in config.DEFAULTS.items()}


def test_tts_falls_back_to_system_then_chime(monkeypatch):
    from relayvox import tts
    from relayvox.tts import kokoro, system

    def boom(*a, **k):
        raise RuntimeError("no model")
        yield

    monkeypatch.setattr(kokoro, "synth", boom)
    monkeypatch.setattr(system, "synth", lambda t, c: iter([(b"\x01\x00", 22050)]))
    assert list(tts.synth("hi", _cfg())) == [(b"\x01\x00", 22050)]

    def unbuilt(t, c):
        raise NotImplementedError

    monkeypatch.setattr(system, "synth", unbuilt)
    chunks = list(tts.synth("hi", _cfg()))
    assert len(chunks) == 1 and chunks[0][1] == 24000  # the chime


def test_tts_never_restarts_a_half_played_line(monkeypatch):
    from relayvox import tts
    from relayvox.tts import kokoro, system

    def half(*a, **k):
        yield (b"\x00\x00", 24000)
        raise RuntimeError("died")

    monkeypatch.setattr(kokoro, "synth", half)
    monkeypatch.setattr(system, "synth", lambda t, c: pytest.fail("fell back mid-line"))
    assert list(tts.synth("hi", _cfg())) == [(b"\x00\x00", 24000)]


def test_tts_french_and_persian_routing(monkeypatch):
    from relayvox import tts

    cfg = _cfg()
    assert tts._plan("fr", cfg) == ("kokoro", {"voice": "ff_siwis", "lang": "fr-fr"})
    assert tts._plan("en", cfg) == ("kokoro", {})
    assert tts._plan("fa", cfg) == ("kokoro", {})  # English, offline
    tts._noted.clear()
    assert "answer in English" in tts.language_note("fa", cfg)
    assert tts.language_note("fa", cfg) is None  # once per run
    cfg["tts"]["persian"] = "edge"
    assert tts._plan("fa", cfg) == ("edge", {"voice": "fa-IR-FaridNeural"})


def test_elevenlabs_key_never_leaks(monkeypatch, caplog):
    import io
    import urllib.error

    from relayvox.tts import elevenlabs

    secret = "sk_SECRET_KEY_12345"  # noqa: S105
    monkeypatch.setenv("ELEVENLABS_API_KEY", secret)

    def deny(req, timeout=0):
        assert req.get_header("Xi-api-key") == secret
        raise urllib.error.HTTPError(req.full_url, 401, "bad", {}, io.BytesIO(b"x"))

    monkeypatch.setattr("urllib.request.urlopen", deny)
    with pytest.raises(RuntimeError) as e:
        list(elevenlabs.synth("hi", _cfg()))
    assert secret not in str(e.value) and secret not in caplog.text
    assert secret not in repr(e.value.__cause__)


def test_elevenlabs_pcm_stays_int16_aligned(monkeypatch):
    import io

    from relayvox.tts import elevenlabs

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")

    class Resp(io.BytesIO):
        def read(self, n=-1):
            return super().read(3)  # odd-sized reads

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: Resp(bytes(range(10))))
    out = b"".join(c for c, _ in elevenlabs.synth("hi", _cfg()))
    assert out == bytes(range(10))
    assert all(len(c) % 2 == 0 for c, _ in elevenlabs.synth("hi", _cfg()))
