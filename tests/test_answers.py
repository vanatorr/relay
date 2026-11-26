from relayvox.answers import fast_path, recap, status, where
from relayvox.route import Command, RecapEntry, RecapStore, Session


def sess(n: int, name: str, st: str = "idle") -> Session:
    return Session(f"id{n}", None, name, f"/x/{name}", 100 + n, n, st, n)


def store() -> RecapStore:
    s = RecapStore()
    s.recap["id1"] = RecapEntry("Fixed nginx. Deployed it. Tests pass.", 10.0)
    s.recap["id2"] = RecapEntry("Refactored router.", 10.0)
    s.ask["id2"] = RecapEntry("Should I merge? Say yes.", 20.0)
    s.older["id1"] = [RecapEntry("Older one.", 1.0)]
    return s


def test_status_order_and_lines():
    out = status([sess(1, "infra", "busy"), sess(2, "router"), sess(3, "docs")], store())
    assert out.startswith("2, router: needs you. Should I merge?")
    assert out.index("1, infra: busy.") < out.index("3, docs: idle.")


def test_status_empty_and_overflow():
    assert status([], RecapStore()) == "No sessions running."
    live = [sess(i, f"s{i}") for i in range(1, 10)]
    assert status(live, RecapStore()).endswith("and 3 more idle.")


def test_status_fr():
    assert status([], RecapStore(), "fr") == "Aucune session en cours."
    assert "a besoin de vous" in status([sess(2, "router")], store(), "fr")


def test_where_two_sentences():
    assert where(sess(1, "infra", "busy"), store()) == "1, infra: busy. Fixed nginx. Deployed it."


def test_recap_and_back():
    st = store()
    assert "Refactored router. Should I merge?" in recap(sess(2, "router"), st)
    assert "Older one." in recap(sess(1, "infra"), st, back=1)
    assert recap(sess(1, "infra"), st, back=2) == "Nothing earlier from infra."
    assert recap(sess(5, "x"), st) == "x hasn't said anything yet."


def test_recap_cap():
    st = RecapStore()
    st.recap["id1"] = RecapEntry("word " * 400, 1.0)
    assert len(recap(sess(1, "a"), st)) <= 490


def test_fast_path_routes():
    live = [sess(1, "infra", "busy"), sess(2, "router")]
    st = store()
    assert (fast_path(Command("status"), live, st) or "").startswith("2, router")
    assert (fast_path(Command("where", target="infra"), live, st) or "").startswith("1, infra")
    two = fast_path(Command("recap", target="number two"), live, st)
    assert (two or "").startswith("2, router")
    assert (fast_path(Command("recap", target="nobody"), live, st) or "").startswith("Which one:")
    assert fast_path(Command("repeat"), live, st) is None
    assert fast_path(Command("plain", body="hi"), live, st) is None


def test_fast_path_fr_target():
    live = [sess(1, "infra"), sess(2, "router")]
    out = fast_path(Command("where", target="numéro deux"), live, store(), "fr")
    assert out is not None
    assert out.startswith("2, router")
    assert "needs" not in out
