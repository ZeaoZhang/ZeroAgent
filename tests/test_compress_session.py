"""Tests for L4 session compression."""

from __future__ import annotations

import os
import time

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
    assert "SESSION: 0403_2013-0403_2013" in histories
    assert "[USER] hello" in histories
