"""Scheduler tests for ZeroAgent schedule-mode semantics."""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime
from pathlib import Path


ZA_ROOT = Path(__file__).resolve().parents[1]


def _load_scheduler(monkeypatch, tmp_path):
    tasks = tmp_path / "sche_tasks"
    monkeypatch.setenv("ZA_SCHED_TASKS_DIR", str(tasks))
    monkeypatch.setenv("ZA_SCHED_LOCK_PORT", "0")
    path = ZA_ROOT / "zero_agent" / "reflect" / "scheduler.py"
    name = f"za_scheduler_alignment_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._l4_t = 1000
    monkeypatch.setattr(module._time, "time", lambda: 1000)
    # Never let a test touch the real session archive directories.
    monkeypatch.setattr(
        "zero_agent.memory.compress_session.batch_process",
        lambda *args, **kwargs: {"processed": 0, "retained_raw": []},
    )
    return module, tasks


def _freeze_now(monkeypatch, module, frozen: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return frozen

    monkeypatch.setattr(module, "datetime", FrozenDateTime)


def _write_task(tasks: Path, name: str, data: dict) -> None:
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )


def test_schedule_mode_daily_task_prompt_contract(
    monkeypatch,
    tmp_path,
) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, scheduler, datetime(2099, 1, 2, 9, 30))
    _write_task(tasks, "morning", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "检查日报",
    })

    prompt = scheduler.check()

    rpt = tasks / "done" / "2099-01-02_0930_morning.md"
    assert prompt == (
        "[定时任务] morning\n"
        f"[报告路径] {rpt}\n\n"
        "先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n"
        "检查日报\n\n"
        f"完成后将执行报告写入 {rpt}。"
    )


def test_schedule_mode_weekday_skips_weekend(monkeypatch, tmp_path) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, scheduler, datetime(2099, 1, 3, 9, 30))
    _write_task(tasks, "weekday", {
        "enabled": True,
        "repeat": "weekday",
        "schedule": "09:00",
        "prompt": "工作日任务",
    })

    assert scheduler.check() is None


def test_schedule_mode_skips_when_past_max_delay(monkeypatch, tmp_path) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, scheduler, datetime(2099, 1, 2, 18, 30))
    _write_task(tasks, "late", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "max_delay_hours": 1,
        "prompt": "过期任务",
    })

    assert scheduler.check() is None


def test_schedule_mode_cooldown_skips_recent_done_report(
    monkeypatch,
    tmp_path,
) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, scheduler, datetime(2099, 1, 2, 9, 30))
    _write_task(tasks, "cooldown", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "冷却任务",
    })
    done = tasks / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "2099-01-01_1800_cooldown.md").write_text("done", encoding="utf-8")

    assert scheduler.check() is None


def test_cron_mode_isolated_zeroagent_extension(monkeypatch, tmp_path) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, scheduler, datetime(2099, 1, 2, 9, 30))
    _write_task(tasks, "cronjob", {
        "enabled": True,
        "cron": "30 9 * * *",
        "prompt": "cron 扩展任务",
    })

    prompt = scheduler.check()

    assert prompt is not None
    assert "[定时任务] cronjob\n" in prompt
    assert "cron 扩展任务" in prompt


# ── 执行账本：running / retry / once / stale ──

START_TS = datetime(2099, 1, 2, 9, 30).timestamp()


def _clock(monkeypatch, module, start_ts: float) -> dict:
    """Drive both time.time() and datetime.now() from one mutable clock."""
    holder = {"ts": float(start_ts)}

    class ClockedDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime.fromtimestamp(holder["ts"])

    monkeypatch.setattr(module, "datetime", ClockedDateTime)
    monkeypatch.setattr(module._time, "time", lambda: holder["ts"])
    module._l4_t = holder["ts"]  # keep the 12h L4 cron out of the way
    return holder


def _read_state(tasks: Path, tid: str) -> dict:
    state = json.loads((tasks / ".state.json").read_text(encoding="utf-8"))
    return state["tasks"][tid]


def test_running_task_is_not_triggered_again_before_on_done(
    monkeypatch,
    tmp_path,
) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "longjob", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "长任务",
    })

    assert scheduler.check() is not None
    assert _read_state(tasks, "longjob")["last_status"] == "running"
    assert _read_state(tasks, "longjob")["attempts"] == 1
    assert scheduler.check() is None


def test_failed_task_waits_for_retry_backoff(monkeypatch, tmp_path) -> None:
    from zero_agent.core.types import TerminalEvent, TerminalStatus

    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    holder = _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "flaky", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "不稳定任务",
    })

    assert scheduler.check() is not None
    scheduler.on_done(TerminalEvent(status=TerminalStatus.FAILED, reason="boom"))
    entry = _read_state(tasks, "flaky")
    assert entry["last_status"] == "failed"
    assert entry["next_retry_at"] == START_TS + 600
    assert scheduler.check() is None

    holder["ts"] += 11 * 60
    assert scheduler.check() is not None
    assert _read_state(tasks, "flaky")["attempts"] == 2


