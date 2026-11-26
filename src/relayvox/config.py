"""Config defaults, TOML load, env overrides, validation, paths (DESIGN section 12)."""

from __future__ import annotations

import difflib
import os
import sys
import tomllib
from typing import Any

import platformdirs

# One dict holds every default. Key names are plain strings: "f8", "right_ctrl", "x1"... are all
# valid values for keys.talk / keys.dictate ("auto" picks the OS default, "" turns dictate off).
DEFAULTS: dict[str, dict[str, Any]] = {
    "keys": {
        "talk": "auto",  # auto: right_ctrl (Windows/Linux), right_option (macOS); also "f8" etc.
        "dictate": "auto",  # auto: right_alt (Windows/Linux), right_cmd (macOS); "" = off
        "mouse": "",  # "x1" or "x2" to hold a mouse side button
        "latch_auto_end": False,
    },
    "speech": {
        "max_sentences": 2,
        "max_chars": 240,
        "speak_when_claude_focused": True,
        "volume": 0.6,
    },
    "tts": {
        "engine": "kokoro",  # kokoro | elevenlabs | edge | system
        "voice": "bm_george",
        "speed": 1.0,
        "kokoro_small": False,
        "voice_fr": "ff_siwis",
        "persian": "english",  # english | edge (cloud, opt-in)
        "other_languages": "english",
        "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
        "elevenlabs_model": "eleven_flash_v2_5",
        "edge_voice": "en-US-AvaMultilingualNeural",
        "model_dir": "",  # "" = the default Kokoro model folder
    },
    "audio": {"mic": "", "max_gain": 20},
    "stt": {
        "model": "auto",
        "language": "",
        "vocabulary": ["Anthropic", "Claude", "Haiku", "Sonnet", "Opus"],  # Whisper prompt words
    },
    "wake": {"enabled": False, "model": "hey_jarvis", "threshold": 0.5},
    "listen": {
        "end": "silence",  # silence | word (hands-free only)
        "end_silence_ms": 1000,
        "end_words": ["send it", "over"],
        "cancel_words": ["cancel", "never mind"],
    },
    "routing": {"cancel_sec": 0.6},
    "brain": {"enabled": False, "default_for": "wake", "model": "haiku"},
    "log": {"transcripts": False},
}


class Config(dict):  # type: ignore[type-arg]
    """Nested settings, `cfg["section"]["key"]`. Built by load(); see DESIGN section 12."""


_ENUMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("tts", "engine"): ("kokoro", "elevenlabs", "edge", "system"),
    ("tts", "persian"): ("english", "edge"),
    ("tts", "other_languages"): ("english", "edge"),
    ("listen", "end"): ("silence", "word"),
    ("brain", "default_for"): ("wake", "both", "none"),
}
_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("speech", "max_sentences"): (1, 10),
    ("speech", "max_chars"): (40, 2000),
    ("speech", "volume"): (0, 1),
    ("tts", "speed"): (0.5, 2.0),
    ("audio", "max_gain"): (1, 100),
    ("wake", "threshold"): (0, 1),
    ("listen", "end_silence_ms"): (200, 5000),
    ("routing", "cancel_sec"): (0, 5),
}


def config_dir() -> str:
    """Config folder; RELAYVOX_CONFIG_DIR overrides (tests)."""
    return os.environ.get("RELAYVOX_CONFIG_DIR") or platformdirs.user_config_dir(
        "relayvox", appauthor=False
    )


def config_path() -> str:
    """Path of config.toml under platformdirs.user_config_dir("relayvox") (section 12)."""
    return os.path.join(config_dir(), "config.toml")


def state_dir() -> str:
    """Per-user state dir: endpoint.json, aliases.json, brain/ (sections 8.1, 10.2). Created."""
    from relayvox.hook import state_dir as _state_dir  # stdlib-only single source of truth

    path = _state_dir()
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return isinstance(value, str)


