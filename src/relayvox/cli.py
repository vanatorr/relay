"""argparse commands: run, stop, status, install-hooks, doctor, say, ... (DESIGN section 9)."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import sys
from typing import Any

from relayvox import __version__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relayvox", description="A voice layer for Claude Code.")
    p.add_argument("--version", action="version", version=f"relayvox {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start Relay (one instance)")
    sub.add_parser("stop", help="stop the running Relay")
    sub.add_parser("status", help="sessions, mic, voice, mute state")
    for name in ("install-hooks", "uninstall-hooks"):
        h = sub.add_parser(name, help=f"{name.split('-')[0]} the Claude Code hooks")
        h.add_argument("--dry-run", action="store_true", help="print the diff, change nothing")
    d = sub.add_parser("doctor", help="check the setup")
    d.add_argument("--keys", action="store_true", help="live key test")
    s = sub.add_parser("say", help="test the voice")
    s.add_argument("text", nargs="+")
    sub.add_parser("mics", help="list capture devices")
    sub.add_parser("voices", help="list the current voice backend's voices")
    ptt = sub.add_parser("ptt", help="push-to-talk fallback for compositor binds")
    ptt.add_argument("action", choices=["down", "up", "toggle"])
    ptt.add_argument("--dictate", action="store_true")
    m = sub.add_parser("mute", help="mute or unmute")
    m.add_argument("state", nargs="?", choices=["on", "off"])
    mo = sub.add_parser("models", help="download the voice and speech models now")
    mo.add_argument("--wake", action="store_true", help="also the wake-word models (opt-in)")
    a = sub.add_parser("alias", help="name session N")
    a.add_argument("number", type=int)
    a.add_argument("name", nargs="+")
    q = sub.add_parser("ask", help="ask Relay a question as if spoken (read-only)")
    q.add_argument("text", nargs="+")
    sub.add_parser("config", help="print the config path, writing a template if missing")
    return p


def _request(msg: dict[str, Any]) -> dict[str, Any] | None:
    from relayvox import app

    return app.request(msg)


def _not_running() -> int:
    print("Relay is not running. Start it with: relayvox run")
    return 1


def _lock_path() -> str:
    from relayvox import config

    return os.path.join(config.state_dir(), "relay.lock")


def _take_lock() -> bool:
    """One instance only: an exclusive lock file holding our pid; a dead owner's is taken over."""
    from relayvox.hook import pid_alive

    path = _lock_path()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                with open(path, encoding="utf-8") as f:
                    owner = int(f.read().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner and owner != os.getpid() and pid_alive(owner):
                return False
            with contextlib.suppress(OSError):
                os.remove(path)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _cmd_run() -> int:
    from relayvox import app, config

    try:
        cfg = config.load()
    except ValueError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        return 2
    if not _take_lock():
        print("Relay is already running.")
        return 1
    try:
        return app.main(cfg)
    finally:
        with contextlib.suppress(OSError):
            os.remove(_lock_path())


def _cmd_status() -> int:
    from relayvox import route

    reply = _request({"type": "status"})
    print("Relay is running." if reply else "Relay is not running.")
    if reply:
        for key in ("muted", "mic", "voice"):
            if key in reply:
                print(f"{key}: {reply[key]}")
    live = route.sessions()
    if not live:
        print("No sessions running.")
    for s in live:
        how = "desktop app" if s.host_id else "tmux or speak-only"
        print(f"{s.number}, {s.name}: {s.status}, via {how}")
    return 0 if reply else 1


def _cmd_ask(text: str) -> int:
    from relayvox import answers, route

    reply = _request({"type": "status", "ask": text})
    if reply is not None and isinstance(reply.get("answer"), str):
        print(reply["answer"])
        return 0
    live = route.sessions()
    cmd = route.parse(text)
    out = answers.fast_path(cmd, live, route.STORE)
    if out is None:
        print("Relay is not running, so only status-style questions can be answered.")
        return 1
    print(out)
    return 0


def _cmd_alias(number: int, name: str) -> int:
    from relayvox import route

    for s in route.sessions():
        if s.number == number:
            route.set_alias(s.id, name)
            print(f"Session {number} ({s.name}) is now called {name}.")
            return 0
    print(f"No session number {number}.")
    return 1


def _cmd_mics() -> int:
    import miniaudio

    names = [d["name"] for d in miniaudio.Devices().get_captures()]
    if not names:
        print("No capture devices found.")
        return 1
    for n in names:
        print(n)
    return 0


def _cmd_optional(module: str, func: str, *args: Any) -> int:
    """Call modules owned by other parts of Relay by name; say so if they lack the function."""
    fn = getattr(importlib.import_module(f"relayvox.{module}"), func, None)
    if fn is None:
        print(f"relayvox {module}: not available in this build.")
        return 1
    out = fn(*args)
    return out if isinstance(out, int) else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `relayvox` and `relayvox-bg` (section 9)."""
    args = _parser().parse_args(argv)
    cmd = args.cmd
    if cmd == "run":
        return _cmd_run()
    if cmd == "stop":
        return 0 if _request({"type": "stop"}) else _not_running()
    if cmd == "status":
        return _cmd_status()
    if cmd in ("install-hooks", "uninstall-hooks"):
        from relayvox import install

        fn = install.install_hooks if cmd == "install-hooks" else install.uninstall_hooks
        try:
            print(fn(dry_run=args.dry_run))
        except install.InstallError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    if cmd == "doctor":
        return _cmd_optional("doctor", "run", args.keys)
    if cmd == "say":
        return 0 if _request({"type": "say", "text": " ".join(args.text)}) else _not_running()
    if cmd == "mics":
        return _cmd_mics()
    if cmd == "voices":
        return _cmd_optional("tts", "voices")
    if cmd == "ptt":
        binding = "dictate" if args.dictate else "talk"
        ok = _request({"type": "ptt", "action": args.action, "binding": binding})
        return 0 if ok else _not_running()
    if cmd == "mute":
        on = {"on": True, "off": False, None: None}[args.state]
        return 0 if _request({"type": "mute", "on": on}) else _not_running()
    if cmd == "models":
        return _cmd_optional("tts", "download_models", args.wake)
    if cmd == "alias":
        return _cmd_alias(args.number, " ".join(args.name))
    if cmd == "ask":
        return _cmd_ask(" ".join(args.text))
    if cmd == "config":
        from relayvox import config

        print(config.write_template())
        return 0
    return 2
