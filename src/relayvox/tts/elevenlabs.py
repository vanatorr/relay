"""TTS backend elevenlabs: ElevenLabs streaming PCM over urllib (DESIGN section 6.4)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

RATE = 22050


def synth(text: str, cfg: Any, voice: str | None = None) -> Iterator[tuple[bytes, int]]:
    """Yield (int16 mono PCM, 22.05 kHz) pieces as they stream in (section 6.4).

    The key comes only from the environment and never appears in errors or logs.
    """
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise RuntimeError("ElevenLabs key not set")
    vid = voice or str(cfg["tts"]["elevenlabs_voice_id"])
    if not re.fullmatch(r"[A-Za-z0-9]{10,40}", vid):
        raise RuntimeError("bad ElevenLabs voice id")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream?output_format=pcm_22050"
    body = json.dumps({"text": text, "model_id": str(cfg["tts"]["elevenlabs_model"])}).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed https URL
        url, data=body, headers={"xi-api-key": key, "Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)  # noqa: S310
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ElevenLabs HTTP {e.code}") from None
    except OSError as e:
        raise RuntimeError(f"ElevenLabs unreachable ({type(e).__name__})") from None
    carry = b""
    with resp:
        while True:
            block = resp.read(4096)
            if not block:
                break
            block = carry + block
            carry = block[-1:] if len(block) % 2 else b""
            if len(block) - len(carry) > 0:
                yield block[: len(block) - len(carry)], RATE
