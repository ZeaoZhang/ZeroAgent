"""Scheduler compatibility tests for GenericAgent schedule-mode semantics."""

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


def test_schedule_mode_daily_task_prompt_matches_ga_contract(
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

