"""Contract tests for SQLite desktop session persistence."""

from __future__ import annotations

import json
import sqlite3

import pytest

from zero_agent.frontends.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path / "sessions.sqlite3")
    value.initialize()
    yield value
    value.close()


def session_record(session_id: str = "sess-test") -> dict:
    return {
        "id": session_id,
        "title": "Test chat",
        "cwd": "/tmp/project",
        "created_at": 10.0,
        "updated_at": 20.0,
        "msg_seq": 0,
        "last_error": "",
        "terminal_status": "",
        "terminal_reason": "",
        "model_override": None,
        "token_usage": {"input": 3},
        "group_id": None,
        "sub_agents": [],
        "plan_path": None,
        "plan_status": "inactive",
        "plan_task": "",
    }


def message_record(message_id: int = 1, role: str = "user", content: str = "hello") -> dict:
    return {
        "id": message_id,
        "role": role,
        "content": content,
        "ts": float(message_id),
        "image_ids": ["img-1"] if role == "user" else [],
    }


def test_initialize_creates_idempotent_schema(store, tmp_path):
    store.initialize()

    with sqlite3.connect(tmp_path / "sessions.sqlite3") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"sessions", "messages", "groups", "app_state", "schema_meta"} <= tables
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone() == ("1",)


def test_new_store_has_no_sessions_and_does_not_read_legacy_json(store, tmp_path):
    legacy = tmp_path / "sessions.json"
    legacy.write_text(
        json.dumps({"sessions": [{"id": "legacy", "messages": []}]}),
        encoding="utf-8",
    )
    before = legacy.read_bytes()

    state = store.load_state()

    assert state == {"sessions": {}, "groups": [], "active_session_id": None}
    assert legacy.read_bytes() == before


def test_session_and_messages_round_trip(store):
    session = session_record()
    store.append_message(session, message_record(1))
    session["msg_seq"] = 2
    session["updated_at"] = 30.0
    store.append_message(session, message_record(2, "assistant", "world"))

    loaded = store.load_state()

    assert loaded["sessions"]["sess-test"]["title"] == "Test chat"
    assert loaded["sessions"]["sess-test"]["messages"] == [
        {
            "id": 1,
            "role": "user",
            "content": "hello",
            "ts": 1.0,
            "image_ids": ["img-1"],
        },
        {
            "id": 2,
            "role": "assistant",
            "content": "world",
            "ts": 2.0,
            "image_ids": [],
        },
    ]


def test_messages_are_partitioned_and_sorted_by_session_and_message_id(store):
    first = session_record("sess-first")
    second = session_record("sess-second")
    store.append_message(first, message_record(2, content="second"))
    first["msg_seq"] = 1
    store.append_message(first, message_record(1, content="first"))
    store.append_message(second, message_record(1, content="other"))

    loaded = store.load_state()["sessions"]

    assert [m["content"] for m in loaded["sess-first"]["messages"]] == [
        "first",
        "second",
    ]
    assert [m["content"] for m in loaded["sess-second"]["messages"]] == ["other"]


def test_groups_and_active_session_round_trip(store):
    group = {
        "id": "group-work",
        "name": "Work",
        "created_at": 11.0,
        "position": 0,
    }
    session = session_record()
    session["group_id"] = group["id"]
    store.save_group(group)
    store.append_message(session, message_record())
    store.set_active_session(session["id"])

    loaded = store.load_state()

    assert loaded["groups"] == [group]
    assert loaded["sessions"][session["id"]]["group_id"] == group["id"]
    assert loaded["active_session_id"] == session["id"]


def test_delete_session_cascades_messages_but_preserves_other_sessions(store):
    first = session_record("sess-first")
    second = session_record("sess-second")
    store.append_message(first, message_record())
    store.append_message(second, message_record())

    store.delete_session("sess-first")

    loaded = store.load_state()
    assert "sess-first" not in loaded["sessions"]
    assert "sess-second" in loaded["sessions"]
    assert loaded["sessions"]["sess-second"]["messages"]


def test_delete_group_unassigns_sessions_in_one_store_operation(store):
    group = {"id": "group-work", "name": "Work", "created_at": 1.0, "position": 0}
    session = session_record()
    session["group_id"] = group["id"]
    store.save_group(group)
    store.append_message(session, message_record())

    store.delete_group_and_unassign_sessions(group["id"])

    loaded = store.load_state()
    assert loaded["groups"] == []
    assert loaded["sessions"][session["id"]]["group_id"] is None


def test_duplicate_message_id_is_rejected_without_overwriting_existing_row(store):
    session = session_record()
    store.append_message(session, message_record())

    with pytest.raises(sqlite3.IntegrityError):
        store.append_message(session, message_record(content="different"))

    assert store.load_state()["sessions"][session["id"]]["messages"][0]["content"] == "hello"


def test_persist_session_rolls_back_all_rows_on_duplicate_message(store):
    session = session_record()
    messages = [
        message_record(1, content="first"),
        message_record(1, content="duplicate"),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        store.persist_session(session, messages)

    assert store.load_state() == {
        "sessions": {},
        "groups": [],
        "active_session_id": None,
    }


def test_malformed_json_metadata_uses_safe_defaults(store, tmp_path):
    session = session_record()
    store.append_message(session, message_record())
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as conn:
        conn.execute(
            "UPDATE sessions SET token_usage_json = ?, sub_agents_json = ?",
            ("not-json", "[]"),
        )
        conn.commit()

    loaded = store.load_state()["sessions"][session["id"]]

    assert loaded["token_usage"] == {}
    assert loaded["sub_agents"] == []


def test_concurrent_message_appends_are_not_lost(store):
    import threading

    session = session_record()
    store.append_message(session, message_record(1))
    errors = []

    def append(message_id):
        try:
            store.append_message(session, message_record(message_id, content=str(message_id)))
        except Exception as exc:  # pragma: no cover - failure detail is asserted below
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(message_id,)) for message_id in (2, 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    messages = store.load_state()["sessions"][session["id"]]["messages"]
    assert [message["id"] for message in messages] == [1, 2, 3]
