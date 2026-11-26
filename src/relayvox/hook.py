"""Claude Code hook entry. STDLIB ONLY. Prints nothing, always exits 0 (DESIGN section 10.3)."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
from typing import Any

MAX_STDIN = 1 << 20
_CAPS = {"last_assistant_message": 4000, "message": 1000, "recap": 2000, "ask": 500}


def state_dir() -> str:
    """Per-user state folder (config.state_dir() uses this). RELAYVOX_STATE_DIR overrides."""
    env = os.environ.get("RELAYVOX_STATE_DIR")
    if env:
        return env
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "relayvox")


def pid_alive(pid: int) -> bool:
    """True if the process exists. Never os.kill on Windows (signal 0 would terminate it)."""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    import ctypes

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def read_endpoint() -> dict[str, Any] | None:
    """endpoint.json as {"port","token","pid"}, or None if missing, malformed or stale (10.2)."""
    try:
        with open(os.path.join(state_dir(), "endpoint.json"), encoding="utf-8") as f:
            ep = json.load(f)
    except (OSError, ValueError):
        return None
    if not (
        isinstance(ep, dict)
        and isinstance(ep.get("port"), int)
        and isinstance(ep.get("token"), str)
        and isinstance(ep.get("pid"), int)
    ):
        return None
    return ep if pid_alive(ep["pid"]) else None


def build_message(payload: dict) -> dict | None:  # type: ignore[type-arg]
    """Whitelist the payload fields that may leave the hook (section 10.3 step 2)."""
    if not isinstance(payload, dict):
        return None
    msg: dict[str, Any] = {}
    for key in (
        "hook_event_name",
        "session_id",
        "cwd",
        "agent_id",
        "last_assistant_message",
        "notification_type",
        "message",
        "tool_name",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val:
            msg[key] = val[: _CAPS.get(key, 500)]
    if payload.get("stop_hook_active") is True:
        msg["stop_hook_active"] = True
    if str(msg.get("tool_name", "")).endswith("wrap_up"):
        tin = payload.get("tool_input")
        if isinstance(tin, dict):
            for key in ("recap", "ask"):
                val = tin.get(key)
                if isinstance(val, str) and val:
                    msg[key] = val[: _CAPS[key]]
    return msg if msg.get("hook_event_name") and msg.get("session_id") else None


def main() -> int:
    """Read stdin, forward to the running Relay over loopback, swallow every error (10.3)."""
    if os.environ.get("RELAYVOX_BRAIN") == "1":
        return 0
    raw = sys.stdin.buffer.read(MAX_STDIN)
    msg = build_message(json.loads(raw))
    if msg is None:
        return 0
    ep = read_endpoint()
    if ep is None:
        return 0
    line = json.dumps({"token": ep["token"], "type": "event", "event": msg}) + "\n"
    with socket.create_connection(("127.0.0.1", ep["port"]), timeout=0.3) as s:
        s.settimeout(0.3)
        s.sendall(line.encode("utf-8"))
        s.recv(4096)
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BaseException):  # the hook must never affect Claude
        main()
    sys.exit(0)
