"""Tests for the idle-gated autonomous reflect module."""

from __future__ import annotations

import os
import time

from zero_agent.reflect import autonomous


def _setup(tmp_path, monkeypatch, idle_seconds: float):
    activity = tmp_path / "last_user_activity"
    state = tmp_path / "autonomous_state.json"
    activity.write_text("stamp", encoding="utf-8")
    stamp = time.time() - idle_seconds
    os.utime(activity, (stamp, stamp))
    monkeypatch.setenv("ZA_USER_ACTIVITY_FILE", str(activity))
    monkeypatch.setenv("ZA_AUTONOMOUS_STATE_FILE", str(state))
    return activity, state


def test_check_returns_prompt_after_long_idle(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, 31 * 60)

    prompt = autonomous.check()

    assert prompt is not None
    assert prompt.startswith("[AUTO]")


def test_check_skips_when_user_is_active(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, 5 * 60)

    assert autonomous.check() is None


def test_check_skips_without_activity_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ZA_USER_ACTIVITY_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("ZA_AUTONOMOUS_STATE_FILE", str(tmp_path / "state.json"))

    assert autonomous.check() is None


def test_check_honours_idle_threshold_override(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, 20 * 60)
    monkeypatch.setenv("ZA_AUTONOMOUS_IDLE_MINUTES", "5")

    assert autonomous.check() is not None


def test_prompt_reports_configured_idle_minutes(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, 6 * 60)
    monkeypatch.setenv("ZA_AUTONOMOUS_IDLE_MINUTES", "5")

    prompt = autonomous.check()

    assert prompt is not None
    assert "超过5分钟" in prompt


def test_on_done_suppresses_immediate_rerun(tmp_path, monkeypatch) -> None:
    activity, _state = _setup(tmp_path, monkeypatch, 31 * 60)
    assert autonomous.check() is not None

    autonomous.on_done(None)

    assert autonomous.check() is None
