"""session_history builtin: query the desktop session SQLite store directly."""

from __future__ import annotations

from pathlib import Path

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.frontends.session_store import SessionStore
from zero_agent.tools.registry import ToolRegistry


def _config(tmp_path: Path, sessions_dir: Path | None = None) -> AgentConfig:
    return AgentConfig(
        llm_backends={
            "d": LLMBackendConfig(
                name="d",
                provider="openai",
                api_key="k",
                api_base="https://api.openai.com/v1",
                model="gpt-4o",
            )
        },
        default_backend="d",
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
        sessions_dir=str(sessions_dir or (tmp_path / "sessions")),
    )


def _session(session_id: str = "sess-test") -> dict:
    return {
        "id": session_id,
        "title": "History chat",
        "cwd": "/tmp/project",
        "created_at": 10.0,
        "updated_at": 20.0,
        "msg_seq": 0,
        "last_error": "",
        "terminal_status": "",
        "terminal_reason": "",
        "model_override": None,
        "token_usage": {},
        "group_id": None,
        "sub_agents": [],
        "plan_path": None,
        "plan_status": "inactive",
        "plan_task": "",
    }


def _message(message_id: int, role: str = "user", content: str = "hello") -> dict:
    return {"id": message_id, "role": role, "content": content, "ts": float(message_id)}


def _run(tool, args: dict) -> dict:
    generator = tool.handler(args, None, None)
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return stop.value  # type: ignore[no-any-return]


def _store_with_two_sessions(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "sessions" / "sessions.sqlite3")
    store.initialize()
    first = _session("sess-a")
    store.append_message(first, _message(1, "user", "hello alpha"))
    first["msg_seq"] = 2
    first["updated_at"] = 30.0
    store.append_message(first, _message(2, "assistant", "world beta"))
    second = _session("sess-b")
    second["title"] = "Other chat"
    store.append_message(second, _message(1, "user", "unrelated"))
    store.close()
    return store


def test_session_history_is_registered(tmp_path: Path) -> None:
    registry = ToolRegistry.with_builtins(_config(tmp_path))

    tool = registry.get("session_history")

    assert tool is not None
    assert tool.parameters["required"] == ["action"]


def test_list_read_and_search(tmp_path: Path) -> None:
    _store_with_two_sessions(tmp_path)
    tool = ToolRegistry.with_builtins(_config(tmp_path)).get("session_history")

    listed = _run(tool, {"action": "list"})
    assert listed["status"] == "success"
    assert [item["sessionId"] for item in listed["sessions"]] == ["sess-a", "sess-b"]
    assert listed["sessions"][0]["messageCount"] == 2

    read = _run(tool, {"action": "read", "session_id": "sess-a"})
    assert read["status"] == "success"
    assert read["totalMessages"] == 2
    assert [(m["role"], m["id"]) for m in read["messages"]] == [("user", 1), ("assistant", 2)]
    assert read["messages"][0]["content"] == "hello alpha"

    tail = _run(tool, {"action": "read", "session_id": "sess-a", "after_id": 1})
    assert [m["id"] for m in tail["messages"]] == [2]

    found = _run(tool, {"action": "search", "query": "beta"})
    assert found["status"] == "success"
    assert [(hit["sessionId"], hit["messageId"]) for hit in found["matches"]] == [("sess-a", 2)]

    scoped = _run(tool, {"action": "search", "query": "hello", "session_id": "sess-b"})
    assert scoped["matches"] == []


def test_read_truncates_long_content_unless_full(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions" / "sessions.sqlite3")
    store.initialize()
    session = _session("sess-long")
    store.append_message(session, _message(1, "user", "x" * 5000))
    store.close()
    tool = ToolRegistry.with_builtins(_config(tmp_path)).get("session_history")

    clipped = _run(tool, {"action": "read", "session_id": "sess-long"})
    assert clipped["messages"][0]["truncated_chars"] == 1000
    assert len(clipped["messages"][0]["content"]) == 4000

    full = _run(tool, {"action": "read", "session_id": "sess-long", "full": True})
    assert len(full["messages"][0]["content"]) == 5000


def test_missing_store_and_missing_session_report_errors(tmp_path: Path) -> None:
    tool = ToolRegistry.with_builtins(
        _config(tmp_path, sessions_dir=tmp_path / "nope")
    ).get("session_history")

    result = _run(tool, {"action": "list"})

    assert result["status"] == "error"
    assert "sessions.sqlite3" in result["db"]
