"""热重启检查点：序列化 / 恢复当前任务的运行状态.

检查点只在两个时机写盘：核心代码热重启前，以及任务进入 ``WAITING`` 时。
进程崩溃不保证自动续跑；这里的目标是「重启后从上一个 turn 边界继续」。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from zero_agent.core.types import EvidenceLedger, EvidenceRecord, TaskContract, TaskMode

_logger = logging.getLogger("zero_agent.core.checkpoint")

CHECKPOINT_VERSION = 1

CONTINUATION_PROMPT = (
    "[System] 进程因核心代码热重启而重启。请基于已有对话、工具结果与证据继续原任务；"
    "不要重做已完成或有副作用的调用；先确认当前状态再继续。"
)

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitized_session_id(session_id: str) -> str:
    """Return a filesystem-safe session id fragment."""

    return _SANITIZE_RE.sub("_", str(session_id or ""))[:100] or "session"


def checkpoint_dir(sessions_dir: str | os.PathLike[str]) -> Path:
    """Return the checkpoint directory for one sessions directory."""

    return Path(sessions_dir).expanduser().resolve() / "checkpoints"


def checkpoint_path(
    sessions_dir: str | os.PathLike[str],
    session_id: str,
) -> Path:
    """Return the checkpoint file path for one session."""

    return checkpoint_dir(sessions_dir) / f"{_sanitized_session_id(session_id)}.json"


def save(
    sessions_dir: str | os.PathLike[str],
    session_id: str,
    payload: dict[str, Any],
) -> Path:
    """Atomically persist one checkpoint payload.

    Args:
        sessions_dir: 会话目录.
        session_id: 会话标识，用于文件名.
        payload: 可 JSON 序列化的检查点数据.

    Returns:
        写入后的检查点路径.
    """
    directory = checkpoint_dir(sessions_dir)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    target = checkpoint_path(sessions_dir, session_id)
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{target.stem}-",
        suffix=".tmp",
        dir=str(directory),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise
    return target


def load(path: str | os.PathLike[str]) -> Optional[dict[str, Any]]:
    """Load one checkpoint file, returning None for missing/corrupt data."""

    target = Path(path)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("检查点读取失败，已忽略 %s: %s", target, exc)
        return None
    if not isinstance(data, dict):
        _logger.warning("检查点格式非法，已忽略 %s", target)
        return None
    if data.get("version") != CHECKPOINT_VERSION or not data.get("session_id"):
        _logger.warning("检查点版本或会话标识不匹配，已忽略 %s", target)
        return None
    return data


def list_checkpoints(
    sessions_dir: str | os.PathLike[str],
) -> list[tuple[Path, dict[str, Any]]]:
    """Return every readable checkpoint under one sessions directory."""

    directory = checkpoint_dir(sessions_dir)
    if not directory.is_dir():
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        data = load(path)
        if data is not None:
            found.append((path, data))
    return found


def clear(sessions_dir: str | os.PathLike[str], session_id: str) -> None:
    """Delete one session's checkpoint; missing files are ignored."""

    path = checkpoint_path(sessions_dir, session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _logger.warning("检查点删除失败 %s: %s", path, exc)


def contract_to_payload(contract: Any) -> Optional[dict[str, Any]]:
    """Serialize a ``TaskContract`` for checkpoint storage."""

    if not isinstance(contract, TaskContract):
        return None
    return {
        "task_id": contract.task_id,
        "user_request": contract.user_request,
        "mode": contract.mode.value,
        "plan_path": contract.plan_path,
    }


def contract_from_payload(data: Any) -> Optional[TaskContract]:
    """Rebuild a ``TaskContract`` from checkpoint data."""

    if not isinstance(data, dict) or not data.get("task_id"):
        return None
    try:
        mode = TaskMode(str(data.get("mode") or TaskMode.OPEN.value))
    except ValueError:
        mode = TaskMode.OPEN
    return TaskContract(
        task_id=str(data["task_id"]),
        user_request=str(data.get("user_request") or ""),
        mode=mode,
        plan_path=data.get("plan_path"),
    )


def ledger_to_payload(ledger: Any) -> dict[str, Any]:
    """Serialize an ``EvidenceLedger`` (records use ``dataclasses.asdict``)."""

    records = getattr(ledger, "records", None)
    if not isinstance(records, list):
        return {"records": []}
    return {"records": [asdict(record) for record in records]}


def ledger_from_payload(data: Any) -> EvidenceLedger:
    """Rebuild an ``EvidenceLedger``; malformed records are skipped."""

    records: list[EvidenceRecord] = []
    raw_records = data.get("records") if isinstance(data, dict) else None
    for raw in raw_records or []:
        if not isinstance(raw, dict):
            continue
        try:
            records.append(EvidenceRecord(**raw))
        except TypeError:
            continue
    return EvidenceLedger(records=records)
