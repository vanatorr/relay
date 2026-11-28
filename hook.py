"""Claude Code hook: forward the hook payload to the local Relay. Never blocks, never fails."""
import os
import sys
import urllib.request

if os.environ.get("RELAYVOX_BRAIN") == "1":
    sys.exit(0)  # the brain's own process must never show up as a session

try:
    data = sys.stdin.buffer.read()
    req = urllib.request.Request(
        "http://127.0.0.1:47811/event", data=data, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=0.5).read()
except Exception:
    pass
sys.exit(0)
