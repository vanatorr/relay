"""app glue on fakes: temp dirs, FakePlatform, recorded deliver.send, a fake-voiced Speaker."""

import threading
import time

import pytest

from fakes import FakeClock, FakePlatform, FakePlayer, FakeSynth
from relayvox import app, brain, config, deliver, listen, route, speech, wake
from relayvox.route import Session


class _Wake:
    def pause(self):
        pass

    def resume(self):
        pass


INFRA = Session("s1", "local_infra1", "infra", "/work/infra", 11, 1, "idle", 1)
DOCS = Session("s2", "local_docs22", "docs site", "/work/docs", 12, 2, "idle", 2)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var, sub in (
        ("CLAUDE_CONFIG_DIR", "claude"),
        ("RELAYVOX_CONFIG_DIR", "config"),
        ("RELAYVOX_STATE_DIR", "state"),
    ):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    cfg = config.Config({k: dict(v) for k, v in config.DEFAULTS.items()})
    cfg["routing"]["cancel_sec"] = 0.01
    monkeypatch.setattr(app, "_S", app._State())
    monkeypatch.setattr(route, "_delivery_listeners", [])
    live = [INFRA, DOCS]
    monkeypatch.setattr(route, "sessions", lambda force=False: list(live))
    monkeypatch.setattr(listen, "transcribe", lambda audio, names, cache=True: audio)
    monkeypatch.setattr(listen, "last_language", lambda: "en")
    monkeypatch.setattr(wake, "pause", lambda: None)
    monkeypatch.setattr(wake, "resume", lambda: None)
    app.setup(cfg)

    plat = FakePlatform()
    clock = FakeClock()
    monkeypatch.setattr(deliver, "_platform", lambda: plat)
    monkeypatch.setattr(deliver, "_desktop_os", lambda: True)
    monkeypatch.setattr(deliver, "_sleep", clock.advance)
    monkeypatch.setattr(deliver, "_now", clock)
    sent = []
    real_send = deliver.send

    def recording_send(session, text):
        sent.append((session.id, text))
        return real_send(session, text)

    monkeypatch.setattr(deliver, "send", recording_send)

    synth = FakeSynth()
    sp = speech.Speaker(
        synth, FakePlayer(), cfg, tone_player=FakePlayer(), wake=_Wake(), claude_in_front=None
    )
    monkeypatch.setattr(speech, "_default_speaker", sp)

    class Env:
        pass

    e = Env()
    e.cfg, e.plat, e.sent, e.synth, e.sp, e.live = cfg, plat, sent, synth, sp, live
    return e


def spoken(e):
    assert wait_for(lambda: e.sp.pending() == 0)
    time.sleep(0.05)
    return list(e.synth.texts)


def test_delivery_drops_only_lines_from_before_the_recording(env):
    speech.barge()  # something else is recording; lines queue up
    speech.say("Old line from infra.", session="infra", session_id="s1")
    speech.say("Line from docs.", session="docs site", session_id="s2")
    time.sleep(0.05)
    app.on_action("talk", "barge")  # this recording starts: the cutoff
    time.sleep(0.05)
    speech.say("New line from infra.", session="infra", session_id="s1")
    app.on_utterance("tell infra to ship it", "talk")

    assert len(env.sent) == 1 and env.sent[0][0] == "s1" and "ship it" in env.sent[0][1]
    assert "open_url" in env.plat.names()
    texts = spoken(env)
    assert not any("Old line from infra" in t for t in texts)
    assert any("New line from infra" in t for t in texts)
    assert any("Line from docs" in t for t in texts)
    assert any("infra got it" in t.lower() for t in texts), texts
    assert app._S.rs.last_sent == "s1"


def test_target_name_spoken_only_when_it_changes(env):
    app.on_utterance("tell infra to ship it", "talk")
    app.on_utterance("tell infra to push it", "talk")
    assert len(env.sent) == 2
    assert sum("got it" in t for t in spoken(env)) == 1


