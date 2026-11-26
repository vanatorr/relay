import json
import os
import time

import pytest

from relayvox import route
from relayvox.route import Ask, Command, RecapEntry, RouteState, Session


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYVOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    route.reset()


def sess(n, name, cwd="C:\\code\\other", status="idle", sid=None):
    return Session(sid or f"id{n}", None, name, cwd, 1000 + n, n, status, n)


def state_with(live, recaps=None, **kw):
    st = RouteState(**kw)
    for sid, text in (recaps or {}).items():
        st.store.recap[sid] = RecapEntry(text, 0.0)
    return st


# The rejected case, as a fixture: the session that TALKS about VPS is not the one fixing it.
ROUTER = sess(3, "Relay router", "C:\\code\\tools\\relay")
INFRA = sess(1, "infra", "C:\\code\\infra")
LIVE = [INFRA, ROUTER]
RECAPS = {
    ROUTER.id: "VPS routing works. The VPS bug in the router needs a VPS test.",
    INFRA.id: "Checked ssh, the server and the Next.js build.",
}


def test_rejected_vps_case_asks_and_never_sends_to_router():
    st = state_with(LIVE, RECAPS)
    cmd, out = route.route_text("tell the one fixing the VPS bug to restart nginx", LIVE, st)
    assert isinstance(out, Ask)
    assert {c.number for c in out.candidates} == {1, 3}
    assert out.question == "Which one: 1, infra, or 3, Relay router?"
    assert out.body == "restart nginx"
    assert cmd.kind == "tell"


def test_one_word_reply_picks_the_session():
    st = state_with(LIVE, RECAPS)
    _, ask = route.route_text("tell the one fixing the VPS bug to restart nginx", LIVE, st)
    assert route.resolve_reply(ask, "one", now=ask.expires_at - 1) is INFRA
    assert route.resolve_reply(ask, "number three", now=ask.expires_at - 1) is ROUTER
    assert route.resolve_reply(ask, "infra", now=ask.expires_at - 1) is INFRA
    assert route.resolve_reply(ask, "neither", now=ask.expires_at - 1) is None
    assert route.resolve_reply(ask, "banana", now=ask.expires_at - 1) is None
    assert route.resolve_reply(ask, "one", now=ask.expires_at + 1) is None  # held for 60 s only


def test_ask_body_expires_after_60_seconds():
    st = state_with(LIVE)
    _, ask = route.route_text("tell the thing to go", LIVE, st, now=100.0)
    assert isinstance(ask, Ask) and ask.expires_at == pytest.approx(160.0)


def test_numbers_parse_and_resolve():
    st = state_with(LIVE)
    for text in ("number three, restart it", "three: restart it", "tell number 3 to restart it"):
        cmd, out = route.route_text(text, LIVE, st)
        assert out is ROUTER, text
        assert cmd.body == "restart it", text


def test_alias_beats_title_and_persists(tmp_path):
    route.set_alias(INFRA.id, "vps")
    st = state_with(LIVE, aliases=route.load_aliases())
    assert st.aliases == {INFRA.id: "vps"}
    cmd, out = route.route_text("tell vps to restart nginx", LIVE, st)
    assert out is INFRA and cmd.body == "restart nginx"
    st_path = tmp_path / "state" / "aliases.json"
    assert json.loads(st_path.read_text()) == {INFRA.id: "vps"}
    route.set_alias(ROUTER.id, "vps")  # one owner per alias
    assert route.load_aliases() == {ROUTER.id: "vps"}


def test_title_and_fuzzy_title():
    st = state_with(LIVE)
    assert route.route_text("tell infra to restart", LIVE, st)[1] is INFRA
    assert route.route_text("tell the relay router to restart", LIVE, st)[1] is ROUTER
    assert route.route_text("tell routing to restart", LIVE, st)[1] is None or isinstance(
        route.route_text("tell routing to restart", LIVE, st)[1], Ask
    )


def test_project_match():
    app = sess(2, "Header polish", "C:\\code\\docs-site")
    live = [INFRA, app]
    st = state_with(live)
    cmd, out = route.route_text("tell docs site to ship it", live, st)
    assert out is app and cmd.body == "ship it"


def test_two_sessions_in_one_project_ask():
    a = sess(1, "alpha", "C:\\code\\docs-site")
    b = sess(2, "beta", "C:\\code\\docs-site")
    st = state_with([a, b])
    _, out = route.route_text("tell docs site to ship it", [a, b], st)
    assert isinstance(out, Ask) and {c.number for c in out.candidates} == {1, 2}


def test_recency_last_one_and_needs_me():
    st = state_with(LIVE, last_spoke=ROUTER.id, needs_you={INFRA.id})
    assert route.route_text("tell the last one to stop", LIVE, st)[1] is ROUTER
    assert route.route_text("tell the one that needs me to continue", LIVE, st)[1] is INFRA
    st2 = state_with(LIVE, needs_you={INFRA.id, ROUTER.id})
    assert isinstance(route.route_text("tell the one asking to continue", LIVE, st2)[1], Ask)


