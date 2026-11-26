"""Last user-activity marker shared by agents and autonomous mode."""

from __future__ import annotations

import os
import time


def activity_path(memory_dir: str) -> str:
    """Return the marker path, honouring the ZA_USER_ACTIVITY_FILE override."""
    override = os.environ.get("ZA_USER_ACTIVITY_FILE", "").strip()
    return override or os.path.join(memory_dir, "last_user_activity")


def record_user_activity(memory_dir: str) -> None:
    """Stamp the marker; failures must never break a task."""
    path = activity_path(memory_dir)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(str(time.time()))
    except OSError:
        pass


def last_activity_at(memory_dir: str) -> float | None:
    """Return the marker mtime, or None when it was never recorded."""
    try:
        return os.path.getmtime(activity_path(memory_dir))
    except OSError:
        return None
