"""Hook payload -> (priority, session, text); cleaning; summary length (DESIGN section 6.1)."""

from __future__ import annotations

import re
import threading
from typing import Any

from relayvox import route

_SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LIMITS = {"max_sentences": 2, "max_chars": 240}
_spoken: dict[str, None] = {}  # sessions whose wrap_up recap was already spoken this turn
_lock = threading.Lock()

_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{10,})"
    r"|(?<![A-Za-z0-9])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9])"
)


def configure(max_sentences: int = 2, max_chars: int = 240) -> None:
    """Set speech.max_sentences / speech.max_chars used by parse()."""
    _LIMITS["max_sentences"] = max_sentences
    _LIMITS["max_chars"] = max_chars


def redact(text: str) -> str:
    """Replace anything shaped like an API key with "a key" (shared with brain and logs)."""
    return _KEY.sub("a key", text)


def clean(text: str) -> str:
    """Drop code blocks, say links as "a link", paths as "a file", redact keys (section 6.1)."""
    t = text if isinstance(text, str) else ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"```.*", " ", t, flags=re.S)  # unterminated block
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[A-Za-z]:\\[^\s,;]+", "a file", t)
    t = re.sub(r"(?<!\w)(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.\w+", "a file", t)
    t = redact(t)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"(^|[\s:])\d{1,2}[.)]\s+", r" ", t)  # list markers; "3.11" has no space
    t = re.sub(r"[*_#>|~]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _sentences(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut or text[:limit]


def summarize(recap: str, ask: str, max_sentences: int, max_chars: int) -> str:
    """Cut to speech.max_sentences / speech.max_chars, ask's first sentence kept whole (6.1)."""
    recap_s = _sentences(clean(recap))
    ask_s = _sentences(clean(ask))
    if ask_s:
        first_ask = ask_s[0]
        keep = recap_s[: max(0, max_sentences - 1)]
        head = " ".join(keep)
        budget = max_chars - len(first_ask) - 1
        if head and budget >= 20:
            return f"{_cut(head, budget)} {first_ask}"
        return first_ask
    return _cut(" ".join(recap_s[:max_sentences]), max_chars)


def _str(payload: dict[str, Any], key: str, cap: int) -> str:
    val = payload.get(key)
    return val[:cap] if isinstance(val, str) else ""


def parse(
    payload: dict,  # type: ignore[type-arg]
    max_sentences: int | None = None,
    max_chars: int | None = None,
) -> tuple[str, str, str] | None:
    """Return (priority, session_id, text), priority "needs_you" | "status", or None (6.1).

    A wrap_up recap is spoken at once (returned here); the Stop of that turn is then silent.
    """
    if not isinstance(payload, dict):
        return None
    ms = max_sentences or _LIMITS["max_sentences"]
    mc = max_chars or _LIMITS["max_chars"]
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not _SID.match(sid) or route.is_ignored_session(sid):
        return None
    if payload.get("agent_id"):
        return None
    kind = _str(payload, "hook_event_name", 40) or _str(payload, "event", 40)

    if kind == "PostToolUse":
        if not _str(payload, "tool_name", 200).endswith("wrap_up"):
            return None
        tin = payload.get("tool_input")
        tin = tin if isinstance(tin, dict) else {}
        recap = _str(payload, "recap", 2000) or _str(tin, "recap", 2000)
        ask = _str(payload, "ask", 500) or _str(tin, "ask", 500)
        text = summarize(recap, ask, ms, mc)
        if not text:
            return None
        if recap:
            route.remember(sid, clean(recap), "recap")
        if ask:
            route.remember(sid, clean(ask), "ask")
        with _lock:
            _spoken[sid] = None
            while len(_spoken) > 256:
                _spoken.pop(next(iter(_spoken)))
        return ("needs_you" if ask else "status", sid, text)

    if kind == "Stop":
        route.note_stop(sid)  # busy/idle fallback when the session file has no status
        if payload.get("stop_hook_active"):
            return None
        with _lock:
            already = _spoken.pop(sid, 0) is None
        if already:
            return None
        last = _str(payload, "last_assistant_message", 4000)
        text = summarize(last, "", ms, mc)
        if not text:
            return None
        route.remember(sid, clean(last), "message")
        return ("status", sid, text)

    if kind == "Notification":
        ntype = _str(payload, "notification_type", 60)
        if ntype == "permission_prompt":
            return ("needs_you", sid, "Needs your approval.")
        if ntype in ("agent_needs_input", "elicitation_dialog"):
            q = _sentences(clean(_str(payload, "message", 1000)))
            first = q[0] if q else ""
            if first:
                route.remember(sid, first, "ask")
            return ("needs_you", sid, _cut(f"Has a question. {first}".strip(), mc))
    return None
