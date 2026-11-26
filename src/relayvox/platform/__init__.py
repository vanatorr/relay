"""Picks windows / macos / linux by sys.platform and re-exports its contract (DESIGN 3, 11)."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from relayvox.platform import windows as impl
elif sys.platform == "darwin":
    from relayvox.platform import macos as impl
else:
    from relayvox.platform import linux as impl

start_key_listener = impl.start_key_listener
on_arm = impl.on_arm
claude_in_front = impl.claude_in_front
remember_front = impl.remember_front
restore_front = impl.restore_front
open_url = impl.open_url
type_text = impl.type_text
press_enter = impl.press_enter
copy_text = impl.copy_text
permissions = impl.permissions

__all__ = [
    "claude_in_front",
    "copy_text",
    "impl",
    "on_arm",
    "open_url",
    "permissions",
    "press_enter",
    "remember_front",
    "restore_front",
    "start_key_listener",
    "type_text",
]
