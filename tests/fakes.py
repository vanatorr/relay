"""Fakes for tests: platform, synth, player, VAD, clock. No mic, GPU or OS hooks (DESIGN 14)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any


class FakeClock:
    """Manual monotonic clock in seconds; advance() moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def ms(self) -> int:
        return int(self.t * 1000)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakePlatform:
    """Records every call; claude_in_front is configurable (a bool or a callable)."""

    def __init__(self, claude_in_front: bool | Callable[[], bool] = True) -> None:
        self._front = claude_in_front
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.sink: Callable[[str, bool], None] | None = None
        self.type_ok = True
        self.open_ok = True

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def start_key_listener(self, keys: dict[str, str], sink: Callable[[str, bool], None]) -> bool:
        self._rec("start_key_listener", keys)
        self.sink = sink
        return True

    def on_arm(self, binding: str) -> None:
        self._rec("on_arm", binding)

    def claude_in_front(self) -> bool:
        self._rec("claude_in_front")
        return self._front() if callable(self._front) else self._front

    def remember_front(self) -> object:
        self._rec("remember_front")
        return "front-handle"

    def restore_front(self, handle: object) -> None:
        self._rec("restore_front", handle)

    def open_url(self, url: str) -> bool:
        self._rec("open_url", url)
        return self.open_ok

    def type_text(self, text: str) -> bool:
        self._rec("type_text", text)
        return self.type_ok

    def press_enter(self) -> bool:
        self._rec("press_enter")
        return True

    def copy_text(self, text: str) -> bool:
        self._rec("copy_text", text)
        return True

    def permissions(self) -> dict[str, str]:
        return {}


class FakeSynth:
    """synth(text, cfg) yielding one short silent chunk; records the texts it was asked for."""

    def __init__(self, rate: int = 24000, fail: bool = False) -> None:
        self.rate = rate
        self.fail = fail
        self.texts: list[str] = []

    def __call__(self, text: str, cfg: Any = None) -> Iterator[tuple[bytes, int]]:
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("synth failed")
        yield (b"\x00\x00" * 480, self.rate)


@dataclass
class FakePlayer:
    """Records played chunks; stop() sets a flag that play() honours between chunks."""

    played: list[bytes] = field(default_factory=list)
    stopped: int = 0

    def play(self, pcm: bytes, rate: int) -> bool:
        self.played.append(pcm)
        return True

    def stop(self) -> None:
        self.stopped += 1


class FakeVad:
    """Scripted speech probabilities, one per 512-sample frame; repeats the last when empty."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = list(probs)
        self.frames = 0

    def prob(self, frame512: Any) -> float:
        self.frames += 1
        if len(self._probs) > 1:
            return self._probs.pop(0)
        return self._probs[0] if self._probs else 0.0