def validate(cfg: dict[str, Any]) -> list[str]:
    """Return warnings for unknown keys; raise ValueError naming a bad key (section 12)."""
    warnings: list[str] = []
    for sec, vals in cfg.items():
        if sec not in DEFAULTS:
            hint = difflib.get_close_matches(sec, DEFAULTS, n=1)
            tip = f", did you mean [{hint[0]}]?" if hint else ""
            warnings.append(f"unknown section [{sec}]{tip}")
            continue
        if not isinstance(vals, dict):
            raise ValueError(f"[{sec}] must be a table")
        for key, value in vals.items():
            if key not in DEFAULTS[sec]:
                hint = difflib.get_close_matches(key, DEFAULTS[sec], n=1)
                tip = f", did you mean {sec}.{hint[0]}?" if hint else ""
                warnings.append(f"unknown key {sec}.{key}{tip}")
                continue
            if not _type_ok(DEFAULTS[sec][key], value):
                want = type(DEFAULTS[sec][key]).__name__
                raise ValueError(f"{sec}.{key}: expected {want}, got {value!r}")
            if (sec, key) in _ENUMS and value not in _ENUMS[(sec, key)]:
                raise ValueError(f"{sec}.{key}: must be one of {', '.join(_ENUMS[(sec, key)])}")
            if (sec, key) in _RANGES:
                lo, hi = _RANGES[(sec, key)]
                if not lo <= value <= hi:
                    raise ValueError(f"{sec}.{key}: must be between {lo} and {hi}")
    return warnings


def _coerce(default: Any, raw: str, name: str) -> Any:
    low = raw.strip().lower()
    try:
        if isinstance(default, bool):
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off", ""):
                return False
            raise ValueError(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        raise ValueError(f"{name}: cannot read {raw!r}") from None
    if isinstance(default, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def load() -> Config:
    """Defaults, then config.toml, then RELAYVOX_<SECTION>_<KEY> env, validated (section 12)."""
    cfg = Config()
    for sec, vals in DEFAULTS.items():
        cfg[sec] = {k: (list(v) if isinstance(v, list) else v) for k, v in vals.items()}
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path}: {e}") from None
        for w in validate(data):
            print(f"relayvox config: {w}", file=sys.stderr)
        for sec, vals in data.items():
            if sec in cfg and isinstance(vals, dict):
                for key, value in vals.items():
                    if key in cfg[sec]:
                        cfg[sec][key] = value
    for sec, vals in DEFAULTS.items():
        for key, default in vals.items():
            name = f"RELAYVOX_{sec}_{key}".upper()
            if name in os.environ:
                cfg[sec][key] = _coerce(default, os.environ[name], name)
    validate(cfg)
    return cfg


TEMPLATE = """\
[keys]
talk = "auto"        # auto: right_ctrl (Windows/Linux), right_option (macOS)
dictate = "auto"     # auto: right_alt (Windows/Linux), right_cmd (macOS); "" turns it off
mouse = ""           # "x1" or "x2" to hold a mouse side button
latch_auto_end = false

[speech]
max_sentences = 2
max_chars = 240
speak_when_claude_focused = true   # false = chime only while Claude is in front
volume = 0.6

[tts]
engine = "kokoro"    # kokoro | elevenlabs | edge | system
voice = "bm_george"  # per engine; ElevenLabs uses voice_id below
speed = 1.0
kokoro_small = false
voice_fr = "ff_siwis"
persian = "english"   # english | edge  (edge = cloud, opt-in)
other_languages = "english"
elevenlabs_voice_id = "21m00Tcm4TlvDq8ikWAM"
elevenlabs_model = "eleven_flash_v2_5"
edge_voice = "en-US-AvaMultilingualNeural"
model_dir = ""       # "" = the default Kokoro model folder

[audio]
mic = ""             # part of the device name; "" = system default
max_gain = 20

[stt]
model = "auto"
language = ""        # "" = detect
vocabulary = ["Anthropic", "Claude", "Haiku", "Sonnet", "Opus"]  # words Whisper should expect

[wake]
enabled = false
model = "hey_jarvis" # or a path to a custom .onnx
threshold = 0.5

[listen]
end = "silence"      # silence | word   (hands-free only)
end_silence_ms = 1000
end_words = ["send it", "over"]
cancel_words = ["cancel", "never mind"]

[routing]
cancel_sec = 0.6

[brain]
enabled = false      # opt-in; needs the claude CLI and uses your Claude Code login
default_for = "wake" # wake | both | none
model = "haiku"

[log]
transcripts = false
"""


def write_template() -> str:
    """Write the commented template config.toml if missing, return its path (section 12)."""
    path = config_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(TEMPLATE)
    return path
