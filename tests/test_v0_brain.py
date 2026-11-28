"""Tests for the v0 brain: recap store, fast-path questions, routing guards, brain process (mocked)."""
import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

if sys.platform != "win32":
    pytest.skip("relay.py (v0) is Windows only", allow_module_level=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
relay = pytest.importorskip("relay")

SESSIONS = [
    {"id": "a1", "host": "h1", "name": "olives chat 1", "cwd": "C:\\code\\olives-app", "status": "busy", "updated": 0},
    {"id": "b2", "host": "h2", "name": "Relay", "cwd": "C:\\code\\tools\\relay", "status": "idle", "updated": 0},
]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(relay, "load_sessions", lambda: [dict(s) for s in SESSIONS])
    relay.state["recaps"].clear()
    said = []
    monkeypatch.setattr(relay.speaker, "say", lambda text, **kw: said.append(text))
    monkeypatch.setattr(relay, "log", lambda *a: None)
    return said


@pytest.mark.parametrize("text", [
    "What did olives chat 1 say?", "what's olives chat 1 saying", "What did olives chat 1 just say",
    "what did relay just say before that", "Hey, what did Relay say?",
])
def test_said_matches(text):
    assert relay.SAID_RE.match(text)


@pytest.mark.parametrize("text", [
    "status", "Status.", "what's everyone doing?", "status on all the agents", "What's the status on the agents?",
    "how's everyone doing", "what are the agents doing",
])
def test_status_matches(text):
    assert relay.STATUS_RE.match(text)


@pytest.mark.parametrize("text", ["where's olives at?", "how's relay doing", "how is olives chat 1 doing?"])
def test_where_matches(text):
    assert relay.WHERE_RE.match(text)


@pytest.mark.parametrize("text,q", [
    ("Relay, what's blocking payments?", "what's blocking payments?"),
    ("Hey Relay what is going on", "what is going on"),
    ("hey brain, which is closest to done", "which is closest to done"),
    ("Brain, summarize", "summarize"),
])
def test_brain_prefix(text, q):
    assert relay.BRAIN_RE.match(text).group("q") == q


@pytest.mark.parametrize("text", ["tell Relay to commit", "tell relay, deploy", "ask Relay about it", "relays are cool"])
def test_not_brain_prefix(text):
    assert not relay.BRAIN_RE.match(text)


def test_said_question_is_answered_not_routed(env, monkeypatch):
    sent = []
    monkeypatch.setattr(relay, "deliver", lambda *a: sent.append(a))
    monkeypatch.setattr(relay, "paste_text", lambda *a, **k: sent.append(a))
    relay.remember("a1", "Built the map. Tests pass.")
    relay.remember("a1", "Started the paywall.")
    relay.handle_talk("What did olives chat 1 say?")
    relay.handle_talk("What did olives chat 1 just say before that?")
    assert env == ["olives chat 1. Started the paywall.", "olives chat 1. Built the map. Tests pass."]
    assert sent == []


def test_unknown_session_and_empty(env):
    relay.handle_talk("what did nonsense widgets say")
    relay.handle_talk("what did Relay say")
    assert env[0].startswith("I don't have a session called")
    assert env[1] == "Relay hasn't said anything yet."


def test_status_lists_busy_idle_and_first_sentence(env):
    relay.remember("a1", "Built the map. Tests pass.", now=time.time() - 300)
    relay.handle_talk("status on all the agents")
    out = env[0]
    assert "olives chat 1: busy, last line 5 minutes ago. Built the map." in out
    assert "Tests pass" not in out and "Relay: idle, nothing said yet." in out


def test_tell_relay_still_routes_to_session():
    target, body, bad = relay.parse_tell("tell Relay to commit it")
    assert target["name"] == "Relay" and body == "commit it" and not bad
    assert relay.answer_about_agents("tell Relay to commit it") is False


def test_recap_store_keeps_last_five():
    for i in range(8):
        relay.remember("a1", f"line {i}")
    assert [t for _, t in relay.lines_of(SESSIONS[0])] == [f"line {i}" for i in range(3, 8)]


def test_announce_records_even_when_claude_in_front(monkeypatch):
    monkeypatch.setattr(relay, "session_by_id", lambda sid: dict(SESSIONS[0]))
    monkeypatch.setattr(relay, "claude_in_front", lambda: True)
    monkeypatch.setitem(relay.CFG, "speak_when_claude_focused", False)
    monkeypatch.setattr(relay, "chime", lambda: None)
    relay.announce("a1", "C:\\x", "All done.")
    assert relay.lines_of(SESSIONS[0])[-1][1] == "All done."


def test_context_builder():
    relay.remember("a1", "Mail me at a@b.com about it", now=time.time() - 120)
    ctx = relay.build_brain_context([dict(s) for s in SESSIONS])
    assert "1, olives chat 1, olives-app, busy, 2 min since last line" in ctx
    assert "2, Relay, relay, idle, no lines yet" in ctx
    assert "an address" in ctx and "a@b.com" not in ctx


def test_brain_events_dropped(monkeypatch):
    relay.brain.sid = "brainsid"
    heard = []
    monkeypatch.setattr(relay, "announce", lambda *a: heard.append(a))
    relay.handle_event({"hook_event_name": "Stop", "session_id": "brainsid", "last_assistant_message": "hi"})
    relay.brain.sid = None
    assert heard == []


def test_hook_exits_on_brain_env():
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, os.path.join(ROOT, "hook.py")], input=b"{}",
                       env={**os.environ, "RELAYVOX_BRAIN": "1"}, capture_output=True, timeout=10)
    assert p.returncode == 0 and p.stdout == b"" and time.monotonic() - t0 < 5


