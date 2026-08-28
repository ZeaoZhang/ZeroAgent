"""SQLite persistence for desktop sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_SCHEMA_VERSION = "1"
_UNSET = object()
_MESSAGE_COLUMNS = {"id", "role", "content", "ts"}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    cwd TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    msg_seq INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    terminal_status TEXT NOT NULL DEFAULT '',
    terminal_reason TEXT NOT NULL DEFAULT '',
    model_override TEXT,
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    group_id TEXT,
    sub_agents_json TEXT NOT NULL DEFAULT '[]',
    plan_path TEXT,
    plan_status TEXT NOT NULL DEFAULT 'inactive',
    plan_task TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, message_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp
    ON messages(session_id, timestamp, message_id);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SessionStore:
    """Transactional SQLite store independent of desktop bridge dataclasses."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(Path(path).expanduser().resolve())
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        """Create the database schema and reject unsupported versions."""
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with self._connect() as conn:
                meta_exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'schema_meta'
                    """
                ).fetchone()
                version_row = None
                if meta_exists:
                    version_row = conn.execute(
                        "SELECT value FROM schema_meta WHERE key = ?",
                        ("version",),
                    ).fetchone()
                    if version_row is not None and str(version_row["value"]) != _SCHEMA_VERSION:
                        raise RuntimeError(
                            "unsupported session store schema version: "
                            f"{version_row['value']}"
                        )
                conn.executescript(_SCHEMA)
                if version_row is None:
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                        ("version", _SCHEMA_VERSION),
                    )

    @staticmethod
    def _json_dump(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _json_load(value: Any, default: Any) -> Any:
        if not isinstance(value, str):
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @classmethod
    def _session_values(cls, session: dict) -> tuple[Any, ...]:
        return (
            str(session.get("id") or ""),
            str(session.get("title") or "New chat"),
            str(session.get("cwd") or ""),
            float(session.get("created_at") or time.time()),
            float(session.get("updated_at") or time.time()),
            int(session.get("msg_seq") or 0),
            str(session.get("last_error") or ""),
            str(session.get("terminal_status") or ""),
            str(session.get("terminal_reason") or ""),
            session.get("model_override"),
            cls._json_dump(session.get("token_usage", {})),
            session.get("group_id"),
            cls._json_dump(session.get("sub_agents", [])),
            session.get("plan_path"),
            str(session.get("plan_status") or "inactive"),
            str(session.get("plan_task") or ""),
        )

    @classmethod
    def _write_session(cls, conn: sqlite3.Connection, session: dict) -> None:
        values = cls._session_values(session)
        if not values[0]:
            raise ValueError("session id is required")
        conn.execute(
            """
            INSERT INTO sessions(
                id, title, cwd, created_at, updated_at, msg_seq,
                last_error, terminal_status, terminal_reason, model_override,
                token_usage_json, group_id, sub_agents_json,
                plan_path, plan_status, plan_task
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                cwd = excluded.cwd,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                msg_seq = excluded.msg_seq,
                last_error = excluded.last_error,
                terminal_status = excluded.terminal_status,
                terminal_reason = excluded.terminal_reason,
                model_override = excluded.model_override,
                token_usage_json = excluded.token_usage_json,
                group_id = excluded.group_id,
                sub_agents_json = excluded.sub_agents_json,
                plan_path = excluded.plan_path,
                plan_status = excluded.plan_status,
                plan_task = excluded.plan_task
            """,
            values,
        )

    @classmethod
    def _write_message(
        cls,
        conn: sqlite3.Connection,
        session_id: str,
        message: dict,
    ) -> None:
        metadata = {
            key: value for key, value in message.items() if key not in _MESSAGE_COLUMNS
        }
        conn.execute(
            """
            INSERT INTO messages(
                session_id, message_id, role, content, timestamp, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                int(message.get("id")),
                str(message.get("role") or ""),
                str(message.get("content") or ""),
                float(message.get("ts") or time.time()),
                cls._json_dump(metadata),
            ),
        )

    def append_message(
        self,
        session: dict,
        message: dict,
        *,
        active_session_id: str | None = None,
    ) -> None:
        """Atomically upsert session metadata, append one message, and active state."""
        session_id = str(session.get("id") or "")
        with self._lock:
            with self._connect() as conn:
                self._write_session(conn, session)
                self._write_message(conn, session_id, message)
                if active_session_id is not None:
                    conn.execute(
                        """
                        INSERT INTO app_state(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        ("active_session_id", str(active_session_id)),
                    )
    def upsert_session(self, session: dict) -> None:
        with self._lock:
            with self._connect() as conn:
                self._write_session(conn, session)

    def sync_sessions(
        self,
        sessions: list[dict],
        active_session_id: str | None,
    ) -> None:
        """Atomically synchronize session rows and active state."""
        session_ids = [str(session.get("id") or "") for session in sessions]
        if any(not session_id for session_id in session_ids):
            raise ValueError("session id is required")
        with self._lock:
            with self._connect() as conn:
                if session_ids:
                    placeholders = ", ".join("?" for _ in session_ids)
                    conn.execute(
                        f"DELETE FROM sessions WHERE id NOT IN ({placeholders})",
                        session_ids,
                    )
                else:
                    conn.execute("DELETE FROM sessions")
                for session in sessions:
                    self._write_session(conn, session)
                if active_session_id is None:
                    conn.execute(
                        "DELETE FROM app_state WHERE key = ?",
                        ("active_session_id",),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO app_state(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        ("active_session_id", str(active_session_id)),
                    )

    def persist_session(
        self,
        session: dict,
        messages: list[dict],
        *,
        active_session_id: str | None = None,
    ) -> None:
        """Atomically replace a session's complete message list."""
        session_id = str(session.get("id") or "")
        with self._lock:
            with self._connect() as conn:
                self._write_session(conn, session)
                conn.execute(
                    "DELETE FROM messages WHERE session_id = ?",
                    (session_id,),
                )
                for message in messages:
                    self._write_message(conn, session_id, message)
                if active_session_id is not None:
                    conn.execute(
                        """
                        INSERT INTO app_state(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        ("active_session_id", str(active_session_id)),
                    )

    def _load_groups_with_connection(self, conn: sqlite3.Connection) -> list[dict]:
        return [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "created_at": self._as_float(row["created_at"], time.time()),
                "position": self._as_int(row["position"], 0),
            }
            for row in conn.execute(
                "SELECT id, name, created_at, position FROM groups ORDER BY position, id"
            )
        ]

    @classmethod
    def _session_from_row(cls, row: sqlite3.Row) -> dict:
        return {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "cwd": str(row["cwd"]),
            "created_at": cls._as_float(row["created_at"], time.time()),
            "updated_at": cls._as_float(row["updated_at"], time.time()),
            "msg_seq": cls._as_int(row["msg_seq"], 0),
            "last_error": str(row["last_error"]),
            "terminal_status": str(row["terminal_status"]),
            "terminal_reason": str(row["terminal_reason"]),
            "model_override": row["model_override"],
            "token_usage": cls._json_load(row["token_usage_json"], {}),
            "group_id": row["group_id"],
            "sub_agents": cls._json_load(row["sub_agents_json"], []),
            "plan_path": row["plan_path"],
            "plan_status": str(row["plan_status"]),
            "plan_task": str(row["plan_task"]),
            "messages": [],
        }

    @classmethod
    def _message_from_row(cls, row: sqlite3.Row) -> dict:
        metadata = cls._json_load(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "id": int(row["message_id"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "ts": cls._as_float(row["timestamp"], time.time()),
            **metadata,
        }

    def load_groups(self) -> list[dict]:
        with self._lock:
            with self._connect() as conn:
                return self._load_groups_with_connection(conn)

    def load_state(self) -> dict:
        with self._lock:
            with self._connect() as conn:
                sessions: dict[str, dict] = {}
                for row in conn.execute(
                    """
                    SELECT id, title, cwd, created_at, updated_at, msg_seq,
                           last_error, terminal_status, terminal_reason,
                           model_override, token_usage_json, group_id,
                           sub_agents_json, plan_path, plan_status, plan_task
                    FROM sessions
                    ORDER BY updated_at DESC, id
                    """
                ):
                    session = self._session_from_row(row)
                    sessions[session["id"]] = session

                for row in conn.execute(
                    """
                    SELECT session_id, message_id, role, content, timestamp, metadata_json
                    FROM messages
                    ORDER BY session_id, message_id
                    """
                ):
                    session = sessions.get(str(row["session_id"]))
                    if session is not None:
                        try:
                            message = self._message_from_row(row)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        session["messages"].append(message)

                active_row = conn.execute(
                    "SELECT value FROM app_state WHERE key = ?",
                    ("active_session_id",),
                ).fetchone()
                active_id = active_row["value"] if active_row else None
                if active_id not in sessions:
                    active_id = next(iter(sessions), None)
                return {
                    "sessions": sessions,
                    "groups": self._load_groups_with_connection(conn),
                    "active_session_id": active_id,
                }

    def save_group(self, group: dict) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO groups(id, name, created_at, position)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        created_at = excluded.created_at,
                        position = excluded.position
                    """,
                    (
                        str(group.get("id") or ""),
                        str(group.get("name") or ""),
                        float(group.get("created_at") or time.time()),
                        int(group.get("position") or 0),
                    ),
                )

    def delete_group_and_unassign_sessions(
        self,
        group_id: str,
        updated_at_by_session: dict[str, float] | None = None,
    ) -> None:
        with self._lock:
            with self._connect() as conn:
                updates = updated_at_by_session or {}
                if updates:
                    for session_id, updated_at in updates.items():
                        conn.execute(
                            """
                            UPDATE sessions
                            SET group_id = NULL, updated_at = ?
                            WHERE id = ? AND group_id = ?
                            """,
                            (float(updated_at), session_id, group_id),
                        )
                else:
                    conn.execute(
                        "UPDATE sessions SET group_id = NULL WHERE group_id = ?",
                        (group_id,),
                    )
                conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    def delete_session(
        self,
        session_id: str,
        *,
        active_session_id: str | None | object = _UNSET,
    ) -> None:
        """Delete a session and optionally update active state atomically."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                if active_session_id is _UNSET:
                    conn.execute(
                        "DELETE FROM app_state WHERE key = ? AND value = ?",
                        ("active_session_id", session_id),
                    )
                elif active_session_id is None:
                    conn.execute(
                        "DELETE FROM app_state WHERE key = ?",
                        ("active_session_id",),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO app_state(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        ("active_session_id", str(active_session_id)),
                    )

    def set_active_session(self, session_id: str | None) -> None:
        with self._lock:
            with self._connect() as conn:
                if session_id is None:
                    conn.execute(
                        "DELETE FROM app_state WHERE key = ?",
                        ("active_session_id",),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO app_state(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        ("active_session_id", str(session_id)),
                    )

    def close(self) -> None:
        """Connections are scoped to operations; retained for manager lifecycle symmetry."""
        return None
