import pytest

from relayvox import events, route


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path))
    route.reset()
    events.configure(2, 240)
    events._spoken.clear()


def wrap(sid="s1", recap="Fixed the build. Tests pass.", ask="", **kw):
    p = {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": "mcp__x__wrap_up"}
    p["tool_input"] = {"recap": recap, "ask": ask}
    p.update(kw)
    return p


def stop(sid="s1", text="Done."):
    return {"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": text}


def test_wrap_up_speaks_immediately_and_stop_is_silent():
    assert events.parse(wrap()) == ("status", "s1", "Fixed the build. Tests pass.")
    assert events.parse(stop()) is None
    assert events.parse(stop()) == ("status", "s1", "Done.")  # next turn speaks again


def test_wrap_up_with_ask_is_needs_you_and_keeps_ask_whole():
    out = events.parse(wrap(recap="Did A. Did B. Did C.", ask="Ship it? Or wait for review."))
    assert out == ("needs_you", "s1", "Did A. Ship it?")


def test_max_sentences_one_hears_only_the_ask():
    events.configure(1, 240)
    assert events.parse(wrap(recap="Did A.", ask="Ship it?"))[2] == "Ship it?"


def test_hook_forwarded_recap_fields():
    p = {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_name": "wrap_up",
        "recap": "Forwarded recap.",
    }
    assert events.parse(p) == ("status", "s1", "Forwarded recap.")


def test_wrap_up_remembers_recap_and_ask():
    events.parse(wrap(recap="Recap text.", ask="Question?"))
    assert route.STORE.recap["s1"].text == "Recap text."
    assert route.STORE.ask["s1"].text == "Question?"


def test_stop_uses_last_message_and_remembers():
    out = events.parse(stop("s2", "All done. Bye. More."))
    assert out == ("status", "s2", "All done. Bye.")
    assert route.STORE.message["s2"].text.startswith("All done")


def test_stop_marks_session_idle_for_status_fallback():
    events.parse(stop("s9"))
    assert "s9" in route._hook_idle


def test_stop_hook_active_ignored():
    p = stop("s", "x.")
    p["stop_hook_active"] = True
    assert events.parse(p) is None


def test_notifications():
    n = {"hook_event_name": "Notification", "session_id": "s"}
    assert events.parse({**n, "notification_type": "permission_prompt"}) == (
        "needs_you",
        "s",
        "Needs your approval.",
    )
    q = {**n, "notification_type": "elicitation_dialog", "message": "Which db? Postgres or sqlite."}
    assert events.parse(q) == ("needs_you", "s", "Has a question. Which db?")
    assert events.parse({**n, "notification_type": "idle_prompt"}) is None


def test_subagents_bad_ids_and_types_dropped():
    assert events.parse(wrap(agent_id="a1")) is None
    assert events.parse(wrap(sid="bad id!")) is None
    assert events.parse(wrap(sid="x" * 129)) is None
    assert events.parse({"hook_event_name": "Stop", "session_id": 5}) is None
    assert events.parse(stop(text=7)) is None
    assert events.parse("nope") is None  # type: ignore[arg-type]


def test_brain_session_dropped():
    route.ignore_brain(session_id="brain1")
    assert events.parse(wrap(sid="brain1")) is None


def test_clean_rules():
    t = "See `foo` at https://x.io/a and C:\\code\\a.py or src/app/main.py.\n```py\nx=1\n```\nDone."
    assert events.clean(t) == "See foo at a link and a file or a file. Done."


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnop1234",
        "ghp_" + "a" * 36,
        "AKIAIOSFODNN7EXAMPLE",
        "a" * 32,
        "d41d8cd98f00b204e9800998ecf8427e",
    ],
)
def test_keys_read_as_a_key(secret):
    assert events.clean(f"token {secret} end") == "token a key end"


def test_summary_limits():
    long = " ".join(f"Sentence number {i} is here." for i in range(10))
    assert (
        events.summarize(long, "", 2, 240)
        == "Sentence number 0 is here. Sentence number 1 is here."
    )
    assert len(events.summarize("word " * 100 + ".", "", 2, 50)) <= 50
    mixed = events.summarize("word " * 60 + ".", "Q?", 2, 60)
    assert mixed.endswith("Q?") and len(mixed) <= 60
    assert events.summarize("word " * 60 + ".", "Q?", 2, 20) == "Q?"  # no room: ask only
