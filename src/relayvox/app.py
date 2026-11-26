"""The running process: IPC server, threads, glue. Only caller of deliver.send (DESIGN 1, 10.2)."""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from relayvox import (
    answers,
    brain,
    config,
    deliver,
    events,
    hotkeys,
    listen,
    platform,
    route,
    speech,
    wake,
)
from relayvox.hook import read_endpoint

log = logging.getLogger("relayvox.app")

IPC_TYPES = ("event", "say", "status", "ptt", "mute", "stop")  # none of them delivers text
MAX_LINE = 64 * 1024
MAX_CONNS = 8
LINE_TIMEOUT = 1.0

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]


def _read_line(conn: socket.socket) -> bytes | None:
    """One line of at most 64 KiB within 1 s, else None (caller closes silently)."""
    deadline = time.monotonic() + LINE_TIMEOUT
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        conn.settimeout(left)
        try:
            chunk = conn.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > MAX_LINE + 1:
            return None
    line = buf.split(b"\n", 1)[0]
    return line if len(line) <= MAX_LINE else None


def _handle(
    conn: socket.socket,
    token: str,
    handlers: dict[str, Handler],
    stop: threading.Event,
) -> None:
    with conn:
        line = _read_line(conn)
        if line is None:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        given = msg.get("token")
        if not isinstance(given, str) or not hmac.compare_digest(
            given.encode("utf-8"), token.encode("utf-8")
        ):
            return
        mtype = msg.get("type")
        if mtype not in IPC_TYPES:
            return
        reply: dict[str, Any] | None = {"ok": True}
        if mtype == "stop":
            stop.set()
        fn = handlers.get(mtype)
        if fn is not None:
            try:
                reply = fn(msg) or reply
            except Exception:  # noqa: BLE001  a handler bug must not kill the server
                reply = {"ok": False}
        try:
            conn.settimeout(LINE_TIMEOUT)
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            return


def serve(
    cfg: Any,
    handlers: dict[str, Handler] | None = None,
    stop: threading.Event | None = None,
    ready: threading.Event | None = None,
) -> None:
    """Bind 127.0.0.1:0, write endpoint.json (0600, per-run token), handle IPC (section 10.2).

    handlers maps an IPC type ("event", "say", "status", "ptt", "mute", "stop") to a function
    taking the message dict and returning the reply dict. A "stop" message always sets `stop`.
    Blocks until `stop` is set; deletes endpoint.json on the way out.
    """
    handlers = handlers or {}
    stop = stop or threading.Event()
    token = secrets.token_hex(32)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.2)
    path = os.path.join(config.state_dir(), "endpoint.json")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": srv.getsockname()[1], "token": token, "pid": os.getpid()}, f)
    os.replace(tmp, path)
    slots = threading.BoundedSemaphore(MAX_CONNS)

    def run(conn: socket.socket) -> None:
        try:
            _handle(conn, token, handlers, stop)
        finally:
            slots.release()

    if ready is not None:
        ready.set()
    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=run, args=(conn,), daemon=True).start()
    finally:
        srv.close()
        try:
            with open(path, encoding="utf-8") as f:
                mine = json.load(f).get("token") == token
            if mine:
                os.remove(path)
        except (OSError, ValueError):
            pass


def request(msg: dict[str, Any], timeout: float = 1.0) -> dict[str, Any] | None:
    """Client side of the IPC protocol: one JSON line out, one back (section 10.2).

    Returns None if Relay is not running, the token is refused, or anything else goes wrong.
    """
    ep = read_endpoint()
    if ep is None:
        return None
    line = json.dumps({**msg, "token": ep["token"]}) + "\n"
    try:
        with socket.create_connection(("127.0.0.1", ep["port"]), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(line.encode("utf-8"))
            buf = b""
            while b"\n" not in buf and len(buf) <= MAX_LINE:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0])
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


# --------------------------------------------------------------------------- glue

PROPOSAL_SEC = 15.0
_YES = {"yes", "send it", "do it"}
_PUNCT = re.compile(r"[^\w\s']+")


