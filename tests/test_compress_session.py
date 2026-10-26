"""Tests for L4 session compression."""

from __future__ import annotations

import os
import time
from pathlib import Path

from zero_agent.memory import compress_session as compress_session_module
from zero_agent.memory.compress_session import batch_process


def _session_log() -> str:
    payload = "important user context " * 300
    return (
        "=== Prompt === 2026-04-03 20:13:06\n"
        "system prompt that should be skipped\n"
        "=== USER ===\n"
        "<history>\n"
        "[USER] hello\n"
        "[Agent] hi\n"
        "</history>\n"
        f"{payload}\n"
        "=== ASSISTANT ===\n"
        "assistant echo that should be skipped\n"
        "=== Response === 2026-04-03 20:14:06\n"
        "final answer\n"
    )


def test_batch_process_discovers_model_response_logs(tmp_path) -> None:
    raw_dir = tmp_path / "sessions"
    l4_dir = tmp_path / "memory" / "L4_raw_sessions"
    raw_dir.mkdir()
    raw = raw_dir / "model_responses_123.txt"
    raw.write_text(_session_log(), encoding="utf-8")
    old = time.time() - 8000
    os.utime(raw, (old, old))

    result = batch_process(str(raw_dir), str(l4_dir), dry_run=True)

    assert result["processed"] == 1
    assert result["new_sessions"] == 1
    assert result["sessions"] == ["0403_2013-0403_2013"]


def test_batch_process_archives_history_and_removes_raw_log(tmp_path) -> None:
    raw_dir = tmp_path / "sessions"
    l4_dir = tmp_path / "memory" / "L4_raw_sessions"
    raw_dir.mkdir()
    raw = raw_dir / "model_responses_123.txt"
    raw.write_text(_session_log(), encoding="utf-8")
    old = time.time() - 8000
    os.utime(raw, (old, old))

    result = batch_process(str(raw_dir), str(l4_dir), dry_run=False)

    assert result["processed"] == 1
    assert result["new_sessions"] == 1
    assert result["deleted_raw"] == 1
    assert not raw.exists()
    assert (l4_dir / "2026-04.zip").is_file()
    histories = (l4_dir / "all_histories.txt").read_text(encoding="utf-8")
    assert "[USER] hello" in histories


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    repo_root = Path(compress_session_module.__file__).resolve().parents[2]

    assert Path(compress_session_module.L4_DIR) == repo_root / "memory" / "L4_raw_sessions"
    assert Path(compress_session_module.RAW_DIR) == repo_root / "workspace" / "sessions"


def test_batch_archive_uses_shared_configured_session_and_memory_roots(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace
    import zero_agent.core.config as config_module

    raw_dir = tmp_path / "custom sessions"
    memory_dir = tmp_path / "custom memory"
    raw_dir.mkdir()
    monkeypatch.delenv("ZA_MODEL_RESPONSES_DIR", raising=False)
    monkeypatch.delenv("ZA_L4_DIR", raising=False)
    monkeypatch.setattr(
        config_module,
        "load_default_config",
        lambda: SimpleNamespace(sessions_dir=str(raw_dir), memory_dir=str(memory_dir)),
    )

    result = batch_process(dry_run=True)

    assert result["processed"] == 0
    assert (memory_dir / "L4_raw_sessions").is_dir()
