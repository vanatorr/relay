"""DESIGN invariants, checked on the source tree with ast (no imports, nothing runs)."""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "relayvox"
FILES = sorted(SRC.rglob("*.py"))


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def calls(t):
    for node in ast.walk(t):
        if isinstance(node, ast.Call):
            yield node


def attr_name(func):
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


def imported(t):
    names = set()
    for node in ast.walk(t):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            names |= {f"{mod}.{a.name}" if mod else a.name for a in node.names}
    return names


def test_source_tree_found():
    assert (SRC / "app.py").exists() and len(FILES) > 10


def test_only_app_calls_deliver_send():
    callers = {
        p.relative_to(SRC).as_posix()
        for p in FILES
        for c in calls(tree(p))
        if attr_name(c.func) == "deliver.send"
    }
    assert callers == {"app.py"}
    # and inside app.py only _deliver sends
    t = tree(SRC / "app.py")
    owners = {
        fn.name
        for fn in ast.walk(t)
        if isinstance(fn, ast.FunctionDef)
        for c in calls(fn)
        if attr_name(c.func) == "deliver.send"
    }
    assert owners == {"_deliver"}


def test_deliver_and_send_reached_only_from_an_utterance():
    t = tree(SRC / "app.py")
    users = {
        fn.name
        for fn in ast.walk(t)
        if isinstance(fn, ast.FunctionDef)
        for c in calls(fn)
        if attr_name(c.func) == "_deliver"
    }
    assert users == {"_utterance"}


def test_answers_and_brain_never_import_deliver():
    for name in ("answers.py", "brain.py"):
        names = imported(tree(SRC / name))
        assert not any(n.split(".")[-1] == "deliver" for n in names), name


def test_brain_proposal_is_sent_only_after_a_spoken_yes():
    t = tree(SRC / "app.py")
    fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "_utterance")
    sends = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and any(attr_name(c.func) == "_is_yes" for c in calls(node.test))
    ]
    assert len(sends) == 1
    assert any(
        attr_name(c.func) == "_deliver" and ast.unparse(c.args[1]) == "prop.message"
        for c in calls(sends[0])
    )
    # nowhere else delivers the proposal's message
    others = [c for c in calls(t) if attr_name(c.func) == "_deliver" and "prop" in ast.unparse(c)]
    assert len(others) == 1


def test_no_shell_true_anywhere():
    for p in FILES:
        for c in calls(tree(p)):
            for kw in c.keywords:
                if kw.arg == "shell":
                    assert isinstance(kw.value, ast.Constant) and kw.value.value is False, p


def test_hook_prints_nothing():
    t = tree(SRC / "hook.py")
    for c in calls(t):
        assert attr_name(c.func) not in ("print", "sys.stdout.write"), "hook must stay silent"
    assert "stdout" not in (SRC / "hook.py").read_text(encoding="utf-8")


def test_ipc_has_no_type_that_delivers():
    t = tree(SRC / "app.py")
    for node in ast.walk(t):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_on_"):
            names = {attr_name(c.func) for c in calls(node)}
            assert "_deliver" not in names and "deliver.send" not in names, node.name
