from relayvox import hotkeys
from relayvox.hotkeys import DEAD, HELD, IDLE, LATCHED, PRESSED, TAP_WAIT, Ptt, step


def run(events):
    """events: list of (event, t_ms). Returns (final state, all actions)."""
    s, acts = Ptt(), []
    for ev, t in events:
        s, a = step(s, ev, t)
        acts += a
    return s, acts


def test_hold_opens_mic_after_200ms_and_sends_on_release():
    s, acts = run([("down", 0), ("tick", 100)])
    assert s.state == PRESSED and acts == []
    s, acts = run([("down", 0), ("tick", 100), ("tick", 200)])
    assert s.state == HELD and s.mic_open and acts == ["barge", "mic_on"]
    s, acts = run([("down", 0), ("tick", 200), ("up", 1200)])
    assert s.state == IDLE and not s.mic_open
    assert acts == ["barge", "mic_on", "send"]


def test_short_hold_discards():
    _, acts = run([("down", 0), ("tick", 200), ("up", 400)])
    assert acts == ["barge", "mic_on", "discard"]


def test_tap_cancels_pending_then_quiets():
    s, acts = run([("down", 0), ("up", 80)])
    assert s.state == TAP_WAIT
    assert acts == ["cancel_pending", "quiet"]


def test_tap_wait_expires():
    s, _ = run([("down", 0), ("up", 80), ("tick", 500)])
    assert s.state == IDLE


def test_double_tap_latches_then_tap_sends():
    s, acts = run([("down", 0), ("up", 80), ("down", 250)])
    assert s.state == LATCHED and s.mic_open
    assert acts[-2:] == ["barge", "mic_on"]
    s, a = step(s, "up", 300)  # release of the second press: stay latched
    assert s.state == LATCHED and a == []
    s, a = step(s, "other", 900)  # typing elsewhere does not end it
    assert s.state == LATCHED
    s, a = step(s, "down", 3000)
    assert s.state == IDLE and a == ["send"]


def test_slow_second_tap_is_not_a_double_tap():
    s, acts = run([("down", 0), ("up", 80), ("down", 600)])
    assert s.state == PRESSED and "mic_on" not in acts


def test_esc_discards_latched():
    s, _ = run([("down", 0), ("up", 80), ("down", 250)])
    s, a = step(s, "esc", 2000)
    assert s.state == IDLE and a == ["discard"] and not s.mic_open


def test_other_key_before_hold_is_a_shortcut_and_ignores_autorepeat():
    s, acts = run([("down", 0), ("other", 50)])
    assert s.state == DEAD and acts == []
    s, a = step(s, "down", 80)  # auto-repeat
    s, a2 = step(s, "tick", 400)
    assert a == [] and a2 == [] and s.state == DEAD
    s, _ = step(s, "up", 500)
    assert s.state == IDLE


def test_other_key_during_hold_discards():
    s, acts = run([("down", 0), ("tick", 200), ("other", 900)])
    assert acts == ["barge", "mic_on", "discard"] and s.state == DEAD
    s, _ = step(s, "up", 1000)
    assert s.state == IDLE


def test_autorepeat_downs_ignored_while_held():
    _, acts = run([("down", 0), ("tick", 200), ("down", 230), ("down", 260), ("up", 2000)])
    assert acts == ["barge", "mic_on", "send"]


def test_120s_cap_sends_once():
    s, acts = run([("down", 0), ("tick", 200), ("tick", 200 + 120_000)])
    assert acts == ["barge", "mic_on", "send"] and s.state == DEAD
    s, a = step(s, "up", 130_000)
    assert a == [] and s.state == IDLE


def test_latched_cap():
    s, _ = run([("down", 0), ("up", 50), ("down", 100)])
    s, a = step(s, "tick", 100 + 120_000)
    assert a == ["send"] and s.state == IDLE


def test_toggle_acts_like_double_tap():
    s, a = hotkeys.toggle(Ptt(), 0)
    assert s.state == LATCHED and a == ["barge", "mic_on"]
    s, a = hotkeys.toggle(s, 2000)
    assert s.state == IDLE and a == ["send"]


def test_idle_ignores_stray_events():
    for ev in ("up", "other", "esc", "tick"):
        assert step(Ptt(), ev, 5) == (Ptt(), [])


def test_resolve_keys():
    cfg = {"keys": {"talk": "auto", "dictate": "", "mouse": "x1", "latch_auto_end": False}}
    keys = hotkeys.resolve_keys(cfg)
    assert keys["talk"] == hotkeys.default_keys()["talk"]
    assert "dictate" not in keys and keys["mouse"] == "x1"


def test_worker_only_one_binding_records():
    got = []
    hotkeys._machines.clear()
    hotkeys._machines["talk"] = Ptt(HELD, 0, True)
    hotkeys._handle("dictate", "down", 500, lambda b, a: got.append((b, a)), None)
    assert got == [("talk", "discard")]  # the other key counts as "another key" to the recorder
    assert hotkeys._machines["dictate"].state == IDLE
    hotkeys._machines.clear()


def test_worker_calls_on_arm_and_actions():
    class Plat:
        def __init__(self):
            self.armed = []

        def on_arm(self, b):
            self.armed.append(b)

    got, plat = [], Plat()
    hotkeys._machines.clear()

    def cb(b, a):
        got.append((b, a))

    hotkeys._handle("talk", "down", 0, cb, plat)
    hotkeys._handle("*", "tick", 250, cb, plat)
    hotkeys._handle("talk", "up", 1500, cb, plat)
    assert plat.armed == ["talk"]
    assert got == [("talk", "barge"), ("talk", "mic_on"), ("talk", "send")]
    hotkeys._machines.clear()
