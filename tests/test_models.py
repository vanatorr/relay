import hashlib
import io
import os

import numpy as np
import pytest

from relayvox import cli
from relayvox.tts import kokoro, models


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cfg(tmp_path):
    return {
        "tts": {"model_dir": str(tmp_path / "k"), "kokoro_small": False},
        "wake": {"model": "hey_jarvis"},
    }


def _fake_net(monkeypatch, payload=b"data"):
    calls = []

    def urlopen(req, timeout=0):
        calls.append(req.full_url)
        return FakeResp(payload)

    monkeypatch.setattr(models.urllib.request, "urlopen", urlopen)
    return calls


def test_fetch_verifies_and_renames(tmp_path, monkeypatch):
    calls = _fake_net(monkeypatch)
    dest = str(tmp_path / "a" / "f.bin")
    models.fetch("https://example.com/f.bin", dest, hashlib.sha256(b"data").hexdigest())
    with open(dest, "rb") as f:
        assert f.read() == b"data"
    assert not os.path.exists(dest + ".part")
    assert calls == ["https://example.com/f.bin"]


def test_fetch_bad_checksum_and_http(tmp_path, monkeypatch):
    _fake_net(monkeypatch)
    dest = str(tmp_path / "f.bin")
    with pytest.raises(RuntimeError):
        models.fetch("https://example.com/f.bin", dest, "0" * 64)
    assert not os.path.exists(dest) and not os.path.exists(dest + ".part")
    with pytest.raises(ValueError):
        models.fetch("http://example.com/f.bin", dest, "0" * 64)


def test_download_models_skips_present_and_prints(cfg, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(models, "fetch", lambda url, dest, sha: calls.append((url, dest)))
    monkeypatch.setattr(models, "_bundled_vad", lambda: True)
    monkeypatch.setattr(models, "data_dir", lambda sub: os.path.join(cfg["tts"]["model_dir"], sub))
    os.makedirs(cfg["tts"]["model_dir"])
    open(os.path.join(cfg["tts"]["model_dir"], "kokoro-v1.0.onnx"), "wb").close()
    assert models.download_models(False, cfg) == 0
    assert [os.path.basename(d) for _, d in calls] == ["voices-v1.0.bin"]
    assert all(u.startswith("https://") for u, _ in calls)
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "already present" in out[0]
    calls.clear()
    assert models.download_models(True, cfg) == 0
    names = {os.path.basename(d) for _, d in calls}
    assert {"melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"} <= names
    assert "silero_vad_v6.onnx" not in names


def test_download_failure_is_reported(cfg, monkeypatch, capsys):
    def boom(url, dest, sha):
        raise OSError("offline")

    monkeypatch.setattr(models, "fetch", boom)
    assert models.download_models(False, cfg) == 1
    assert "failed" in capsys.readouterr().out


def test_pins_are_https_and_kokoro_pinned():
    assert kokoro._BASE.startswith("https://")
    for url, sha in models.WAKE_FILES.values():
        assert url.startswith("https://") and len(sha) == 64
    assert models.VAD_FILE[1].startswith("https://")


def test_voices(cfg, capsys):
    assert models.voices(cfg) == 1
    assert "relayvox models" in capsys.readouterr().out
    os.makedirs(cfg["tts"]["model_dir"])
    path = os.path.join(cfg["tts"]["model_dir"], models.VOICES_FILE)
    with open(path, "wb") as f:
        np.savez(f, bm_george=np.zeros(2), af_heart=np.zeros(2))
    assert models.voices(cfg) == 0
    assert capsys.readouterr().out.split() == ["af_heart", "bm_george"]


def test_cli_models_calls_download(monkeypatch):
    got = []
    import relayvox.tts as tts_pkg

    monkeypatch.setattr(tts_pkg, "download_models", lambda wake=False: got.append(wake) or 0)
    assert cli.main(["models", "--wake"]) == 0
    assert got == [True]
