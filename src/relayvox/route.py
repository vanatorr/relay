"""Sessions on disk, numbering, aliases, recap store, command parsing (DESIGN section 8).

Routing never silently guesses: it sends only when exactly one session has strong evidence
(number, recency, alias, title, project). The latest recap is weak evidence and only ever puts a
session forward as a numbered choice in an Ask.
"""

from __future__ import annotations

import difflib
import functools
import glob
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import psutil

ASK_HOLD_SEC = 60.0
MAX_SESSION_FILE = 64 * 1024
_RECAP_KEEP = 3  # latest + 2 older


@dataclass(frozen=True)
class Session:
    """A live Claude Code session (section 8.1)."""

    id: str
    host_id: str | None
    name: str
    cwd: str
    pid: int
    started: int
    status: str  # "busy" | "idle" | "unknown"
    number: int


@dataclass(frozen=True)
class Command:
    """A parsed utterance (section 8.2)."""

    kind: str  # quiet|mute|unmute|status|recap|where|repeat|alias|attach|detach|tell|plain|ask
    target: str = ""  # target phrase, "" if none
    body: str = ""  # message body for tell/plain (alias: the new alias)
    back: int = 0  # "before that" steps for recap
    text: str = ""  # the original text


@dataclass(frozen=True)
class Ask:
    """A numbered "Which one" question holding the message body (section 8.3)."""

    question: str  # spoken text
    candidates: tuple[Session, ...]
    body: str
    expires_at: float = 0.0  # monotonic seconds; body held 60 s


@dataclass
class RecapEntry:
    """One remembered text with its arrival time (section 8.1)."""

    text: str
    at: float


@dataclass
class RecapStore:
    """In-memory latest recap, ask, message, and 2 older recaps per session (section 8.1)."""

    recap: dict[str, RecapEntry] = field(default_factory=dict)
    ask: dict[str, RecapEntry] = field(default_factory=dict)
    message: dict[str, RecapEntry] = field(default_factory=dict)
    older: dict[str, list[RecapEntry]] = field(default_factory=dict)


@dataclass
class RouteState:
    """What resolve() needs besides the sessions (sections 8.3, 8.5)."""

    attached: str | None = None  # session id
    last_spoke: str | None = None  # session id (only for "that one", never a send default)
    last_spoke_at: float = 0.0
    last_sent: str | None = None
    needs_you: set[str] = field(default_factory=set)  # session ids with an unanswered ask
    aliases: dict[str, str] = field(default_factory=dict)  # session id -> alias
    store: RecapStore = field(default_factory=RecapStore)
    on_screen: str | None = None  # session id currently shown in the Claude app, if known


# --------------------------------------------------------------------------- sessions

STORE = RecapStore()
_lock = threading.RLock()
_numbers: dict[str, int] = {}
_cache: tuple[float, list[Session]] | None = None
_hook_idle: set[str] = set()
_ignored_pids: set[int] = set()
_ignored_sids: set[str] = set()
_clock = time.monotonic


def reset() -> None:
    """Forget every in-memory table (tests)."""
    global _cache
    with _lock:
        _numbers.clear()
        _hook_idle.clear()
        _ignored_pids.clear()
        _ignored_sids.clear()
        _delivery_listeners.clear()
        _cache = None
        for d in (STORE.recap, STORE.ask, STORE.message, STORE.older):
            d.clear()


