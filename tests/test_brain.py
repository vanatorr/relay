import json
import threading
import time
from typing import Any

import pytest

from relayvox import brain
from relayvox.route import RecapEntry, RecapStore, Session


def delta(text: str) -> str:
    ev = {"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": text}}}  # fmt: skip
    return json.dumps(ev) + "\n"


RESULT = json.dumps({"type": "result", "is_error": False, "result": ""}) + "\n"
INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "brain-sid"}) + "\n"


class FakeProc:
    """stdout is fed by a script: each question written to stdin releases the next batch."""

    def __init__(self, script: list[list[str]]) -> None:
        self.script = script
        self.written: list[str] = []
        self.pid = 4242
        self.killed = False
        self.q: Any = __import__("queue").Queue()
        self.q.put(["Sign-in ends tomorrow. Run claude auth login\n", INIT])
        self.stdin = self
        self.stdout = self._lines()

    def _lines(self) -> Any:
        while True:
            for line in self.q.get():
                if line is None:
                    return
                yield line

    def write(self, s: str) -> None:
        self.written.append(s)
        if self.script:
            self.q.put(self.script.pop(0))

    def flush(self) -> None:
        pass

    def poll(self) -> int | None:
        return 1 if self.killed else None

    def kill(self) -> None:
        self.killed = True
        self.q.put([None])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    holder: dict[str, Any] = {}

    def make(script: list[list[str]]) -> FakeProc:
        proc = FakeProc(script)
        holder["proc"] = proc
        monkeypatch.setattr(brain, "_popen", lambda *a, **k: (holder.setdefault("kw", k), proc)[1])
        return proc

    monkeypatch.setattr(brain.shutil, "which", lambda n: "claude")
    monkeypatch.setattr(brain.config, "state_dir", lambda: str(tmp_path), raising=False)
    monkeypatch.setattr(brain, "_brain", None)
    monkeypatch.setattr(brain, "_warned_down", False)
    monkeypatch.setattr(brain, "_questions", 0)
    holder["make"] = make
    return holder


def run(question: str = "q", lang: str = "en") -> tuple[list[str], brain.Proposal | None]:
    got: list[str] = []
    prop = brain.ask(question, lang, "ctx", got.append)
    return got, prop


def test_disabled_and_missing(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    assert brain.start({"brain": {"enabled": False}}) is False
    monkeypatch.setattr(brain.shutil, "which", lambda n: None)
    assert brain.start({"brain": {"enabled": True}}) is False


def test_start_guards_and_argv(fake: Any) -> None:
    fake["make"]([])
    assert brain.start({"brain": {"enabled": True, "model": "haiku"}})
    kw = fake["kw"]
    assert kw["env"]["RELAYVOX_BRAIN"] == "1"
    assert kw["cwd"].endswith("brain")
    time.sleep(0.1)
    assert brain.pid() == 4242
    assert brain.session_id() == "brain-sid"  # init read, warning line skipped


def test_streams_sentences_and_holds_propose(fake: Any) -> None:
    fake["make"]([[delta("Infra is busy. It is rest"), delta("arting nginx.\nPROP"),
                   delta("OSE 1: restart nginx\n"), RESULT]])  # fmt: skip
    brain.start({"brain": {"enabled": True}})
    got, prop = run("how is infra", "en")
    assert got == ["Infra is busy.", "It is restarting nginx."]
    assert prop == brain.Proposal(1, "restart nginx")
    msg = json.loads(fake["proc"].written[0])
    assert "Language: en" in msg["message"]["content"]
    assert "Question: how is infra" in msg["message"]["content"]


def test_result_text_fallback_and_french(fake: Any) -> None:
    res = json.dumps({"type": "result", "is_error": False, "result": "Tout va bien."}) + "\n"
    fake["make"]([[res]])
    brain.start({"brain": {"enabled": True}})
    got, prop = run("ça va", "fr")
    assert got == ["Tout va bien."] and prop is None
    assert "Language: fr" in fake["proc"].written[0]


def test_error_result_says_down_once(fake: Any) -> None:
    err = json.dumps({"type": "result", "is_error": True, "result": "login"}) + "\n"
    fake["make"]([[err], [err]])
    brain.start({"brain": {"enabled": True}})
    assert run()[0] == [brain.LINE_DOWN]
    assert run()[0] == []  # same outage: not repeated


def test_process_gone(fake: Any) -> None:
    proc = fake["make"]([])
    brain.start({"brain": {"enabled": True}})
    proc.kill()
    got, prop = run()
    assert got == [brain.LINE_DOWN] and prop is None


def test_first_token_timeout(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    fake["make"]([[]])
    monkeypatch.setattr(brain, "FIRST_TOKEN_SEC", 0.3)
    brain.start({"brain": {"enabled": True}})
    got, _ = run()
    assert got == [brain.LINE_SLOW]


def test_cancel_returns_none(fake: Any) -> None:
    fake["make"]([[]])
    brain.start({"brain": {"enabled": True}})
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(run()))
    t.start()
    time.sleep(0.3)
    brain.cancel()
    t.join(2)
    assert out == [([], None)]


def test_stale_events_ignored(fake: Any) -> None:
    # question 1 gets cancelled; its late output must not leak into question 2
    fake["make"]([[], [RESULT, delta("Second. "), RESULT]])
    brain.start({"brain": {"enabled": True}})
    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    brain.cancel()
    t.join(2)
    got, _ = run()
    assert got == ["Second."]


def test_build_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    proj = tmp_path / "projects" / "slug"
    proj.mkdir(parents=True)
    lines = [
        {"message": {"role": "user", "content": "secret user text"}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Done, mail me at a@b.com key sk-abcdefghijklmnop1234"},
            {"type": "tool_use", "input": {"x": "TOOLDATA"}}]}},
    ]  # fmt: skip
    (proj / "id1.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    s1 = Session("id1", None, "infra", "/home/me/docs-site", 1, 1, "busy", 1)
    store = RecapStore()
    store.recap["id1"] = RecapEntry("word " * 200, time.monotonic() - 120)
    ctx = brain.build_context([s1], store, s1)
    assert "1, infra, docs-site, busy, 2" in ctx
    assert ctx.count("word") == 80  # 400 chars cap
    assert "TOOLDATA" not in ctx and "secret user text" not in ctx
    assert "a@b.com" not in ctx and "sk-abc" not in ctx
    assert "/home/me" not in ctx


def test_prompt_forbids_identity_and_proposals_dont_send() -> None:
    assert "email" in brain.PROMPT and "PROPOSE" in brain.PROMPT