class FakeProc:
    def __init__(self, lines):
        self.pid = 4242
        self.stdin = io.StringIO()
        self.stdout = iter(lines)

    def poll(self):
        return None

    def kill(self):
        pass


def delta(text):
    ev = {"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": text}}}
    return json.dumps(ev) + "\n"


def test_brain_streams_sentences_and_spawns_safely():
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "bs"}) + "\n",
             delta("Infra is busy. It is rest"), delta("arting nginx.\nPROPOSE 1: restart\n"),
             json.dumps({"type": "result", "result": "x"}) + "\n"]
    calls = {}

    def popen(argv, **kw):
        calls.update(argv=argv, **kw)
        return FakeProc(lines)

    b = relay.Brain(popen=popen, which=lambda n: "claude.exe")
    out = []
    b.ask("what is infra doing", "ctx", out.append)
    assert out == ["Infra is busy.", "It is restarting nginx."]
    assert calls["env"]["RELAYVOX_BRAIN"] == "1" and calls["cwd"].endswith("brain")
    a = calls["argv"]
    assert a[a.index("--tools") + 1] == "" and a[a.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in a and "haiku" in a
    sent = json.loads(b.proc.stdin.getvalue())
    assert "<context>\nctx\n</context>" in sent["message"]["content"]
    time.sleep(0.1)
    assert b.session_id() == "bs" and b.pid() == 4242


def test_brain_times_out(monkeypatch):
    monkeypatch.setattr(relay, "BRAIN_FIRST_TOKEN_SEC", 0.3)

    class Hang(FakeProc):
        def __init__(self):
            super().__init__([])
            self.stdout = iter(())  # replaced below; reader must block, so use a pipe-like blocker
            self.gate = threading.Event()
            self.stdout = self

        def __iter__(self):
            return self

        def __next__(self):
            self.gate.wait(3)
            raise StopIteration

    b = relay.Brain(popen=lambda argv, **kw: Hang(), which=lambda n: "claude.exe")
    monkeypatch.setattr(b, "restart_async", lambda: None)
    out = []
    b.ask("q", "ctx", out.append)
    assert out == ["The brain isn't answering, try again."]


def test_brain_disabled_without_claude_or_config():
    assert not relay.Brain(which=lambda n: None).enabled({})
    assert not relay.Brain(which=lambda n: "c").enabled({"brain_enabled": False})
    assert relay.Brain(which=lambda n: "c").enabled({})


def test_french_detector():
    assert relay.is_french("Bonjour, c'est terminé et les tests sont passés.")
    assert relay.is_french("Je n'ai pas fini, mais le build est prêt.")
    assert not relay.is_french("All done, the tests pass and the build is ready.")
    assert not relay.is_french("")


def test_held_body_sent_to_named_session(env, monkeypatch):
    sent = []
    monkeypatch.setattr(relay, "deliver", lambda s, body: sent.append((s["name"], body)))
    monkeypatch.setattr(relay, "claude_in_front", lambda: False)
    monkeypatch.setattr(relay, "session_by_id", lambda sid: None)
    monkeypatch.setattr(relay, "winsound", type("W", (), {"Beep": staticmethod(lambda *a: None)}))
    monkeypatch.setattr(relay.speaker, "say_now", lambda t: env.append(t))
    relay.state.update(attached=None, last_sent=None)
    for follow in ("Relay.", "Tell Relay."):
        sent.clear()
        relay.cancel.clear()
        relay.handle_talk("Another issue with the paywall")
        assert "Who should I send that to?" in env[-1] and sent == []
        relay.handle_talk(follow)
        assert sent == [("Relay", "Another issue with the paywall")]
    assert "To Relay." in env


def test_tell_with_no_body_asks(env):
    relay.state.pop("held", None)
    relay.handle_talk("Tell Relay.")
    assert env[-1] == "What should I tell Relay?" and relay.state["attached"] == "h2"
