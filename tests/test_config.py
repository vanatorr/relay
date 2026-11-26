import os
import pathlib

import pytest

from relayvox import config


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path / "state"))


def test_defaults_when_no_file():
    cfg = config.load()
    assert cfg["speech"]["max_sentences"] == 2
    assert cfg["tts"]["voice"] == "bm_george"
    cfg["listen"]["end_words"].append("x")
    assert config.load()["listen"]["end_words"] == ["send it", "over"]  # no shared mutation


def test_file_overrides_defaults():
    path = pathlib.Path(config.write_template())
    text = path.read_text(encoding="utf-8").replace("max_sentences = 2", "max_sentences = 1")
    path.write_text(text, encoding="utf-8")
    assert config.load()["speech"]["max_sentences"] == 1


def test_template_matches_defaults():
    path = config.write_template()
    assert path.endswith("config.toml")
    cfg = config.load()
    for sec, vals in config.DEFAULTS.items():
        assert cfg[sec] == vals


def test_write_template_never_overwrites():
    path = config.write_template()
    pathlib.Path(path).write_text("[speech]\nmax_chars = 100\n", encoding="utf-8")
    config.write_template()
    assert config.load()["speech"]["max_chars"] == 100


def test_env_overrides_file(monkeypatch):
    monkeypatch.setenv("RELAYVOX_SPEECH_MAX_SENTENCES", "1")
    monkeypatch.setenv("RELAYVOX_WAKE_ENABLED", "true")
    monkeypatch.setenv("RELAYVOX_LISTEN_END_WORDS", "go, done")
    cfg = config.load()
    assert cfg["speech"]["max_sentences"] == 1
    assert cfg["wake"]["enabled"] is True
    assert cfg["listen"]["end_words"] == ["go", "done"]


def test_bad_env_value_names_the_variable(monkeypatch):
    monkeypatch.setenv("RELAYVOX_SPEECH_MAX_CHARS", "lots")
    with pytest.raises(ValueError, match="RELAYVOX_SPEECH_MAX_CHARS"):
        config.load()


def test_unknown_keys_warn_with_hint():
    w = config.validate({"speech": {"max_sentence": 2}, "sppech": {}})
    assert any("speech.max_sentences" in x for x in w)
    assert any("[speech]" in x for x in w)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("speech", "max_sentences", "two"),
        ("speech", "max_sentences", True),
        ("speech", "volume", 3),
        ("tts", "engine", "robot"),
        ("brain", "default_for", "sometimes"),
        ("wake", "enabled", "yes"),
    ],
)
def test_bad_value_names_the_key(section, key, value):
    with pytest.raises(ValueError, match=f"{section}.{key}"):
        config.validate({section: {key: value}})


def test_int_allowed_for_float():
    config.validate({"tts": {"speed": 1}})


def test_state_dir_created_and_shared_with_hook(tmp_path):
    from relayvox import hook

    assert config.state_dir() == hook.state_dir() == str(tmp_path / "state")
    assert (tmp_path / "state").is_dir()


def test_bad_toml_names_file():
    path = config.config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pathlib.Path(path).write_text("[speech\n", encoding="utf-8")
    with pytest.raises(ValueError, match="config.toml"):
        config.load()
