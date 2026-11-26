import glob
import json
import os

import pytest

from relayvox import install


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path / "settings.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def other_hook(cmd="echo hi"):
    return {"hooks": [{"type": "command", "command": cmd}]}


def test_creates_missing_settings_with_all_three_hooks(settings):
    msg = install.install_hooks()
    assert "Installed" in msg
    hooks = read(settings)["hooks"]
    assert set(hooks) == {"Stop", "Notification", "PostToolUse"}
    assert hooks["PostToolUse"][0]["matcher"] == "mcp__ccd_turn__wrap_up"
    cmd = hooks["Stop"][0]["hooks"][0]["command"]
    assert "-I -m relayvox.hook" in cmd and cmd.startswith('"')
    assert hooks["Stop"][0]["hooks"][0]["timeout"] == 3


def test_idempotent_writes_nothing_second_time(settings):
    install.install_hooks()
    before = settings.read_text(encoding="utf-8")
    mtime = os.stat(settings).st_mtime_ns
    assert "already" in install.install_hooks()
    assert settings.read_text(encoding="utf-8") == before
    assert os.stat(settings).st_mtime_ns == mtime
    assert glob.glob(str(settings) + ".relay-backup-*") == []


def test_keeps_other_hooks_and_settings(settings):
    settings.write_text(
        json.dumps(
            {"model": "x", "hooks": {"Stop": [other_hook()], "PreToolUse": [other_hook("p")]}}
        )
    )
    install.install_hooks()
    data = read(settings)
    assert data["model"] == "x" and data["hooks"]["PreToolUse"] == [other_hook("p")]
    assert data["hooks"]["Stop"][0] == other_hook() and len(data["hooks"]["Stop"]) == 2
    install.uninstall_hooks()
    data = read(settings)
    assert data["hooks"]["Stop"] == [other_hook()] and "Notification" not in data["hooks"]


def test_removes_v0_entries_on_install(settings):
    v0 = other_hook('"C:/py/python.exe" "C:/code/tools/relay/hook.py"')
    settings.write_text(json.dumps({"hooks": {"Stop": [v0]}}))
    install.install_hooks()
    stop = read(settings)["hooks"]["Stop"]
    assert len(stop) == 1 and "relayvox.hook" in stop[0]["hooks"][0]["command"]


def test_aborts_on_bad_json_and_never_overwrites(settings):
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(install.InstallError):
        install.install_hooks()
    assert settings.read_text(encoding="utf-8") == "{not json"


def test_dry_run_prints_diff_and_writes_nothing(settings):
    out = install.install_hooks(dry_run=True)
    assert "+" in out and "relayvox.hook" in out
    assert not settings.exists()
    install.install_hooks()
    assert "-" in install.uninstall_hooks(dry_run=True)  # a diff removing our entries
    assert "relayvox.hook" in read(settings)["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_backup_made_and_only_last_three_kept(settings):
    settings.write_text(json.dumps({"model": "a"}))
    install.install_hooks()
    for i in range(6):
        data = read(settings)
        data["hooks"]["Stop"][0]["hooks"][0]["command"] += f" {i}"  # force a rewrite
        settings.write_text(json.dumps(data))
        install.install_hooks()
        os.utime(settings)
    backups = glob.glob(str(settings) + ".relay-backup-*")
    assert 1 <= len(backups) <= 3


def test_uninstall_when_nothing_installed(settings):
    assert "No Relay hooks" in install.uninstall_hooks()
    assert not settings.exists()