def claude_dir() -> str:
    """CLAUDE_CONFIG_DIR or ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def ignore_brain(pid: int | None = None, session_id: str | None = None) -> None:
    """Keep the brain process out of the session list and the event stream (8B.2 guard 2, 3)."""
    with _lock:
        if pid:
            _ignored_pids.add(pid)
        if session_id:
            _ignored_sids.add(session_id)


def is_ignored_session(session_id: str) -> bool:
    return session_id in _ignored_sids


def note_stop(session_id: str) -> None:
    """A Stop hook arrived: with no `status` field on disk, this session is idle."""
    _hook_idle.add(session_id)


_delivery_listeners: list[Callable[[str, float | None], None]] = []


def on_delivery(cb: Callable[[str, float | None], None]) -> None:
    """Register cb(session_id, before), called when a message is delivered. The app registers
    speech.drop_from here: that session's speech from before `before` is stale (you answered it).
    """
    _delivery_listeners.append(cb)


def note_delivery(session_id: str, before: float | None = None) -> None:
    """You sent to this session: it is busy again until its next Stop; its old speech is stale.

    before: when the recording of the delivered message started (monotonic); None = all of it.
    """
    _hook_idle.discard(session_id)
    STORE.ask.pop(session_id, None)
    for cb in list(_delivery_listeners):
        try:
            cb(session_id, before)
        except Exception:  # noqa: BLE001, S112  a listener must not break delivery
            continue


def _read_session(path: str) -> dict[str, object] | None:
    try:
        if os.path.getsize(path) > MAX_SESSION_FILE:
            return None
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _int(v: object) -> int:
    try:
        return int(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def sessions(force: bool = False) -> list[Session]:
    """Read <claude dir>/sessions/*.json, drop dead pids, assign numbers (section 8.1)."""
    global _cache
    with _lock:
        now = _clock()
        if not force and _cache is not None and now - _cache[0] < 2.0:
            return list(_cache[1])
        raw: list[tuple[int, int, dict[str, object]]] = []
        for path in glob.glob(os.path.join(claude_dir(), "sessions", "*.json")):
            d = _read_session(path)
            if d is None:
                continue
            sid, pid = d.get("sessionId"), _int(d.get("pid"))
            if not isinstance(sid, str) or not sid or pid <= 0:
                continue
            if pid in _ignored_pids or sid in _ignored_sids:
                continue
            if d.get("kind") not in (None, "interactive"):
                continue
            if not psutil.pid_exists(pid):
                continue
            raw.append((_int(d.get("startedAt")), pid, d))
        raw.sort(key=lambda r: (r[0], r[1]))
        live_ids = {str(d["sessionId"]) for _, _, d in raw}
        for sid in [s for s in _numbers if s not in live_ids]:
            _forget(sid)
        out: list[Session] = []
        for started, pid, d in raw:
            sid = str(d["sessionId"])
            if sid not in _numbers:
                used = set(_numbers.values())
                _numbers[sid] = next(n for n in range(1, len(used) + 2) if n not in used)
            raw_cwd, raw_name = d.get("cwd"), d.get("name")
            cwd = raw_cwd if isinstance(raw_cwd, str) else ""
            name = raw_name if isinstance(raw_name, str) else ""
            raw_status = d.get("status")
            if raw_status is None:
                status = "idle" if sid in _hook_idle else "unknown"
            else:
                status = "busy" if raw_status == "busy" else "idle"
            host = d.get("hostSessionId")
            out.append(
                Session(
                    id=sid,
                    host_id=host if isinstance(host, str) else None,
                    name=(name or os.path.basename(str(cwd).rstrip("/\\")) or "a session")[:80],
                    cwd=str(cwd),
                    pid=pid,
                    started=started,
                    status=status,
                    number=_numbers[sid],
                )
            )
        _cache = (now, out)
        return list(out)


def _forget(sid: str) -> None:
    _numbers.pop(sid, None)
    _hook_idle.discard(sid)
    for d in (STORE.recap, STORE.ask, STORE.message, STORE.older):
        d.pop(sid, None)


def find_by_id(live: list[Session], session_id: str) -> Session | None:
    """Match a hook session_id against the session id or the host id."""
    for s in live:
        if session_id in (s.id, s.host_id):
            return s
    return None


# --------------------------------------------------------------------------- recaps, aliases


def remember(session_id: str, text: str, kind: str) -> None:
    """Keep the latest recap/ask/message for routing hints and answers; kind (section 8.1)."""
    if not text or kind not in ("recap", "ask", "message"):
        return
    entry = RecapEntry(text[:4000], _clock())
    with _lock:
        if kind == "recap":
            prev = STORE.recap.get(session_id)
            if prev is not None:
                STORE.older.setdefault(session_id, []).insert(0, prev)
                del STORE.older[session_id][_RECAP_KEEP - 1 :]
            STORE.recap[session_id] = entry
        elif kind == "ask":
            STORE.ask[session_id] = entry
        else:
            STORE.message[session_id] = entry


def _aliases_path() -> str:
    from relayvox import config

    return os.path.join(config.state_dir(), "aliases.json")


def load_aliases() -> dict[str, str]:
    """session id -> alias, from <state dir>/aliases.json."""
    try:
        with open(_aliases_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)}


def set_alias(session_id: str, alias: str) -> None:
    """Persist an alias in <state dir>/aliases.json, 0600 (section 8.1). One owner per alias."""
    alias = " ".join(alias.lower().split())[:40]
    with _lock:
        data = load_aliases()
        data = {k: v for k, v in data.items() if v != alias}
        if alias:
            data[session_id] = alias
        else:
            data.pop(session_id, None)
        path = _aliases_path()
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- parsing

_NUMS = {
    "one": 1, "first": 1, "un": 1, "une": 1, "premier": 1, "premiere": 1,
    "two": 2, "second": 2, "deux": 2, "deuxieme": 2,
    "three": 3, "third": 3, "trois": 3, "troisieme": 3,
    "four": 4, "fourth": 4, "quatre": 4,
    "five": 5, "fifth": 5, "cinq": 5,
    "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "sept": 7,
    "eight": 8, "eighth": 8, "huit": 8,
    "nine": 9, "ninth": 9, "neuf": 9,
    "ten": 10, "tenth": 10, "dix": 10,
}  # fmt: skip
_NUMW = "|".join(sorted((k for k in _NUMS if k not in ("un", "une")), key=len, reverse=True))
_JOIN = {"to", "that", "de", "d'", "que", "qu'"}
_AGGREGATE = {"all", "everyone", "everything", "agents", "sessions", "them", "all the agents"}

_I = re.IGNORECASE
_QUIET = re.compile(r"^(?:be quiet|quiet|stop talking|shut up|tais-?toi|silence)$", _I)
_UNMUTE = re.compile(r"^unmute(?: relay)?$", _I)
_MUTE = re.compile(r"^mute(?: relay)?$", _I)
_REPEAT = re.compile(
    r"^(?:repeat(?: that)?|say that again|what did you say|what was that|come again|pardon|"
    r"i didn'?t (?:get|catch) that|(?:r[ée]p[èe]te|redis)(?: [çc]a)?)$",
    _I,
)
_STATUS = re.compile(
    r"^(?:(?:what(?:'s| is)|give me|tell me) (?:the )?)?(?:status|statut)(?: report| update)?"
    r"(?: (?:on|of|for|about) (?:all |all of )?(?:the )?"
    r"(?:agents|sessions|everyone|everything|them|things))?$"
    r"|^what(?:'s| is| are) (?:everyone|everybody|they|all the agents|the agents) doing$"
    r"|^how(?:'s| is| are) (?:everyone|everybody|the agents)(?: doing)?$"
    r"|^quel est le statut$",
    _I,
)
_STATUS_OF = re.compile(r"^(?:what(?:'s| is) the )?status (?:on|of|for|about|with) (.+)$", _I)
_ALIAS = re.compile(
    rf"^(?:call|name|rename|label|appelle)\s+(?:le |la )?(?:(?:number|session|num[ée]ro) )?"
    rf"(that one|this one|it|(?:the )?last one|\d+|{_NUMW}|un|une)\s+(.+)$",
    _I,
)
_ATTACH = re.compile(r"^(?:talk to|attach(?: to)?|parle [àa]|attache(?:-toi [àa])?)\s+(.+)$", _I)
_DETACH = re.compile(r"^(?:detach(?: from .*)?|unattach|d[ée]tache(?:-toi)?)$", _I)
_RECAP = re.compile(
    r"^what (?:did|does|has|have|do) (.+?) (?:say|said|need|want|ask|asked)"
    r"(?: again)?(?P<back> before that| earlier| previously| last time)?$"
    r"|^(?:qu'est-ce que|qu'est ce que|que) (.+?) a dit(?P<back2> avant| avant [çc]a)?$"
    r"|^qu'a dit (.+)$",
    _I,
)
_WHERE = re.compile(
    r"^where(?:'s| is| are) (.+?) at(?: now)?$"
    r"|^how(?:'s| is| are) (.+?) (?:doing|going|looking)$"
    r"|^what(?:'s| is| are) (.+?) doing$"
    r"|^how far (?:along )?(?:is|are) (.+)$"
    r"|^o[ùu] en est (.+)$|^comment va (.+)$",
    _I,
)
_TELL = re.compile(r"^(?:tell|say to|dis [àa]|dites [àa])\s+(.+)$", _I)
_TO_COLON = re.compile(r"^to\s+(.+?)\s*:\s*(.+)$", _I)
_CARD = "|".join(
    k
    for k in _NUMS
    if k
    not in (
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "premier",
        "premiere",
        "deuxieme",
        "troisieme",
        "un",
        "une",
    )
)
_ORDINALS = {k for k in _NUMS if not re.fullmatch(_CARD, k) and k not in ("un", "une")}
_NUM_FIRST = re.compile(
    rf"^(?:(?:number|session|no\.?)\s+(\d+|{_NUMW})|(\d+|{_CARD}))\s*[,:]\s*(.+)$", _I
)
_NUM_WORD = re.compile(rf"^(?:number|session)\s+(\d+|{_NUMW})\s+(.+)$", _I)
_RELAY = re.compile(r"^(?:(?:hey|ok|okay) )?relay\s*[,:]\s*(.+)$", _I)


def _clean_text(text: str) -> str:
    t = text.replace("’", "'").replace("‘", "'")
    t = " ".join(t.split())
    t = re.sub(r"^(?:please|s'il te pla[iî]t),?\s+", "", t, flags=_I)
    return t


def _split_comma(rest: str) -> tuple[str, str] | None:
    m = re.match(r"^([^,:]{1,60})\s*[,:]\s*(.+)$", rest)
    if m and len(m.group(1).split()) <= 9:
        return m.group(1).strip(), m.group(2).strip()
    return None


def parse(text: str) -> Command:
    """Parse Whisper output into a Command, EN and FR forms (section 8.2, 8B.5)."""
    full = _clean_text(text)
    t = full.rstrip(" .!?")
    cmd = _parse(t, text)
    tail = full[len(t) :]  # keep the speaker's closing punctuation in what gets delivered
    if tail and cmd.kind in ("tell", "plain", "ask"):
        if cmd.body:
            return replace(cmd, body=cmd.body + tail)
        if cmd.kind == "tell":
            return replace(cmd, target=cmd.target + tail)
    return cmd


def _parse(t: str, text: str) -> Command:
    c = functools.partial(Command, text=text)
    if not t:
        return c("plain")
    m = _RELAY.match(t)
    if m:
        inner = _parse(m.group(1), text)
        return inner if inner.kind != "plain" else c("ask", body=m.group(1))
    if _QUIET.match(t):
        return c("quiet")
    if _UNMUTE.match(t):
        return c("unmute")
    if _MUTE.match(t):
        return c("mute")
    if _REPEAT.match(t):
        return c("repeat")
    if _STATUS.match(t):
        return c("status")
    m = _STATUS_OF.match(t)
    if m:
        tgt = m.group(1).strip().lower()
        return c("status") if tgt in _AGGREGATE else c("where", target=m.group(1).strip())
    m = _ALIAS.match(t)
    if m:
        return c("alias", target=m.group(1), body=m.group(2).strip())
    if _DETACH.match(t):
        return c("detach")
    m = _ATTACH.match(t)
    if m:
        return c("attach", target=m.group(1).strip())
    m = _RECAP.match(t)
    if m:
        tgt = next(g for g in (m.group(1), m.group(3), m.group(5)) if g)
        return c(
            "recap", target=tgt.strip(), back=1 if (m.group("back") or m.group("back2")) else 0
        )
    m = _WHERE.match(t)
    if m:
        return c("where", target=next(g for g in m.groups() if g).strip())
    m = _TELL.match(t)
    if m:
        rest = m.group(1).strip()
        sp = _split_comma(rest)
        if sp:
            return c("tell", target=sp[0], body=sp[1])
        return c("tell", target=rest)
    m = _TO_COLON.match(t)
    if m:
        return c("tell", target=m.group(1).strip(), body=m.group(2).strip())
    m = _NUM_FIRST.match(t)
    if m:
        return c("tell", target=m.group(1) or m.group(2), body=m.group(3).strip())
    m = _NUM_WORD.match(t)
    if m:
        return c("tell", target=m.group(1), body=m.group(2).strip())
    return c("plain", body=t)


# --------------------------------------------------------------------------- evidence

_STOP = {
    "the", "a", "an", "one", "ones", "session", "sessions", "agent", "that", "that's", "which",
    "who", "is", "are", "was", "working", "on", "doing", "about", "to", "of", "for", "and", "my",
    "our", "number", "no", "it", "this", "those", "with", "in", "at", "please",
    "le", "la", "les", "un", "une", "qui", "sur", "de", "du", "des", "celui",
}  # fmt: skip
_LAST = re.compile(
    r"^(?:the )?(?:last one|that one|this one|the last (?:speaker|session)|last speaker|"
    r"the one (?:that |who )?(?:just )?(?:spoke|talked|said)|celui-?l[àa]|celui-?ci|le dernier)$"
)
_NEEDS = re.compile(
    r"^(?:the )?(?:one|session|agent)? ?(?:that |who |which )?(?:needs? (?:me|you|my \w+)|"
    r"is asking|asking|asked|is waiting|waiting)(?: for me)?$|^celui qui a besoin de moi$"
)
_STATE = re.compile(r"^(?:the )?(busy|idle) one$")


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9à-ÿ]+", s.lower().replace("'", ""))


@functools.lru_cache(maxsize=16384)
def _fuzzy(a: str, b: str) -> bool:
    """Same word: equal, shared prefix of 5+ letters, or difflib ratio >= 0.85."""
    if a == b:
        return True
    if len(a) >= 5 and len(b) >= 5 and os.path.commonprefix([a, b]).__len__() >= 5:
        return True
    if min(len(a), len(b)) < 4:
        return False
    sm = difflib.SequenceMatcher(None, a, b)
    return sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85 and sm.ratio() >= 0.85


def _number_of(words: list[str]) -> int | None:
    w = [x for x in words if x not in ("number", "session", "no", "numero", "the", "one", "un")]
    if not w:
        return 1 if words and words[-1] in ("one", "un") and len(words) == 1 else None
    if len(w) == 1:
        if w[0].isdigit():
            return int(w[0])
        return _NUMS.get(w[0])
    return None


@dataclass
class _Target:
    phrase: str
    num: int | None
    recency: str  # "", "last", "needs", "busy", "idle"
    content: list[str]


def _target(phrase: str) -> _Target:
    p = " ".join(phrase.lower().replace("’", "'").split()).strip(" .,!?")
    words = _words(p)
    rec = ""
    if _LAST.match(p):
        rec = "last"
    elif _NEEDS.match(p):
        rec = "needs"
    else:
        m = _STATE.match(p)
        if m:
            rec = m.group(1)
    num = None if rec else _number_of(words)
    content = [] if (rec or num is not None) else [w for w in words if w not in _STOP]
    return _Target(p, num, rec, content)


@dataclass
class _Prof:
    s: Session
    title: list[str]
    folder: list[str]
    alias: list[str]
    text: set[str]


def _profiles(live: list[Session], state: RouteState) -> list[_Prof]:
    out = []
    for s in live:
        cwd = s.cwd.rstrip("/\\")
        folder = _words(re.sub(r"[-_.]", " ", os.path.basename(cwd.replace("\\", "/"))))
        folder += _words(
            re.sub(r"[-_.]", " ", os.path.basename(os.path.dirname(cwd.replace("\\", "/"))))
        )
        txt: set[str] = set()
        for d in (state.store.recap, state.store.ask):
            e = d.get(s.id)
            if e is not None:
                txt.update(_words(e.text))
        out.append(_Prof(s, _words(s.name), folder, _words(state.aliases.get(s.id, "")), txt))
    return out


def _all_match(content: list[str], tokens: list[str]) -> bool:
    return (
        bool(content) and bool(tokens) and all(any(_fuzzy(w, t) for t in tokens) for w in content)
    )


def _is_strong(t: _Target, p: _Prof, state: RouteState) -> bool:
    if t.num is not None:
        return p.s.number == t.num
    if t.recency == "last":
        return p.s.id == state.last_spoke
    if t.recency == "needs":
        return p.s.id in state.needs_you
    if t.recency in ("busy", "idle"):
        return p.s.status == t.recency
    c = t.content
    if not c:
        return False
    if p.alias and _all_match(c, p.alias) and all(any(_fuzzy(a, w) for w in c) for a in p.alias):
        return True
    if _all_match(c, p.title):
        return True
    if len(c) > 1 and difflib.SequenceMatcher(None, " ".join(c), " ".join(p.title)).ratio() >= 0.8:
        return True
    return _all_match(c, p.folder)


def _weak(t: _Target, p: _Prof) -> int:
    """Count of phrase content words found in the latest recap or ask (never sends alone)."""
    hits = [
        w for w in t.content if w in p.text or (len(w) >= 5 and any(_fuzzy(w, x) for x in p.text))
    ]
    if len(hits) >= 2 or (len(hits) == 1 and len(hits[0]) >= 5):
        return len(hits)
    return 0


def _short(name: str) -> str:
    return " ".join(name.split()[:3])


def _ask(lead: str, cands: list[Session], body: str, now: float, numbered: bool = False) -> Ask:
    cands = sorted(cands, key=lambda c: c.number)
    items = [f"{c.number}, {_short(c.name)}" for c in cands]
    if numbered:
        q = f"{lead} " + " ".join(f"{i}." for i in items)
    elif len(items) == 1:
        q = f"{lead} {items[0]}?"
    elif len(items) == 2:
        q = f"{lead} {items[0]}, or {items[1]}?"
    else:
        q = f"{lead} {', '.join(items[:-1])}, or {items[-1]}?"
    return Ask(q, tuple(cands), body, now + ASK_HOLD_SEC)


def _order(profs: list[_Prof], t: _Target, state: RouteState) -> list[_Prof]:
    return sorted(
        profs,
        key=lambda p: (-_weak(t, p), p.s.id not in state.needs_you, p.s.number),
    )


def _decide(
    phrase: str,
    body: str,
    profs: list[_Prof],
    state: RouteState,
    now: float,
    sends: bool,
) -> Session | Ask | None:
    t = _target(phrase)
    strong = [p for p in profs if _is_strong(t, p, state)]
    if len(strong) == 1:
        return strong[0].s
    if len(strong) > 1:
        return _ask("Which one:", [p.s for p in _order(strong, t, state)[:4]], body, now)
    weak = _order([p for p in profs if _weak(t, p)], t, state)
    if weak:
        cands = weak[:3]
        if len(cands) < 2:  # pad so the question is a real choice
            rest = [p for p in _order(profs, t, state) if p not in cands]
            cands += rest[: 2 - len(cands)]
        return _ask("Which one:", [p.s for p in cands], body, now)
    if not sends:
        return None  # a read-only question nobody matches: let the caller try the brain
    return _ask("Who should I send that to?", [p.s for p in profs[:6]], body, now, numbered=True)


def _matched(t: _Target, p: _Prof) -> int:
    """Content words that individually match a title, alias or folder word."""
    toks = p.title + p.alias + p.folder
    return sum(1 for w in t.content if any(_fuzzy(w, x) for x in toks)) or 1


def _tier(phrase: str, profs: list[_Prof], state: RouteState) -> tuple[int, int]:
    t = _target(phrase)
    strong = [p for p in profs if _is_strong(t, p, state)]
    if strong:
        return 2, max(_matched(t, p) for p in strong)
    return (1, max(_weak(t, p) for p in profs)) if any(_weak(t, p) for p in profs) else (0, 0)


def split_tell(cmd: Command, live: list[Session], state: RouteState) -> Command:
    """Split an unsplit "tell <target words> <body>" into target and body (section 8.4)."""
    if cmd.kind != "tell" or cmd.body:
        return cmd
    words = cmd.target.split()
    if len(words) < 2 or not live:
        return cmd
    profs = _profiles(live, state)
    best, best_key = 1, (-1, -1, False, 0)
    for k in range(1, min(6, len(words) - 1) + 1):
        tier, score = _tier(" ".join(words[:k]), profs, state)
        key = (tier, score, words[k].lower() in _JOIN, -k)
        if key > best_key:
            best, best_key = k, key
    if best_key[0] == 0:  # nothing matched: split at the first joining word, else after word 1
        best = next(
            (k for k in range(1, min(6, len(words) - 1) + 1) if words[k].lower() in _JOIN), 1
        )
    body_words = words[best:]
    if len(body_words) > 1 and body_words[0].lower() in _JOIN:
        body_words = body_words[1:]
    return replace(cmd, target=" ".join(words[:best]), body=" ".join(body_words))


def _plain_target(cmd: Command, live: list[Session], state: RouteState) -> tuple[Command, bool]:
    """Turn "<name>, <message>" into a tell when the name has strong evidence (8.2)."""
    sp = _split_comma(cmd.body)
    if sp is None:
        return cmd, False
    if sp[0].lower() in _ORDINALS:  # "First, run the tests" is prose, not session 1
        return cmd, False
    profs = _profiles(live, state)
    t = _target(sp[0])
    if any(_is_strong(t, p, state) for p in profs):
        return replace(cmd, kind="tell", target=sp[0], body=sp[1]), True
    return cmd, False


def resolve(
    cmd: Command, live: list[Session], state: RouteState, now: float | None = None
) -> Session | Ask | None:
    """Exactly one strong match sends; otherwise Ask; None = nobody matched (section 8.3).

    Plain messages go to the attached, then on-screen, then last-sent session (never the last
    speaker), else the only live session, else an Ask. Read-only kinds (recap, where, attach,
    alias) return None when nothing matches at all.
    """
    now = _clock() if now is None else now
    if not live:
        return None
    if cmd.kind == "plain":
        cmd, _ = _plain_target(cmd, live, state)
    if cmd.kind == "plain":
        by_id = {s.id: s for s in live}
        for sid in (state.attached, state.on_screen, state.last_sent):
            if sid and sid in by_id:
                return by_id[sid]
        if len(live) == 1:
            return live[0]
        return _ask("Who should I send that to?", live[:6], cmd.body, now, numbered=True)
    if cmd.kind in ("tell", "recap", "where", "attach", "alias"):
        cmd = split_tell(cmd, live, state)
        if cmd.kind == "tell" and not cmd.body:
            return None
        sends = cmd.kind == "tell"
        if not cmd.target:
            by_id = {s.id: s for s in live}
            if cmd.kind in ("recap", "where") and state.last_spoke in by_id:
                return by_id[state.last_spoke]  # read-only: "what did he say" = last speaker
            return None
        return _decide(cmd.target, cmd.body, _profiles(live, state), state, now, sends)
    return None


def route_text(
    text: str, live: list[Session], state: RouteState, now: float | None = None
) -> tuple[Command, Session | Ask | None]:
    """parse + split + resolve in one call. Returns the final Command (target/body split).

    Deliver `cmd.body` from the RETURNED command (the "<name>," prefix is already stripped there),
    never the body from a bare parse().
    """
    cmd = split_tell(parse(text), live, state)
    if cmd.kind == "plain" and live:
        cmd, _ = _plain_target(cmd, live, state)
    return cmd, resolve(cmd, live, state, now)


_NO = {"neither", "cancel", "none", "no", "nobody", "nevermind", "annule", "non", "aucun"}


def resolve_reply(
    ask: Ask,
    text: str,
    aliases: dict[str, str] | None = None,
    now: float | None = None,
) -> Session | None:
    """One-word answer to a "Which one" question: the chosen session, or None (not sent)."""
    now = _clock() if now is None else now
    if now > ask.expires_at:
        return None
    words = _words(_clean_text(text))
    if not words or _NO & set(words) or " ".join(words) == "never mind":
        return None
    n = _number_of(words)
    if n is not None:
        for c in ask.candidates:
            if c.number == n:
                return c
        if 1 <= n <= len(ask.candidates) and words[0] in ("first", "second", "third", "premier"):
            return ask.candidates[n - 1]
        return None
    content = [w for w in words if w not in _STOP]
    hits = []
    for c in ask.candidates:
        toks = _words(c.name) + _words((aliases or {}).get(c.id, ""))
        if content and all(any(_fuzzy(w, x) for x in toks) for w in content):
            hits.append(c)
    return hits[0] if len(hits) == 1 else None