def test_once_task_stops_after_success(monkeypatch, tmp_path) -> None:
    from zero_agent.core.types import TerminalEvent, TerminalStatus

    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    holder = _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "oneoff", {
        "enabled": True,
        "repeat": "once",
        "schedule": "09:00",
        "prompt": "一次性任务",
    })

    assert scheduler.check() is not None
    scheduler.on_done(TerminalEvent(status=TerminalStatus.COMPLETED, reason="done"))
    assert _read_state(tasks, "oneoff")["last_status"] == "success"
    assert scheduler.check() is None

    holder["ts"] += 2 * 3600
    assert scheduler.check() is None


def test_stale_running_task_is_recovered(monkeypatch, tmp_path) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "stale", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "timeout_minutes": 60,
        "prompt": "卡住的任务",
    })
    (tasks / ".state.json").write_text(
        json.dumps({
            "tasks": {
                "stale": {
                    "last_started_at": START_TS - 3 * 3600,
                    "last_finished_at": 0.0,
                    "last_status": "running",
                    "attempts": 1,
                    "next_retry_at": 0.0,
                },
            },
        }),
        encoding="utf-8",
    )

    assert scheduler.check() is None

    entry = _read_state(tasks, "stale")
    assert entry["last_status"] == "failed"
    assert entry["next_retry_at"] == START_TS + 600


def test_failed_task_retries_even_after_it_wrote_a_report(monkeypatch, tmp_path) -> None:
    """A report written by a failed attempt must not gate the ledger retry."""
    from zero_agent.core.types import TerminalEvent, TerminalStatus

    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    holder = _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "reports-then-fails", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "先写报告再失败",
    })

    assert scheduler.check() is not None
    scheduler.on_done(TerminalEvent(status=TerminalStatus.FAILED, reason="boom"))
    # The prompt tells the model to write the report path, so a failed attempt
    # often leaves a fresh done/*.md behind.
    date = datetime.fromtimestamp(START_TS)
    (tasks / "done").mkdir(parents=True, exist_ok=True)
    (tasks / "done" / f"{date.strftime('%Y-%m-%d_%H%M')}_reports-then-fails.md").write_text(
        "partial", encoding="utf-8"
    )

    holder["ts"] += 11 * 60

    assert scheduler.check() is not None


def test_legacy_state_file_is_migrated(monkeypatch, tmp_path) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "state.json").write_text(
        json.dumps({"tasks": {"old": {"last_status": "success"}}}),
        encoding="utf-8",
    )

    scheduler._save_state({"tasks": {}})

    assert (tasks / ".state.json").is_file()
    assert not (tasks / "state.json").exists()
    assert scheduler._load_state() == {"tasks": {}}


def test_task_named_state_is_not_deleted(monkeypatch, tmp_path) -> None:
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, scheduler, datetime(2099, 1, 2, 9, 30))
    _write_task(tasks, "state", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "状态任务",
    })

    scheduler._save_state({"tasks": {}})

    assert (tasks / "state.json").is_file()
    assert scheduler.check() is not None


def test_success_resets_the_retry_counter(monkeypatch, tmp_path) -> None:
    from zero_agent.core.types import TerminalEvent, TerminalStatus

    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    holder = _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "flaky-then-fine", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "先失败后成功",
    })
    scheduler._save_state({
        "tasks": {
            "flaky-then-fine": {
                "last_started_at": START_TS,
                "last_finished_at": 0.0,
                "last_status": "running",
                "attempts": 5,
                "next_retry_at": 0.0,
            },
        },
    })
    scheduler._active_task_id = "flaky-then-fine"

    scheduler.on_done(TerminalEvent(status=TerminalStatus.COMPLETED, reason="done"))

    assert _read_state(tasks, "flaky-then-fine")["attempts"] == 0

    # The next failure starts the streak again from the base delay.
    scheduler._active_task_id = "flaky-then-fine"
    scheduler.on_done(TerminalEvent(status=TerminalStatus.FAILED, reason="boom"))

    entry = _read_state(tasks, "flaky-then-fine")
    assert entry["attempts"] == 0
    assert entry["next_retry_at"] == START_TS + 600


def test_legacy_done_report_still_gates_ledgerless_tasks(monkeypatch, tmp_path) -> None:
    """Tasks that predate state.json keep their report-based cooldown."""
    scheduler, tasks = _load_scheduler(monkeypatch, tmp_path)
    _clock(monkeypatch, scheduler, START_TS)
    _write_task(tasks, "legacy", {
        "enabled": True,
        "repeat": "daily",
        "schedule": "09:00",
        "prompt": "旧任务",
    })
    date = datetime.fromtimestamp(START_TS)
    (tasks / "done").mkdir(parents=True, exist_ok=True)
    (tasks / "done" / f"{date.strftime('%Y-%m-%d_%H%M')}_legacy.md").write_text(
        "done", encoding="utf-8"
    )

    assert scheduler.check() is None
