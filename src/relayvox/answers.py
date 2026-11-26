"""Fast-path answers, no model: status, where's X, recap, repeat; EN + FR (DESIGN section 8.7)."""

from __future__ import annotations

import re

from relayvox.route import Command, RecapStore, Session

MAX_CHARS = 240
MAX_STATUS = 6

_T: dict[str, dict[str, str]] = {
    "en": {
        "none": "No sessions running.",
        "needs": "needs you",
        "busy": "busy",
        "idle": "idle",
        "more": "and {n} more idle.",
        "nothing": "{name} hasn't said anything yet.",
        "nothing_before": "Nothing earlier from {name}.",
        "which": "Which one: {list}?",
    },
    "fr": {
        "none": "Aucune session en cours.",
        "needs": "a besoin de vous",
        "busy": "occupée",
        "idle": "au repos",
        "more": "et {n} autres au repos.",
        "nothing": "{name} n'a encore rien dit.",
        "nothing_before": "Rien de plus ancien pour {name}.",
        "which": "Laquelle : {list} ?",
    },
}

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "first": 1, "second": 2, "third": 3, "fourth": 4,
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "premier": 1, "première": 1, "deuxième": 2, "troisième": 3,
}  # fmt: skip

_SENT = re.compile(r"(?<=[.!?…])\s+")


def _tr(lang: str) -> dict[str, str]:
    return _T["fr" if lang.lower().startswith("fr") else "en"]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(" ".join(text.split())) if s.strip()]


def _cut(text: str, limit: int) -> str:
    """Cut at a sentence end if possible, else at a word, never past limit."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    out = ""
    for s in _sentences(text):
        if out and len(out) + 1 + len(s) > limit:
            break
        out = f"{out} {s}".strip()
    if out and len(out) <= limit:
        return out
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "."


def _state(session: Session, store: RecapStore) -> str:
    """needs | busy | idle: an ask newer than the recap means the session needs you."""
    ask = store.ask.get(session.id)
    if ask is not None:
        rec = store.recap.get(session.id)
        if rec is None or ask.at >= rec.at:
            return "needs"
    return "busy" if session.status == "busy" else "idle"


def _latest_text(session: Session, store: RecapStore) -> str:
    """The ask when the session needs you, else the latest recap, else the ask."""
    rec, ask = store.recap.get(session.id), store.ask.get(session.id)
    if _state(session, store) == "needs" and ask is not None:
        return ask.text
    if rec is not None:
        return rec.text
    return ask.text if ask is not None else ""


def status(live: list[Session], store: RecapStore, lang: str = "en") -> str:
    """One line per session, needs-you first, max 6 (section 8.7)."""
    t = _tr(lang)
    if not live:
        return t["none"]
    rank = {"needs": 0, "busy": 1, "idle": 2}
    rows = sorted(live, key=lambda s: (rank[_state(s, store)], s.number))
    lines = []
    for s in rows[:MAX_STATUS]:
        line = f"{s.number}, {s.name}: {t[_state(s, store)]}."
        first = _sentences(_latest_text(s, store))[:1]
        if first:
            line += " " + _cut(first[0], MAX_CHARS)
        lines.append(line)
    if len(rows) > MAX_STATUS:
        lines.append(t["more"].format(n=len(rows) - MAX_STATUS))
    return " ".join(lines)


def recap(session: Session, store: RecapStore, back: int = 0, lang: str = "en") -> str:
    """Latest recap plus ask, up to 2x max_chars; back steps to older recaps (section 8.7).

    store.older[id] is read newest first: back=1 is the most recent previous recap.
    """
    t = _tr(lang)
    head = f"{session.number}, {session.name}."
    if back > 0:
        older = store.older.get(session.id, [])
        if back > len(older):
            return t["nothing_before"].format(name=session.name)
        return f"{head} {_cut(older[back - 1].text, MAX_CHARS * 2)}"
    rec, ask = store.recap.get(session.id), store.ask.get(session.id)
    text = " ".join(e.text for e in (rec, ask) if e is not None)
    if not text.strip():
        return t["nothing"].format(name=session.name)
    return f"{head} {_cut(text, MAX_CHARS * 2)}"


def where(session: Session, store: RecapStore, lang: str = "en") -> str:
    """Number, name, state and first 2 sentences of the latest recap/ask (section 8B.1)."""
    t = _tr(lang)
    out = f"{session.number}, {session.name}: {t[_state(session, store)]}."
    two = " ".join(_sentences(_latest_text(session, store))[:2])
    return f"{out} {_cut(two, MAX_CHARS)}" if two else out


def _pick(target: str, live: list[Session]) -> Session | None:
    """Simple local resolution for a fast-path target; None if not exactly one match."""
    words = re.findall(r"\w+", target.lower())
    if not words:
        return live[0] if len(live) == 1 else None
    for w in words:
        num = int(w) if w.isdigit() else _NUM_WORDS.get(w)
        hit = [s for s in live if s.number == num] if num else []
        if len(hit) == 1:
            return hit[0]
    hit = [s for s in live if any(w in s.name.lower() for w in words if len(w) >= 3)]
    return hit[0] if len(hit) == 1 else None


def fast_path(cmd: Command, live: list[Session], store: RecapStore, lang: str = "en") -> str | None:
    """Answer from memory, or None if this is not a fast-path question (section 8B.1).

    `repeat` is not answered here: only speech knows the last spoken line, so it returns None.
    """
    t = _tr(lang)
    if cmd.kind == "status":
        return status(live, store, lang)
    if cmd.kind in ("recap", "where"):
        s = _pick(cmd.target, live)
        if s is None:
            if not live:
                return t["none"]
            names = ", ".join(f"{x.number}, {x.name}" for x in live[:3])
            return t["which"].format(list=names)
        if cmd.kind == "where":
            return where(s, store, lang)
        return recap(s, store, cmd.back, lang)
    return None
