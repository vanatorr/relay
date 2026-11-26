import threading

import pytest

from fakes import FakeClock, FakePlatform
from relayvox import deliver
from relayvox.route import Session


def mk(host_id="local_abc123", pid=100, name="infra"):
    return Session("sid", host_id, name, "C:/x", pid, 1, "idle", 1)


@pytest.fixture
def env(monkeypatch):
    clock = FakeClock()
    plat = FakePlatform()
    monkeypatch.setattr(deliver, "_platform", lambda: plat)
    monkeypatch.setattr(deliver, "_desktop_os", lambda: True)
    monkeypatch.setattr(deliver, "_sleep", clock.advance)
    monkeypatch.setattr(deliver, "_now", clock)
    cfg = {"routing": {"cancel_sec": 0.01}}
    monkeypatch.setattr(deliver, "_cfg", cfg)
    return plat


def test_sanitize():
    assert deliver.sanitize("a\x1b[31m b\n\nc  d") == "a [31m b c d"
    assert deliver.sanitize("x\u2028y\u202ez\u200bw") == "x y z w"
    assert deliver.sanitize("e\u0301") == "\u00e9"
    assert len(deliver.sanitize("a" * 5000)) == 2000
    assert deliver.sanitize("\n\n") == ""


def test_desktop_success(env):
    seq = iter([False])  # another app was in front at the start
    env._front = lambda: next(seq, True)
    text = "x" * 70
    assert deliver.send(mk(), text) is None
    names = env.names()
    assert env.calls[names.index("open_url")][1] == ("claude://claude.ai/epitaxy/local_abc123",)
    typed = [c[1][0] for c in env.calls if c[0] == "type_text"]
    assert typed == ["x" * 32, "x" * 32, "x" * 6]
    assert names[-2:] == ["press_enter", "restore_front"]
    assert env.calls[-1][1] == ("front-handle",)


def test_desktop_claude_never_front(env):
    env._front = False
    out = deliver.send(mk(), "hello")
    assert out == "Claude didn't come to the front."
    assert "type_text" not in env.names() and "press_enter" not in env.names()


def test_desktop_focus_moves_no_enter(env):
    n = {"i": 0}

    def front():
        n["i"] += 1
        return n["i"] < 5  # was_claude, wait, then moves away mid-typing

    env._front = front
    out = deliver.send(mk(), "y" * 100)
    assert out == "Focus moved, not sent."
    assert "press_enter" not in env.names()


def test_host_id_with_newline_is_rejected(env, monkeypatch):
    monkeypatch.setattr(deliver, "_tmux", lambda args: None)
    out = deliver.send(mk(host_id="local_abc\n"), "hi")
    assert "tmux" in out and "open_url" not in env.names()


def test_cancel_window_aborts_before_any_side_effect(env, monkeypatch):
    monkeypatch.setattr(deliver, "_cfg", {"routing": {"cancel_sec": 2.0}})
    res = []
    t = threading.Thread(target=lambda: res.append(deliver.send(mk(), "hi")))
    t.start()
    while not deliver._window_open:
        pass
    deliver.cancel()
    t.join(3)
    assert res == [deliver.CANCELLED]
    assert env.calls == []


def test_stale_cancel_does_not_abort_next_send(env):
    deliver.cancel()  # no window open: ignored
    assert deliver.send(mk(), "hi") is None


def _fake_tmux(panes, sent):
    def fake(args):
        if args[0] == "list-panes":
            return panes() if callable(panes) else panes
        sent.append(args)
        return ""

    return fake


def test_tmux_match_and_send(monkeypatch):
    monkeypatch.setattr(deliver, "_cfg", {"routing": {"cancel_sec": 0}})
    sent = []
    monkeypatch.setattr(deliver, "_tmux", _fake_tmux("%1\t50\tbash\n%2\t999\tclaude\n", sent))
    monkeypatch.setattr(deliver, "_pid_chain", lambda pid: {pid, 999})
    assert deliver.send(mk(host_id=None), "go\nrm -rf") is None
    assert sent[0] == ["send-keys", "-t", "%2", "-l", "--", "go rm -rf"]
    assert sent[1] == ["send-keys", "-t", "%2", "Enter"]


def test_tmux_ambiguous_or_none(monkeypatch):
    monkeypatch.setattr(deliver, "_cfg", {"routing": {"cancel_sec": 0}})
    monkeypatch.setattr(deliver, "_pid_chain", lambda pid: {pid})
    monkeypatch.setattr(deliver, "_tmux", _fake_tmux("%1\t100\tclaude\n%2\t100\tclaude\n", []))
    assert "can't type into" in deliver.send(mk(host_id=None), "x")


def test_tmux_pane_changed_aborts(monkeypatch):
    monkeypatch.setattr(deliver, "_cfg", {"routing": {"cancel_sec": 0}})
    monkeypatch.setattr(deliver, "_pid_chain", lambda pid: {pid})
    calls = {"n": 0}
    sent = []

    def panes():
        calls["n"] += 1
        return "%1\t100\tclaude\n" if calls["n"] == 1 else "%1\t100\tvim\n"

    monkeypatch.setattr(deliver, "_tmux", _fake_tmux(panes, sent))
    assert deliver.send(mk(host_id=None), "x") == "That pane changed, not sent."
    assert sent == []