@dataclass
class _Proposal:
    session_id: str
    message: str
    expires_at: float
    cutoff: float | None


@dataclass
class _State:
    cfg: Any = None
    rs: route.RouteState = field(default_factory=route.RouteState)
    ask: route.Ask | None = None
    ask_cutoff: float | None = None
    proposal: _Proposal | None = None
    cutoff: dict[str, float] = field(default_factory=dict)  # binding -> when its recording began
    recording: str | None = None  # binding whose mic is open
    skip_quiet: bool = False
    brain_on: bool = False


_S = _State()
_lock = threading.Lock()
_work: queue.Queue[tuple[Any, str] | None] = queue.Queue()


def _clock() -> float:
    return time.monotonic()


def _say(text: str, lang: str = "en") -> None:
    speech.answer(text, lang)


def _is_yes(text: str) -> bool:
    return " ".join(_PUNCT.sub(" ", text.lower()).split()) in _YES


def _hands_free(binding: str) -> bool:
    if binding == "wake":
        return True
    m = hotkeys._machines.get(binding)
    return bool(m is not None and m.state == hotkeys.LATCHED)


def _deliver(session: route.Session, body: str, cutoff: float | None) -> None:
    """Send, then drop that session's stale lines and say the target when it changed."""
    reason = deliver.send(session, body)
    if reason is not None:
        if reason != deliver.CANCELLED:
            speech.tone("error")
            _say(reason)
        return
    for sid in {session.id, session.host_id} - {None}:
        route.note_delivery(str(sid), cutoff)
    st = _S.rs
    st.needs_you.discard(session.id)
    changed = st.last_sent != session.id
    st.last_sent = session.id
    if changed:
        _say(f"{route._short(session.name)} got it.")


def _ask_brain(question: str, lang: str, live: list[route.Session], cutoff: float | None) -> None:
    if not _S.brain_on:
        _say("The brain is off. Turn it on in the config.")
        return
    ctx = brain.build_context(live, route.STORE, None)
    prop = brain.ask(question, lang, ctx, lambda line: _say(line, lang))
    if prop is None:
        return
    target = next((s for s in live if s.number == prop.session_number), None)
    if target is None:
        return
    with _lock:
        _S.proposal = _Proposal(target.id, prop.message, _clock() + PROPOSAL_SEC, cutoff)
    _say(f"Send to {target.number}, {route._short(target.name)}: '{prop.message}'? Say yes.")


def _dictate(text: str) -> None:
    clean = deliver.sanitize(text)
    if not clean:
        return
    if not platform.type_text(clean):
        if platform.copy_text(clean):
            _say("Copied, paste it.")
        else:
            speech.tone("error")


def on_utterance(audio: Any, binding: str) -> None:
    """The ONLY place deliver.send() is called, for audio the user recorded (invariant 1)."""
    try:
        _utterance(audio, binding)
    finally:
        speech.release()
        wake.resume()


