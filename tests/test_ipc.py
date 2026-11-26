import contextlib
import json
import socket
import threading

import pytest

from relayvox import app, hook


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path))
    seen = []
    stop, ready = threading.Event(), threading.Event()
    handlers = {
        "event": lambda m: seen.append(m) or {"ok": True, "got": m["event"]["session_id"]},
        "status": lambda m: {"ok": True, "muted": False},
    }
    t = threading.Thread(
        target=app.serve, args=({},), kwargs={"handlers": handlers, "stop": stop, "ready": ready}
    )
    t.start()
    assert ready.wait(3)
    yield tmp_path, seen, stop
    stop.set()
    t.join(3)


def raw(port, data: bytes, wait=1.5) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.settimeout(wait)
        s.sendall(data)
        try:
            return s.recv(4096)
        except (TimeoutError, ConnectionError):
            return b""


def test_endpoint_file_and_token(server):
    tmp, _, _ = server
    ep = json.loads((tmp / "endpoint.json").read_text())
    assert set(ep) == {"port", "token", "pid"} and len(ep["token"]) == 64
    assert hook.read_endpoint() == ep


def test_right_token_gets_a_reply(server):
    _, seen, _ = server
    reply = app.request({"type": "event", "event": {"session_id": "s1"}})
    assert reply == {"ok": True, "got": "s1"} and len(seen) == 1
    assert app.request({"type": "status"}) == {"ok": True, "muted": False}


def test_unhandled_type_still_acks_and_unknown_type_is_closed(server):
    assert app.request({"type": "mute", "on": True}) == {"ok": True}
    ep = hook.read_endpoint()
    msg = json.dumps({"token": ep["token"], "type": "send", "text": "rm -rf"}) + "\n"
    assert raw(ep["port"], msg.encode()) == b""  # there is no way to deliver text


def test_wrong_token_and_http_get_no_reply(server):
    ep = hook.read_endpoint()
    bad = json.dumps({"token": "0" * 64, "type": "status"}) + "\n"
    assert raw(ep["port"], bad.encode()) == b""
    assert raw(ep["port"], b"GET / HTTP/1.1\r\nHost: x\r\n\r\n") == b""
    assert raw(ep["port"], b"not json\n") == b""
    assert raw(ep["port"], b'{"type":"status"}\n') == b""  # no token


def test_oversized_line_is_closed(server):
    ep = hook.read_endpoint()
    big = json.dumps({"token": ep["token"], "type": "status", "pad": "x" * 70000}) + "\n"
    with contextlib.suppress(OSError):  # the server may close while we are still sending
        assert raw(ep["port"], big.encode()) == b""


def test_silent_connection_is_dropped_within_about_a_second(server):
    ep = hook.read_endpoint()
    with socket.create_connection(("127.0.0.1", ep["port"]), timeout=3) as s:
        s.settimeout(3)
        assert s.recv(10) == b""  # closed by the server, no reply


def test_stop_message_ends_serve_and_removes_endpoint(server):
    tmp, _, stop = server
    assert app.request({"type": "stop"}) == {"ok": True}
    assert stop.wait(2)
    for _ in range(30):
        if not (tmp / "endpoint.json").exists():
            break
        threading.Event().wait(0.1)
    assert not (tmp / "endpoint.json").exists()
    assert app.request({"type": "status"}) is None


def test_request_with_no_relay_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path))
    assert app.request({"type": "status"}) is None


def test_stale_endpoint_with_dead_pid_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path))
    (tmp_path / "endpoint.json").write_text(
        json.dumps({"port": 9, "token": "t", "pid": 2**22 + 12345})
    )
    assert hook.read_endpoint() is None
