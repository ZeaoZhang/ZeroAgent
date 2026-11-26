"""桌面 bridge 的持久化日志：stdout/stderr/bridge-log 落盘."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zero_agent.frontends import desktop_bridge


@pytest.fixture(autouse=True)
def _isolated_bridge_logging() -> None:
    """每个用例独立：先清空全局句柄，结束后恢复流并关闭文件."""
    saved_out, saved_err = sys.stdout, sys.stderr
    saved_hook = sys.excepthook
    desktop_bridge._BRIDGE_LOG_HANDLE = None
    yield
    handle = desktop_bridge._BRIDGE_LOG_HANDLE
    desktop_bridge._BRIDGE_LOG_HANDLE = None
    sys.stdout, sys.stderr = saved_out, saved_err
    sys.excepthook = saved_hook
    if handle is not None:
        handle.close()


def test_configure_bridge_logging_mirrors_stdout_and_stderr(tmp_path: Path) -> None:
    desktop_bridge._BRIDGE_LOG_HANDLE = None
    path = desktop_bridge.configure_bridge_logging(str(tmp_path))

    assert path == str(tmp_path / "desktop_bridge.log")
    print("stdout-line")
    print("stderr-line", file=sys.stderr)
    desktop_bridge.log_bridge_event("hot restart: core_changed")

    text = Path(path).read_text(encoding="utf-8")
    assert "bridge start" in text
    assert "stdout-line" in text
    assert "stderr-line" in text
    assert "[bridge-log] hot restart: core_changed" in text


def test_uncaught_exception_is_written_to_the_log(tmp_path: Path) -> None:
    desktop_bridge._BRIDGE_LOG_HANDLE = None
    path = desktop_bridge.configure_bridge_logging(str(tmp_path))

    try:
        raise RuntimeError("boom-in-turn")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    text = Path(path).read_text(encoding="utf-8")
    assert "RuntimeError: boom-in-turn" in text


def test_ws_hub_emit_persists_bridge_log(tmp_path: Path) -> None:
    desktop_bridge._BRIDGE_LOG_HANDLE = None
    path = desktop_bridge.configure_bridge_logging(str(tmp_path))
    hub = desktop_bridge.WsHub()

    hub.emit({"type": "bridge-log", "payload": "llm failure: 502"})

    assert "[bridge-log] llm failure: 502" in Path(path).read_text(encoding="utf-8")