def test_questions_never_reach_a_session(env):
    for q in ("status", "what's the status on infra", "recap infra", "repeat"):
        app.on_utterance(q, "talk")
    assert env.sent == []
    assert "open_url" not in env.plat.names()


def test_unclear_target_asks_and_an_unresolved_reply_is_not_sent(env):
    app.on_utterance("ship it", "talk")  # two live sessions, nothing sent before
    assert env.sent == [] and app._S.ask is not None
    app.on_utterance("neither", "talk")
    assert env.sent == [] and app._S.ask is None
    assert any("Not sent" in t for t in spoken(env))


def test_ask_reply_delivers_the_held_body(env):
    app.on_utterance("ship it", "talk")
    app.on_utterance("two", "talk")
    assert env.sent == [("s2", "ship it")]


def test_brain_proposal_needs_a_spoken_yes(env, monkeypatch):
    app._S.brain_on = True
    monkeypatch.setattr(brain, "build_context", lambda live, store, subject: "")
    monkeypatch.setattr(
        brain, "ask", lambda q, lang, ctx, on_sentence: brain.Proposal(1, "restart nginx")
    )
    app.on_utterance("Relay, should infra restart nginx?", "talk")
    assert env.sent == [] and app._S.proposal is not None
    assert any("Say yes" in t for t in spoken(env))
    app.on_utterance("no thanks", "talk")  # anything else drops it
    assert env.sent == [] and app._S.proposal is None
    app._S.ask = None  # "no thanks" became a normal message, which asked who it is for

    app.on_utterance("Relay, should infra restart nginx?", "talk")
    app._S.proposal.expires_at = app._clock() - 1  # past the 15 s window
    app.on_utterance("yes", "talk")
    assert env.sent == []
    app._S.ask = None

    app.on_utterance("Relay, should infra restart nginx?", "talk")
    app.on_utterance("Yes.", "talk")
    assert env.sent == [("s1", "restart nginx")]


def test_brain_off_answers_locally(env, monkeypatch):
    monkeypatch.setattr(brain, "ask", lambda *a: pytest.fail("brain must not run"))
    app.on_utterance("Relay, what is blocking docs?", "talk")
    assert env.sent == []


def test_cancel_tap_during_the_window_skips_quiet(env, monkeypatch):
    env.cfg["routing"]["cancel_sec"] = 2.0
    done = threading.Event()
    threading.Thread(
        target=lambda: (app.on_utterance("tell infra to ship it", "talk"), done.set()),
        daemon=True,
    ).start()
    assert wait_for(lambda: deliver._window_open)
    app.on_action("talk", "cancel_pending")
    app.on_action("talk", "quiet")
    assert done.wait(2)
    assert "open_url" not in env.plat.names()
    assert app._S.skip_quiet is False


def test_event_from_the_brain_session_is_silent(env, monkeypatch):
    monkeypatch.setattr(brain, "session_id", lambda: "brainsid")
    monkeypatch.setattr(brain, "pid", lambda: None)
    payload = {
        "session_id": "brainsid",
        "hook_event_name": "Stop",
        "last_assistant_message": "Done with it.",
    }
    app._on_event({"type": "event", "event": payload})
    assert env.sp.pending() == 0 and env.synth.texts == []
    payload["session_id"] = "s1"
    app._on_event({"type": "event", "event": payload})
    assert any("Done with it" in t for t in spoken(env))
    assert app._S.rs.last_spoke == "s1"


def test_ipc_status_ask_is_read_only(env):
    stop, ready = threading.Event(), threading.Event()
    t = threading.Thread(target=app.serve, args=(env.cfg, app.HANDLERS, stop, ready), daemon=True)
    t.start()
    assert ready.wait(2)
    try:
        reply = app.request({"type": "status", "ask": "status"})
        assert reply is not None and isinstance(reply["answer"], str) and reply["answer"]
        reply = app.request({"type": "status", "ask": "tell infra to ship it"})
        assert isinstance(reply["answer"], str)
        assert env.sent == [] and app._S.ask is None and app._S.proposal is None
        assert {"muted", "mic", "voice"} <= set(reply)
    finally:
        stop.set()
        t.join(2)