def test_busy_one_only_if_unique():
    live = [sess(1, "a", status="busy"), sess(2, "b", status="idle")]
    st = state_with(live)
    assert route.route_text("tell the busy one to wait", live, st)[1] is live[0]
    live2 = [sess(1, "a", status="busy"), sess(2, "b", status="busy")]
    assert isinstance(
        route.route_text("tell the busy one to wait", live2, state_with(live2))[1], Ask
    )


def test_leading_session_name_then_comma_targets_it_and_strips_the_name():
    app = sess(2, "Header polish", "C:\\code\\docs-site")
    live = [INFRA, app]
    st = state_with(live, on_screen=INFRA.id, last_sent=INFRA.id)
    msg = "Header polish, there's still some UI issues with the header."
    cmd, out = route.route_text(msg, live, st)
    assert out is app  # not the on-screen session
    assert cmd.body == "there's still some UI issues with the header."
    assert route.route_text("infra, restart nginx", live, st)[1] is INFRA


def test_ordinal_opener_in_prose_is_not_a_session_number():
    st = state_with(LIVE, on_screen=ROUTER.id)
    cmd, out = route.route_text("First, run the tests", LIVE, st)
    assert out is ROUTER and cmd.body == "First, run the tests"
    assert route.route_text("two: restart it", [INFRA, sess(2, "b")], st)[1].number == 2


def test_leading_comma_phrase_without_a_name_is_a_plain_message():
    st = state_with(LIVE, on_screen=ROUTER.id)
    cmd, out = route.route_text("Well, I think we should wait", LIVE, st)
    assert out is ROUTER and cmd.body.startswith("Well, I think")


def test_plain_goes_attached_then_on_screen_then_last_sent_never_last_speaker():
    st = state_with(LIVE, last_spoke=ROUTER.id, last_sent=INFRA.id)
    assert route.route_text("restart it please now", LIVE, st)[1] is INFRA
    st.on_screen = ROUTER.id
    assert route.route_text("restart it please now", LIVE, st)[1] is ROUTER
    st.attached = INFRA.id
    assert route.route_text("restart it please now", LIVE, st)[1] is INFRA


def test_plain_with_no_default_asks_who_and_lists_numbers():
    st = state_with(LIVE, last_spoke=ROUTER.id)
    _, out = route.route_text("restart nginx", LIVE, st)
    assert isinstance(out, Ask)
    assert out.question == "Who should I send that to? 1, infra. 3, Relay router."
    assert out.body == "restart nginx"


def test_single_session_gets_plain_messages_and_none_when_empty():
    st = state_with([INFRA])
    assert route.route_text("restart nginx", [INFRA], st)[1] is INFRA
    assert route.route_text("restart nginx", [], state_with([]))[1] is None


def test_read_only_questions_with_no_match_return_none():
    st = state_with(LIVE)
    cmd = route.parse("what did zebra say")
    assert cmd.kind == "recap" and route.resolve(cmd, LIVE, st) is None
    assert route.resolve(route.parse("what did infra say"), LIVE, st) is INFRA


@pytest.mark.parametrize(
    ("text", "kind", "target", "back"),
    [
        ("Be quiet.", "quiet", "", 0),
        ("stop talking", "quiet", "", 0),
        ("tais-toi", "quiet", "", 0),
        ("mute", "mute", "", 0),
        ("Unmute.", "unmute", "", 0),
        ("What's the status on the agents?", "status", "", 0),
        ("status", "status", "", 0),
        ("what's everyone doing", "status", "", 0),
        ("statut", "status", "", 0),
        ("status on infra", "where", "infra", 0),
        ("Where's infra at?", "where", "infra", 0),
        ("how's the relay router doing", "where", "the relay router", 0),
        ("what did infra say", "recap", "infra", 0),
        ("what did infra say before that?", "recap", "infra", 1),
        ("what does number two need", "recap", "number two", 0),
        ("repeat that", "repeat", "", 0),
        ("I didn't get that", "repeat", "", 0),
        ("répète", "repeat", "", 0),
        ("talk to infra", "attach", "infra", 0),
        ("detach", "detach", "", 0),
        ("call number two VPS", "alias", "two", 0),
        ("call that one VPS", "alias", "that one", 0),
        ("what's blocking payments?", "plain", "", 0),
    ],
)
def test_parse(text, kind, target, back):
    c = route.parse(text)
    assert (c.kind, c.target, c.back) == (kind, target, back)


