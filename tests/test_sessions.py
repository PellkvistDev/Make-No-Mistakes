"""Session persistence and transcript -> display-item conversion."""

from glmcode.sessions import SessionStore, derive_title, to_display


def test_save_load_roundtrip(tmp_path):
    store = SessionStore(root=tmp_path)
    msgs = [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"}]
    store.save("s1", "/proj", msgs, 10, 20, todos=[], title="My Chat")
    data = store.load("s1")
    assert data["title"] == "My Chat"
    assert data["cwd"] == "/proj"
    assert data["messages"] == msgs
    assert data["prompt_tokens"] == 10 and data["completion_tokens"] == 20
    assert data["auto_backup"] is True  # default


def test_auto_backup_flag_persisted(tmp_path):
    store = SessionStore(root=tmp_path)
    msgs = [{"role": "user", "content": "x"}]
    store.save("s2", "/p", msgs, 0, 0, auto_backup=False)
    assert store.load("s2")["auto_backup"] is False


def test_system_messages_never_persisted(tmp_path):
    store = SessionStore(root=tmp_path)
    msgs = [{"role": "system", "content": "SECRET SYSTEM PROMPT"},
            {"role": "user", "content": "hi"}]
    store.save("s3", "/p", msgs, 0, 0)
    saved = store.load("s3")["messages"]
    assert all(m["role"] != "system" for m in saved)


def test_empty_session_not_persisted(tmp_path):
    store = SessionStore(root=tmp_path)
    store.save("s4", "/p", [{"role": "system", "content": "only system"}], 0, 0)
    assert store.load("s4") is None


def test_delete_and_list(tmp_path):
    store = SessionStore(root=tmp_path)
    store.save("a", "/p", [{"role": "user", "content": "first chat"}], 0, 0)
    store.save("b", "/p", [{"role": "user", "content": "second chat"}], 0, 0)
    assert {s["id"] for s in store.list()} == {"a", "b"}
    store.delete("a")
    assert {s["id"] for s in store.list()} == {"b"}


def test_derive_title():
    msgs = [{"role": "user", "content": "fix the login bug in auth.py\nmore detail"}]
    assert derive_title(msgs) == "fix the login bug in auth.py more detail"
    long = [{"role": "user", "content": "x" * 200}]
    assert derive_title(long).endswith("…")


def test_to_display_tool_calls_and_results():
    msgs = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "function": {"name": "read_file",
                                      "arguments": '{"path": "x.py"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "file contents"},
        {"role": "assistant", "content": "done!"},
    ]
    items = to_display(msgs)
    kinds = [i["kind"] for i in items]
    assert kinds == ["user", "tool", "assistant"]
    tool = items[1]
    assert tool["name"] == "read_file"
    assert tool["args"] == {"path": "x.py"}
    assert tool["result"] == "file contents"
    assert tool["error"] is False


def test_to_display_marks_errors():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "function": {"name": "run_powershell",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ERROR: it broke"},
    ]
    items = to_display(msgs)
    assert items[1]["error"] is True
