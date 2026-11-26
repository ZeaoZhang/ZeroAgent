"""Autonomous mode: only wake when the user has actually been idle."""

import json
import os
import time

INTERVAL = 1800
ONCE = False
POST_TASK_INTERVAL = 1800


def _memory_dir():
    from zero_agent.core.config import load_default_config
    return load_default_config().memory_dir


def _state_path():
    return os.environ.get("ZA_AUTONOMOUS_STATE_FILE", "").strip() or os.path.join(
        _memory_dir(), "autonomous_state.json"
    )


def _last_run():
    try:
        with open(_state_path(), encoding="utf-8") as fp:
            data = json.load(fp)
        return float(data.get("last_run_at") or 0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _write_last_run(now):
    path = _state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({"last_run_at": now}, fp, ensure_ascii=False)
    os.replace(tmp, path)


def check():
    from zero_agent.utils.activity import last_activity_at
    idle_minutes = int(os.environ.get("ZA_AUTONOMOUS_IDLE_MINUTES", "30"))
    activity = last_activity_at(_memory_dir())
    if activity is None:
        return None
    now = time.time()
    if now - activity < idle_minutes * 60:
        return None
    if now - _last_run() < 7200:
        return None
    return f"[AUTO]🤖 用户已经离开超过{idle_minutes}分钟，作为自主智能体，请阅读自动化sop，执行自动任务。"


def on_done(result):
    _write_last_run(time.time())