def _utterance(audio: Any, binding: str) -> None:
    if audio is None:
        return
    st = _S.rs
    live = route.sessions()
    names = [s.name for s in live] + list(st.aliases.values())
    text = listen.transcribe(audio, names).strip()
    lang = listen.last_language() or "en"
    if not text:
        return
    if binding == "dictate":
        _dictate(text)
        return
    wake_word = binding == "wake"
    if _hands_free(binding):
        if listen.is_cancel(text):
            _say(deliver.CANCELLED)
            return
        text, _ = listen.strip_end_word(text)
        if not text:
            return
    cutoff = _S.cutoff.get(binding)
    now = _clock()

    with _lock:
        prop, _S.proposal = _S.proposal, None
        ask, _S.ask = _S.ask, None
        ask_cutoff = _S.ask_cutoff
    if prop is not None and now <= prop.expires_at and _is_yes(text):
        target = route.find_by_id(live, prop.session_id)
        if target is None:
            _say("That session is gone. Not sent.")
        else:
            _deliver(target, prop.message, prop.cutoff)
        return
    if ask is not None and now <= ask.expires_at:
        chosen = route.resolve_reply(ask, text, st.aliases, now)
        if chosen is None:
            _say("Not sent.")
        else:
            _deliver(chosen, ask.body, ask_cutoff)
        return

    cmd, res = route.route_text(text, live, st, now)
    kind = cmd.kind
    if kind == "quiet":
        speech.quiet()
        brain.cancel()
        return
    if kind in ("mute", "unmute"):
        speech.set_mute(kind == "mute")
        return
    if kind == "repeat":
        last = speech.last_spoken()
        if last:
            _say(last)
        return
    if kind in ("status", "recap", "where"):
        reply = answers.fast_path(cmd, live, route.STORE, lang)
        if reply is None:
            _ask_brain(cmd.text or text, lang, live, cutoff)
        else:
            _say(reply, lang)
        return
    if kind == "alias":
        if isinstance(res, route.Session) and cmd.body:
            route.set_alias(res.id, cmd.body)
            st.aliases = route.load_aliases()
            _say(f"{res.number} is now {cmd.body}.")
        else:
            _say("Which session? Not changed.")
        return
    if kind == "attach":
        if isinstance(res, route.Session):
            st.attached = res.id
            _say(f"Talking to {route._short(res.name)}.")
        else:
            _say("Which session? Not attached.")
        return
    if kind == "detach":
        st.attached = None
        _say("Detached.")
        return
    if kind == "ask":
        _ask_brain(cmd.body, lang, live, cutoff)
        return

    default_for = str(_S.cfg["brain"]["default_for"]) if _S.cfg else "wake"
    if (
        kind == "plain"
        and not cmd.target
        and _S.brain_on
        and (default_for == "both" or (default_for == "wake" and wake_word))
    ):
        _ask_brain(cmd.body or text, lang, live, cutoff)
        return
    if isinstance(res, route.Ask):
        with _lock:
            _S.ask, _S.ask_cutoff = res, cutoff
        _say(res.question, lang)
        return
    if isinstance(res, route.Session):
        _deliver(res, cmd.body, cutoff)
        return
    _say("No sessions running. Not sent." if not live else "Not sent.")


# --------------------------------------------------------------------------- threads


def _utterance_worker() -> None:
    while True:
        job = _work.get()
        if job is None:
            return
        try:
            on_utterance(*job)
        except Exception:  # noqa: BLE001  one bad utterance must not stop the next
            log.exception("utterance failed")


def _submit(audio: Any, binding: str) -> None:
    _work.put((audio, binding))


def _open_mic(binding: str, end: str) -> None:
    with _lock:
        _S.recording = binding
    speech.tone("tick")
    listen.start_recording(end)


def _close_mic() -> Any:
    with _lock:
        _S.recording = None
    return listen.stop_recording()


def on_action(binding: str, action: str) -> None:
    """hotkeys callback (worker thread): never waits on a send, so a tap can still cancel one."""
    if action == "barge":
        speech.barge()
        wake.pause()
        _S.cutoff[binding] = _clock()
    elif action == "mic_on":
        auto = _hands_free(binding) and bool(_S.cfg and _S.cfg["keys"]["latch_auto_end"])
        _open_mic(binding, "silence" if auto else "key")
    elif action == "send":
        audio = _close_mic()
        if audio is None:
            speech.release()
            wake.resume()
            return
        _submit(audio, binding)
    elif action == "discard":
        _close_mic()
        speech.release()
        wake.resume()
    elif action == "cancel_pending":
        if deliver._window_open:
            deliver.cancel()
            _S.skip_quiet = True
    elif action == "quiet":
        if _S.skip_quiet:
            _S.skip_quiet = False
            return
        speech.quiet()
        brain.cancel()


def _on_endpoint(audio: Any) -> None:
    """Hands-free end of speech, or the 120 s key limit: send what was recorded."""
    with _lock:
        binding, _S.recording = _S.recording, None
    if binding is None:
        return
    if audio is None:
        speech.release()
        wake.resume()
        return
    _submit(audio, binding)


def _on_wake() -> None:
    with _lock:
        if _S.recording is not None:
            return
    speech.barge()
    wake.pause()
    _S.cutoff["wake"] = _clock()
    end = str(_S.cfg["listen"]["end"]) if _S.cfg else "silence"
    _open_mic("wake", end)


