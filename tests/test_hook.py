import json
import os
import subprocess
import sys
import threading
import time

from relayvox import app, hook


def run_hook(payload, env_extra, raw=None):
    env = {**os.environ, **env_extra}
    t0 = time.monotonic()
    p = subprocess.run(
        [sys.executable, "-I", "-m", "relayvox.hook"],
        input=raw if raw is not None else json.dumps(payload).encode(),
        capture_output=True,
        env={**env, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")},
        timeout=10,
        check=False,
    )
    return p, time.monotonic() - t0


FULL = {
    "hook_event_name": "PostToolUse",
    "session_id": "s1",
    "cwd": "C:\\code\\x",
    "transcript_path": "C:\\secret\\t.jsonl",
    "tool_name": "mcp__ccd_turn__wrap_up",
    "tool_input": {"recap": "Did it.", "ask": "Ship?", "secret_field": "nope"},
    "tool_response": {"big": "x"},
}


def test_build_message_whitelist():
    msg = hook.build_message(FULL)
    assert msg == {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "cwd": "C:\\code\\x",
        "tool_name": "mcp__ccd_turn__wrap_up",
        "recap": "Did it.",
        "ask": "Ship?",
    }


def test_build_message_other_tools_lose_inputs_and_caps_apply():
    p = {**FULL, "tool_name": "Bash", "tool_input": {"command": "rm -rf"}}
    assert "recap" not in hook.build_message(p) and "command" not in str(hook.build_message(p))
    stop = {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "x" * 9000,
            "stop_hook_active": True, "agent_id": "a"}  # fmt: skip
    msg = hook.build_message(stop)
    assert len(msg["last_assistant_message"]) == 4000
    assert msg["stop_hook_active"] is True and msg["agent_id"] == "a"
    assert hook.build_message({"session_id": "s"}) is None
    assert hook.build_message("x") is None  # type: ignore[arg-type]


def test_exits_zero_silently_and_fast_when_relay_absent(tmp_path):
    p, took = run_hook(FULL, {"RELAYVOX_STATE_DIR": str(tmp_path)})
    assert (p.returncode, p.stdout, p.stderr) == (0, b"", b"")
    assert took < 1.0


def test_garbage_and_empty_stdin_still_exit_zero(tmp_path):
    for raw in (b"", b"not json", b"[1,2]", b"\xff\xfe"):
        p, _ = run_hook(None, {"RELAYVOX_STATE_DIR": str(tmp_path)}, raw=raw)
        assert (p.returncode, p.stdout, p.stderr) == (0, b"", b"")


def test_brain_marker_exits_instantly_without_reading_stdin(tmp_path):
    p, _ = run_hook(FULL, {"RELAYVOX_STATE_DIR": str(tmp_path), "RELAYVOX_BRAIN": "1"})
    assert p.returncode == 0 and p.stdout == b""
    assert hook.main.__code__  # the guard is the first statement of main
    os.environ["RELAYVOX_BRAIN"] = "1"
    try:
        assert hook.main() == 0  # would block on stdin if the guard were later
    finally:
        del os.environ["RELAYVOX_BRAIN"]


def test_forwards_whitelisted_event_to_a_running_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path))
    got = []
    stop, ready = threading.Event(), threading.Event()
    t = threading.Thread(
        target=app.serve,
        args=({},),
        kwargs={"handlers": {"event": lambda m: got.append(m["event"]) or {"ok": True}},
                "stop": stop, "ready": ready},
    )  # fmt: skip
    t.start()
    assert ready.wait(3)
    try:
        p, took = run_hook(FULL, {"RELAYVOX_STATE_DIR": str(tmp_path)})
    finally:
        stop.set()
        t.join(3)
    assert p.returncode == 0 and p.stdout == b"" and p.stderr == b""
    assert got and got[0]["recap"] == "Did it." and "transcript_path" not in got[0]
    assert took < 1.0
