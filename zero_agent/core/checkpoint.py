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
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from zero_agent.core.types import EvidenceLedger, EvidenceRecord, TaskContract, TaskMode

_logger = logging.getLogger("zero_agent.core.checkpoint")

CHECKPOINT_VERSION = 1

# 自动续跑的重启预算：同一任务在窗口内最多自动重启 N 次。此处的常量是两个
# 前端（CLI / 桌面 bridge）唯一的判定来源，避免各自的窗口与阈值漂移。
RESTART_BUDGET_WINDOW = 600.0
RESTART_BUDGET_LIMIT = 5

# 热重启冷却：进程刚重启后的这个窗口内不再因核心代码变化重启。agent 在任务
# 中途编辑 zero_agent/**/*.py 时，签名每个 turn 边界都会变；没有冷却就会在
# 几十秒内连续 abort + 重启，把正在流式输出的回复切成碎片。
RESTART_COOLDOWN = 45.0

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


def recent_restarts(
    data: dict[str, Any],
    *,
    now: Optional[float] = None,
) -> list[float]:
    """Return the restart timestamps inside the budget window.

    Args:
        data: 检查点负载.
        now: 参考时刻，默认当前时间.

    Returns:
        窗口内的时间戳列表（升序，忽略非数值条目）.
    """

    current = time.time() if now is None else now
    stamps = [
        float(item)
        for item in data.get("restart_history") or []
        if isinstance(item, (int, float))
        and current - float(item) < RESTART_BUDGET_WINDOW
    ]
    return sorted(stamps)


def restart_budget_exhausted(
    data: dict[str, Any],
    *,
    now: Optional[float] = None,
) -> bool:
    """Return whether a checkpoint must stop auto-resuming.

    只有 ``running`` 的检查点会自动续跑，因此预算也只约束它们；``waiting``
    的检查点在等用户答复，不计入重启循环。

    Args:
        data: 检查点负载.
        now: 参考时刻，默认当前时间.

    Returns:
        True 表示窗口内已达到重启上限.
    """

    if data.get("status") != "running":
        return False
    return len(recent_restarts(data, now=now)) >= RESTART_BUDGET_LIMIT


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


def normalize_history(history: Any) -> list[dict[str, Any]]:
    """Serialize one history list losslessly, dropping what cannot be.

    写检查点时用它落盘，恢复时用它清洗来路不明的文件内容。不能用
    ``default=str`` 兜底：多模态内容（``content`` 是 block 列表）会被字符串化，
    回灌给模型接口后直接报错。这里逐条做 JSON 往返，只有非 dict 条目或无法
    序列化的条目才被丢弃。

    Args:
        history: ``client.history`` 原始列表.

    Returns:
        可 JSON 往返的消息列表.
    """

    if not isinstance(history, list):
        return []
    payload: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            _logger.warning("检查点跳过了非字典历史条目: %r", type(item).__name__)
            continue
        try:
            encoded = json.dumps(item, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            _logger.warning("检查点跳过了无法序列化的历史条目: %s", exc)
            continue
        payload.append(json.loads(encoded))
    return payload


def merge_pending_messages(
    history: list[dict[str, Any]],
    pending_messages: Any,
) -> list[dict[str, Any]]:
    """Append the messages that were in flight but not yet in history.

    只补「尚未落进 history 的尾部」：若整段 pending 已经在 history 末尾（进程
    在请求发出后、写检查点前崩溃）就什么都不加；若只有前几条已入历史，就按最长
    重叠补剩下的，避免把整段对话重复一遍。

    Args:
        history: 检查点里的 history.
        pending_messages: 检查点里的 pending_messages.

    Returns:
        合并后的 history（原地修改并返回）.
    """

    pending = [item for item in (pending_messages or []) if isinstance(item, dict)]
    if not pending:
        return history
    overlap = 0
    upper = min(len(pending), len(history))
    for size in range(upper, 0, -1):
        if history[-size:] == pending[:size]:
            overlap = size
            break
    history.extend(pending[overlap:])
    return history
