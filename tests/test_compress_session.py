"""Tests for L4 session compression."""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

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


def test_batch_process_retains_unarchived_raw_log(tmp_path) -> None:
    raw_dir = tmp_path / "sessions"
    l4_dir = tmp_path / "memory" / "L4_raw_sessions"
    raw_dir.mkdir()
    raw = raw_dir / "model_responses_10001.txt"
    raw.write_text("=== USER ===\nhello with no timestamps\n", encoding="utf-8")
    old = time.time() - 10800
    os.utime(raw, (old, old))

    result = batch_process(str(raw_dir), str(l4_dir), dry_run=False)

    assert raw.exists()
    assert result["processed"] == 0
    assert result["deleted_raw"] == 0
    assert "model_responses_10001.txt" in result["retained_raw"]
    assert not list(l4_dir.glob("*.zip"))


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    repo_root = Path(compress_session_module.__file__).resolve().parents[2]

    assert Path(compress_session_module.L4_DIR) == repo_root / "memory" / "L4_raw_sessions"
    assert Path(compress_session_module.RAW_DIR) == repo_root / "workspace" / "sessions"


def _windowed_log(marker: str, start: str = "2026-04-03 20:13:06",
                  end: str = "2026-04-03 20:14:06") -> str:
    """Log with an explicit time window so tests can force name collisions."""
    payload = f"{marker} " * 2000
    return (
        f"=== Prompt === {start}\n"
        "system prompt that should be skipped\n"
        "=== USER ===\n"
        "<history>\n"
        "[USER] hello\n"
        "[Agent] hi\n"
        "</history>\n"
        f"{payload}\n"
        "=== ASSISTANT ===\n"
        "assistant echo that should be skipped\n"
        f"=== Response === {end}\n"
        "final answer\n"
    )


def test_batch_process_keeps_colliding_sessions_apart(tmp_path) -> None:
    """Two logs covering the same minute must not overwrite each other."""
    raw_dir = tmp_path / "sessions"
    l4_dir = tmp_path / "memory" / "L4_raw_sessions"
    raw_dir.mkdir()
    first = raw_dir / "model_responses_10001.txt"
    second = raw_dir / "model_responses_10002.txt"
    first.write_text(_windowed_log("ALPHA"), encoding="utf-8")
    second.write_text(_windowed_log("BRAVO"), encoding="utf-8")
    old = time.time() - 8000
    os.utime(first, (old, old))
    os.utime(second, (old, old))

    result = batch_process(str(raw_dir), str(l4_dir), dry_run=False)

    assert result["processed"] == 1
    assert result["deleted_raw"] == 1
    assert not first.exists()
    # The losing log keeps its raw file: nothing archived its content.
    assert second.exists()
    assert "model_responses_10002.txt" in result["retained_raw"]

    with zipfile.ZipFile(l4_dir / "2026-04.zip") as archive:
        names = archive.namelist()
        assert names == ["0403_2013-0403_2013.txt"]
        archived = archive.read(names[0]).decode("utf-8")
    assert "ALPHA" in archived
    assert "BRAVO" not in archived


def test_batch_process_skips_sessions_already_inside_a_zip(tmp_path) -> None:
    """A session that only exists inside a monthly zip is still archived."""
    raw_dir = tmp_path / "sessions"
    l4_dir = tmp_path / "memory" / "L4_raw_sessions"
    raw_dir.mkdir()
    l4_dir.mkdir(parents=True)
    raw = raw_dir / "model_responses_10003.txt"
    raw.write_text(_windowed_log("CHARLIE"), encoding="utf-8")
    old = time.time() - 8000
    os.utime(raw, (old, old))
    with zipfile.ZipFile(l4_dir / "2026-04.zip", "w") as archive:
        archive.writestr("0403_2013-0403_2013.txt", "already archived")

    result = batch_process(str(raw_dir), str(l4_dir), dry_run=False)

    assert result["processed"] == 0
    assert result["deleted_raw"] == 0
    assert raw.exists()
    with zipfile.ZipFile(l4_dir / "2026-04.zip") as archive:
        assert archive.namelist() == ["0403_2013-0403_2013.txt"]


def test_archive_failure_keeps_every_raw_log(tmp_path, monkeypatch) -> None:
    """A failed archive step must not authorise deleting raw logs."""
    raw_dir = tmp_path / "sessions"
    l4_dir = tmp_path / "memory" / "L4_raw_sessions"
    raw_dir.mkdir()
    raw = raw_dir / "model_responses_10004.txt"
    raw.write_text(_windowed_log("DELTA"), encoding="utf-8")
    old = time.time() - 8000
    os.utime(raw, (old, old))

    class FailingZip:
        def __init__(self, *args, **kwargs):
            raise OSError("disk full")

    monkeypatch.setattr(compress_session_module.zipfile, "ZipFile", FailingZip)

    with pytest.raises(OSError):
        batch_process(str(raw_dir), str(l4_dir), dry_run=False)

    assert raw.exists()
