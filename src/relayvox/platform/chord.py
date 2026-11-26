"""Pure key-chord parsing and tracking for the dedicated stop key (e.g. "ctrl+alt+x").

Platform listeners translate native key codes to the canonical names used here: the modifiers
"ctrl", "alt", "shift", "cmd", plus a letter, a digit or "f1".."f24".
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

MODIFIERS = ("ctrl", "alt", "shift", "cmd")
_ALIASES = {
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    "win": "cmd",
    "super": "cmd",
    "meta": "cmd",
    "command": "cmd",
}
_KEY = re.compile(r"^(?:[a-z0-9]|f(?:[1-9]|1\d|2[0-4]))$")


@dataclass(frozen=True)
class Chord:
    """Modifiers that must be held and the key that completes the chord."""

    mods: frozenset[str]
    key: str


def parse(spec: str) -> Chord | None:
    """ "ctrl+alt+x" -> Chord; "" -> None (off). Raises ValueError on anything unusable."""
    text = spec.strip().lower()
    if not text:
        return None
    parts = [_ALIASES.get(p.strip(), p.strip()) for p in text.split("+")]
    key = parts[-1]
    mods = frozenset(parts[:-1])
    if not _KEY.match(key) or not mods or not mods <= set(MODIFIERS) or len(mods) != len(parts) - 1:
        raise ValueError(f"unusable key chord {spec!r}; use e.g. ctrl+alt+x")
    return Chord(mods, key)


class Tracker:
    """Feed (canonical name, is_down); feed() is True once per press that completes the chord."""

    def __init__(self, chord: Chord) -> None:
        self.chord = chord
        self._held: set[str] = set()
        self._lock = threading.Lock()

    def feed(self, name: str, is_down: bool) -> bool:
        with self._lock:
            if not is_down:
                self._held.discard(name)
                return False
            fresh = name not in self._held  # auto-repeat keeps the key in the set
            self._held.add(name)
            return fresh and name == self.chord.key and self.chord.mods <= self._held