def test_parse_alias_body_and_tell_forms():
    assert route.parse("call number two VPS").body == "VPS"
    c = route.parse("tell infra, restart nginx")
    assert (c.kind, c.target, c.body) == ("tell", "infra", "restart nginx")
    c = route.parse("to infra: restart nginx")
    assert (c.kind, c.target, c.body) == ("tell", "infra", "restart nginx")
    c = route.parse("dis à infra de redémarrer")
    assert c.kind == "tell" and c.target == "infra de redémarrer"
    c = route.parse("Relay, what's blocking payments?")
    assert (c.kind, c.body) == ("ask", "what's blocking payments?")
    assert route.parse("Relay, status").kind == "status"


def test_french_tell_splits_and_drops_joiner():
    st = state_with(LIVE)
    cmd, out = route.route_text("dis à infra de redémarrer nginx", LIVE, st)
    assert out is INFRA and cmd.body == "redémarrer nginx"


def test_split_prefers_longest_strong_target():
    app = sess(2, "Header polish", "C:\\code\\docs-site")
    live = [INFRA, app]
    cmd = route.split_tell(route.parse("tell header polish to fix header"), live, state_with(live))
    assert (cmd.target, cmd.body) == ("header polish", "fix header")


def test_resolution_under_5ms_for_20_sessions():
    live = [sess(i, f"project{i} thing{i}", f"C:\\code\\proj{i}") for i in range(1, 21)]
    st = state_with(live, recaps={s.id: "some recap about various things" for s in live})
    route.route_text("tell project7 thing7 to restart", live, st)  # warm the fuzzy cache
    best = 1.0
    for _ in range(5):
        t0 = time.perf_counter()
        route.route_text("tell the one fixing the router bug to restart nginx", live, st)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.005


def write_session(tmp_path, sid, pid, **kw):
    d = tmp_path / "claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    data = {"sessionId": sid, "pid": pid, "cwd": "C:\\code\\x", "startedAt": 1, **kw}
    (d / f"{pid}.json").write_text(json.dumps(data))


def test_sessions_numbering_dead_pids_status_and_brain(tmp_path):
    me = os.getpid()
    write_session(
        tmp_path, "a", me, name="alpha", startedAt=5, status="busy", hostSessionId="local_a"
    )
    write_session(tmp_path, "b", me + 100000, startedAt=1)  # dead pid
    write_session(tmp_path, "c", me + 1, startedAt=9, kind="background")
    live = route.sessions(force=True)
    assert [(s.id, s.number, s.status, s.name, s.host_id) for s in live] == [
        ("a", 1, "busy", "alpha", "local_a")
    ]
    route.ignore_brain(pid=me, session_id="zzz")
    assert route.sessions(force=True) == []


def test_numbers_are_stable_and_lowest_free(tmp_path, monkeypatch):
    import psutil

    alive = {11, 12, 13}
    monkeypatch.setattr(psutil, "pid_exists", lambda p: p in alive)
    for i, pid in enumerate((11, 12, 13)):
        write_session(tmp_path, f"s{pid}", pid, startedAt=i + 1)
    assert [s.number for s in route.sessions(force=True)] == [1, 2, 3]
    alive.discard(11)
    assert [s.number for s in route.sessions(force=True)] == [2, 3]  # no shifting
    alive.add(14)
    write_session(tmp_path, "s14", 14, startedAt=99)
    assert {s.id: s.number for s in route.sessions(force=True)}["s14"] == 1  # lowest free


def test_status_fallback_uses_hooks_and_big_files_skipped(tmp_path):
    me = os.getpid()
    write_session(tmp_path, "a", me, name="alpha")
    assert route.sessions(force=True)[0].status == "unknown"
    route.note_stop("a")
    assert route.sessions(force=True)[0].status == "idle"
    route.note_delivery("a")
    assert route.sessions(force=True)[0].status == "unknown"
    (tmp_path / "claude" / "sessions" / "big.json").write_text("x" * 70000)
    assert len(route.sessions(force=True)) == 1


def test_remember_keeps_two_older_recaps_and_clears_when_pid_dies(tmp_path):
    for t in ("one", "two", "three", "four"):
        route.remember("a", t, "recap")
    assert route.STORE.recap["a"].text == "four"
    assert [e.text for e in route.STORE.older["a"]] == ["three", "two"]
    route.remember("a", "q?", "ask")
    route.remember("a", "bad", "other")
    assert "a" not in route.STORE.message
    route._numbers["a"] = 1
    route.sessions(force=True)  # "a" is not live: forgotten
    assert "a" not in route.STORE.recap and "a" not in route.STORE.older


def test_delivery_makes_the_sessions_speech_stale():
    dropped = []
    route.on_delivery(lambda sid, before: dropped.append((sid, before)))
    route.remember("a", "Which db?", "ask")
    route.note_delivery("a", 12.5)
    assert dropped == [("a", 12.5)] and "a" not in route.STORE.ask
    assert isinstance(Command("plain"), Command)
