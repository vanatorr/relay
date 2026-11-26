"""Add (or with --remove, take out) the Relay hooks in ~/.claude/settings.json."""
import json
import os
import shutil
import sys

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
PY = sys.executable.replace("\\", "/")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py").replace("\\", "/")
CMD = f'"{PY}" "{HOOK}"'
MARK = "relay/hook.py"

ENTRIES = {
    "Stop": {"hooks": [{"type": "command", "command": CMD, "timeout": 5}]},
    "Notification": {"hooks": [{"type": "command", "command": CMD, "timeout": 5}]},
    "PostToolUse": {"matcher": "mcp__ccd_turn__wrap_up",
                    "hooks": [{"type": "command", "command": CMD, "timeout": 5}]},
}


def is_ours(group):
    return any(MARK in (h.get("command") or "") for h in group.get("hooks", []))


def main():
    remove = "--remove" in sys.argv
    with open(SETTINGS, encoding="utf-8") as f:
        cfg = json.load(f)
    shutil.copy(SETTINGS, SETTINGS + ".before-relay")
    hooks = cfg.setdefault("hooks", {})
    for event, entry in ENTRIES.items():
        groups = [g for g in hooks.get(event, []) if not is_ours(g)]
        if not remove:
            groups.append(entry)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        cfg.pop("hooks")
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("removed" if remove else "installed", "Relay hooks in", SETTINGS)


if __name__ == "__main__":
    main()
