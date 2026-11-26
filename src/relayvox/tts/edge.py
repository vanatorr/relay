"""TTS backend edge: edge-tts extra, MP3 decoded with miniaudio (DESIGN section 6.4)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

RATE = 24000


async def _fetch(text: str, voice: str) -> bytes:
    import edge_tts

    out = bytearray()
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            out += chunk["data"]
    return bytes(out)


def synth(text: str, cfg: Any, voice: str | None = None) -> Iterator[tuple[bytes, int]]:
    """Yield (int16 mono PCM, sample_rate); sends the text to Microsoft (opt-in cloud)."""
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        raise RuntimeError("edge voice needs: pip install relayvox[edge]") from None
    import miniaudio

    mp3 = asyncio.run(_fetch(text, voice or str(cfg["tts"]["edge_voice"])))
    if not mp3:
        raise RuntimeError("edge returned no audio")
    dec = miniaudio.decode(
        mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=RATE
    )
    yield bytes(dec.samples), RATE
