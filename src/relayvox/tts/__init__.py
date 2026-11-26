"""synth(text, cfg): picks the backend, falls back to system, then a chime (DESIGN section 6.4).

Extras beyond the contract: synth(..., lang="en") picks the voice per language (section 8B.5),
language_note(lang, cfg) returns the one-time "I'll answer in English" line.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("relayvox.tts")

_EDGE_VOICES = {"fa": "fa-IR-FaridNeural", "fr": "fr-FR-HenriNeural"}
_noted: set[str] = set()


def _plan(lang: str, cfg: Any) -> tuple[str, dict[str, Any]]:
    """(engine, backend kwargs) for a language; falls back to English per section 8B.5."""
    t = cfg["tts"]
    engine = str(t["engine"])
    if lang == "fr":
        if engine == "kokoro":
            return "kokoro", {"voice": str(t["voice_fr"]), "lang": "fr-fr"}
        if engine == "edge":
            return "edge", {"voice": _EDGE_VOICES["fr"]}
        return engine, {}
    if lang in ("", "en"):
        return engine, {}
    opt_in = str(t["persian"] if lang == "fa" else t["other_languages"])
    if opt_in == "edge":
        return "edge", {"voice": _EDGE_VOICES.get(lang, str(t["edge_voice"]))}
    return engine, {}  # English voice


def language_note(lang: str, cfg: Any) -> str | None:
    """The "I'll answer in English" line, once per run, when a language has no local voice."""
    if lang in ("", "en", "fr") or lang in _noted:
        return None
    engine, _ = _plan(lang, cfg)
    if engine == "edge":
        return None
    _noted.add(lang)
    return "I'll answer in English. For Persian replies, turn on the Edge voice."


def _chime() -> Iterator[tuple[bytes, int]]:
    import numpy as np

    rate = 24000
    t = np.arange(int(rate * 0.25)) / rate
    wave = np.sin(2 * math.pi * 660 * t) * np.minimum(1.0, (0.25 - t) * 20) * 0.25
    yield (wave * 32767).astype(np.int16).tobytes(), rate


def _run(engine: str, text: str, cfg: Any, kw: dict[str, Any]) -> Iterator[tuple[bytes, int]]:
    if engine == "elevenlabs":
        from relayvox.tts import elevenlabs

        return elevenlabs.synth(text, cfg, **{k: v for k, v in kw.items() if k == "voice"})
    if engine == "edge":
        from relayvox.tts import edge

        return edge.synth(text, cfg, **{k: v for k, v in kw.items() if k == "voice"})
    if engine == "system":
        from relayvox.tts import system

        return system.synth(text, cfg)
    from relayvox.tts import kokoro

    return kokoro.synth(text, cfg, **kw)


def synth(text: str, cfg: Any, lang: str = "en") -> Iterator[tuple[bytes, int]]:
    """Yield (int16 mono PCM, sample_rate) from the configured engine (section 6.4)."""
    engine, kw = _plan(lang, cfg)
    attempts: list[tuple[str, dict[str, Any]]] = [(engine, kw)]
    if engine != "system":
        attempts.append(("system", {}))
    for eng, k in attempts:
        yielded = False
        try:
            for chunk in _run(eng, text, cfg, k):
                yielded = True
                yield chunk
            return
        except Exception as e:  # incl. NotImplementedError from an unbuilt backend
            log.warning("tts engine %s failed (%s)", eng, type(e).__name__)
            if yielded:
                return  # never restart a half-played line in another voice
    yield from _chime()