# --------------------------------------------------------------------------- IPC handlers


def _on_event(msg: dict[str, Any]) -> dict[str, Any] | None:
    route.ignore_brain(brain.pid(), brain.session_id())  # both change when the brain restarts
    payload = msg.get("event")
    if not isinstance(payload, dict):
        return None
    parsed = events.parse(payload)
    if parsed is None:
        return None
    priority, sid, text = parsed
    s = route.find_by_id(route.sessions(), sid)
    sid = s.id if s is not None else sid
    speech.say(text, priority, session=s.name if s else None, session_id=sid)
    st = _S.rs
    st.last_spoke, st.last_spoke_at = sid, _clock()
    if priority == "needs_you":
        st.needs_you.add(sid)
    return None


def _on_say(msg: dict[str, Any]) -> dict[str, Any] | None:
    text = msg.get("text")
    if isinstance(text, str) and text.strip():
        _say(text.strip()[:2000])
    return None


def _on_status(msg: dict[str, Any]) -> dict[str, Any]:
    cfg = _S.cfg or {}
    tts = cfg.get("tts", {})
    reply: dict[str, Any] = {
        "ok": True,
        "muted": speech.is_muted(),
        "mic": cfg.get("audio", {}).get("mic") or "default",
        "voice": f"{tts.get('engine', '')} {tts.get('voice', '')}".strip(),
    }
    question = msg.get("ask")
    if isinstance(question, str):
        cmd = route.parse(question[:500])  # read-only: never touches the Ask or proposal slots
        if cmd.kind == "repeat":
            reply["answer"] = speech.last_spoken()
        else:
            reply["answer"] = (
                answers.fast_path(cmd, route.sessions(), route.STORE, "en")
                or "Only status, recap and where questions are answered here."
            )
    return reply


def _on_ptt(msg: dict[str, Any]) -> dict[str, Any] | None:
    action, binding = msg.get("action"), msg.get("binding", "talk")
    if action in ("down", "up", "toggle") and binding in ("talk", "dictate", "mouse"):
        hotkeys.inject(str(binding), str(action))
        return None
    return {"ok": False}


def _on_mute(msg: dict[str, Any]) -> dict[str, Any]:
    on = msg.get("on")
    speech.set_mute(not speech.is_muted() if on is None else bool(on))
    return {"ok": True, "muted": speech.is_muted()}


HANDLERS: dict[str, Handler] = {
    "event": _on_event,
    "say": _on_say,
    "status": _on_status,
    "ptt": _on_ptt,
    "mute": _on_mute,
}


def setup(cfg: Any) -> None:
    """Configure every module from one config and wire the cross-module callbacks."""
    _S.cfg = cfg
    listen.configure(cfg)
    speech.configure(cfg)
    deliver.configure(cfg)
    events.configure(int(cfg["speech"]["max_sentences"]), int(cfg["speech"]["max_chars"]))
    _S.rs = route.RouteState(aliases=route.load_aliases(), store=route.STORE)
    route.on_delivery(speech.drop_from)
    listen.on_endpoint(_on_endpoint)


def main(cfg: Any) -> int:
    """Start Relay: preload models, key listener, wake word, brain; block until stopped."""
    setup(cfg)
    stop = threading.Event()
    threading.Thread(target=_utterance_worker, name="utterance", daemon=True).start()
    listen.preload()
    if not hotkeys.start(cfg, on_action):
        log.warning("no key listener; use `relayvox ptt` or the wake word")
    if cfg["wake"]["enabled"] and not wake.start(_on_wake, cfg):
        log.warning("wake word not started (models missing?)")
    if cfg["brain"]["enabled"]:
        _S.brain_on = brain.start(cfg)
        route.ignore_brain(brain.pid(), brain.session_id())
    print("Relay is running. Ctrl+C to stop.", flush=True)
    try:
        serve(cfg, HANDLERS, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        hotkeys.stop()
        wake.stop()
        brain.cancel()
        _work.put(None)
    return 0
