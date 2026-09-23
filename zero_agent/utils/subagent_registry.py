"""Small append-only event log shared by desktop sessions and CLI subagents."""

from __future__ import annotations

import json
import os
from typing import Any


REGISTRY_ENV = "ZERO_AGENT_SUBAGENT_REGISTRY"
SESSION_ID_ENV = "ZERO_AGENT_DESKTOP_SESSION_ID"
PARENT_AGENT_ID_ENV = "ZERO_AGENT_PARENT_AGENT_ID"
EXIT_AFTER_ROUND_ENV = "ZERO_AGENT_TASK_EXIT_AFTER_ROUND"


def append_subagent_event(path: str, event: dict[str, Any]) -> bool:
    """Append one JSON event atomically enough for concurrent local workers."""
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def read_subagent_events(path: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSON lines added since ``offset`` and return the new offset."""
    try:
        with open(path, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if offset < 0 or offset > size:
                offset = 0
            stream.seek(offset)
            chunk = stream.read()
    except OSError:
        return [], offset

    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        return [], offset
    complete = chunk[: last_newline + 1]
    events: list[dict[str, Any]] = []
    for line in complete.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events, offset + len(complete)


def process_is_running(pid: Any) -> bool:
    """Return whether a local process id currently exists."""
    try:
        process_id = int(pid)
        if process_id <= 0:
            return False
        os.kill(process_id, 0)
        return True
    except PermissionError:
        return True
    except (TypeError, ValueError, ProcessLookupError, OSError):
        return False
