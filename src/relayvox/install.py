"""Add/remove Relay hooks in the Claude settings.json (DESIGN section 10.4)."""

from __future__ import annotations

import copy
import difflib
import glob
import json
import os
import sys
import tempfile
import time
from typing import Any

OURS = "-m relayvox.hook"
V0_MARKS = ("relay/hook.py", "relay\\hook.py")
WRAP_UP = "mcp__ccd_turn__wrap_up"
KEEP_BACKUPS = 3


class InstallError(Exception):
    """settings.json cannot be edited safely (bad JSON); nothing was written."""


def settings_path() -> str:
    from relayvox import route

    return os.path.join(route.claude_dir(), "settings.json")


def _command() -> str:
    return f'"{sys.executable}" -I {OURS}'


def _is_ours(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks", []):
        cmd = h.get("command", "") if isinstance(h, dict) else ""
        if isinstance(cmd, str) and (OURS in cmd or any(m in cmd for m in V0_MARKS)):
            return True
    return False


def _entries() -> dict[str, dict[str, Any]]:
    hook = [{"type": "command", "command": _command(), "timeout": 3}]
    return {
        "Stop": {"hooks": hook},
        "Notification": {"hooks": hook},
        "PostToolUse": {"matcher": WRAP_UP, "hooks": hook},
    }


def _load(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise InstallError(
            f"{path} is not valid JSON ({e}); fix it by hand. Nothing changed."
        ) from e
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object. Nothing changed.")
    return data


def _edit(data: dict[str, Any], add: bool) -> dict[str, Any]:
    new = copy.deepcopy(data)
    hooks = new.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    entries = _entries()
    for event in list(hooks) + [e for e in entries if e not in hooks]:
        groups = hooks.get(event, [])
        groups = [g for g in groups if not _is_ours(g)] if isinstance(groups, list) else groups
        if add and event in entries:
            groups = [*groups, entries[event]]
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        new["hooks"] = hooks
    else:
        new.pop("hooks", None)
    return new


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _write(path: str, data: dict[str, Any]) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(path, "rb") as src, open(f"{path}.relay-backup-{stamp}", "wb") as dst:
            dst.write(src.read())
        for old in sorted(glob.glob(path + ".relay-backup-*"))[:-KEEP_BACKUPS]:
            os.remove(old)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".settings-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_dump(data))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _apply(add: bool, dry_run: bool) -> str:
    path = settings_path()
    old = _load(path)
    new = _edit(old, add)
    if new == old:
        return "Relay hooks already installed." if add else "No Relay hooks found."
    if dry_run:
        diff = difflib.unified_diff(
            _dump(old).splitlines(True), _dump(new).splitlines(True), "settings.json", "new"
        )
        return "".join(diff)
    _write(path, new)
    return f"{'Installed' if add else 'Removed'} Relay hooks in {path}"


def install_hooks(dry_run: bool = False) -> str:
    """Add Stop, Notification and wrap_up PostToolUse hooks; return a diff or summary (10.4)."""
    return _apply(True, dry_run)


def uninstall_hooks(dry_run: bool = False) -> str:
    """Remove only entries whose command contains `-m relayvox.hook` (section 10.4)."""
    return _apply(False, dry_run)
