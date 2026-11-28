import relay


def test_tell_project_routes_to_orchestrator(monkeypatch):
    monkeypatch.setitem(relay.CFG, "projects", {"gamedev": "chan_015GJ2soaP6w8hoChoc2tEME"})
    monkeypatch.setattr(relay, "load_sessions", lambda: relay.project_sessions())
    session, body, _ = relay.parse_tell("Tell gamedev what's the status on the water")
    assert session and session["name"] == "gamedev"
    assert "status on the water" in body
    assert relay.session_link(session["host"]) == "claude://claude.ai/code/project/chan_015GJ2soaP6w8hoChoc2tEME"


def test_bad_project_id_ignored(monkeypatch):
    monkeypatch.setitem(relay.CFG, "projects", {"x": "not a chan"})
    assert relay.project_sessions() == []


def test_local_link_unchanged():
    assert relay.session_link("abc") == "claude://claude.ai/epitaxy/abc"
