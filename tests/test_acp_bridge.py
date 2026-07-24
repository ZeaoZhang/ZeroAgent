"""Tests for Phase 7 — frontend entry points and imports."""

import queue

import pytest


def test_packaging_acp_bridge_import():
    """ACP bridge module imports (when available)."""
    import importlib.util

    spec = importlib.util.find_spec("zero_agent.frontends.acp_bridge")
    if spec is None:
        pytest.skip("acp_bridge module not yet created")
    import zero_agent.frontends.acp_bridge  # noqa: F401


def test_packaging_stapp_import():
    """Streamlit stapp module imports."""
    import zero_agent.frontends.stapp  # noqa: F401


def test_packaging_tui_import():
    """TUI module imports."""
    import zero_agent.frontends.tui  # noqa: F401


def test_packaging_conductor_import():
    """Conductor module imports."""
    import zero_agent.frontends.conductor  # noqa: F401


def test_tui_main_without_textual_prints_hint(monkeypatch, capfd):
    """Textual 不可用时，tui.main() 应打印安装提示并以 1 退出。"""
    import zero_agent.frontends.tui as tui

    monkeypatch.setattr(tui, "_TEXTUAL_AVAILABLE", False)

    with pytest.raises(SystemExit) as exc:
        tui.main()

    assert exc.value.code == 1
    captured = capfd.readouterr()
    assert "Install zero-agent[ui]" in captured.out


def test_conductor_main_help_does_not_crash():
    """conductor.py main() with --help should not crash."""
    import sys
    from zero_agent.frontends.conductor import main

    # Use --help which exits 0
    sys.argv = ["conductor", "--help"]
    try:
        main()
    except SystemExit as e:
        assert e.code == 0




def _terminal(status: str, reason: str, text: str = "", data=None) -> dict:
    return {
        "type": "terminal",
        "status": status,
        "reason": reason,
        "text": text,
        "data": data,
        "turn": 1,
        "source": "agent",
        "certificate": None,
    }


def _drain_messages(*items: dict) -> tuple[str, list[dict]]:
    from zero_agent.frontends import acp_bridge
    bridge = object.__new__(acp_bridge.ZeroAgentAcpBridge)
    messages: list[dict] = []
    bridge.write_message = messages.append
    session = acp_bridge.SessionState("session-1", "/tmp", agent=None)
    output = queue.Queue()
    for item in items:
        output.put(item)
    return bridge._drain_agent_queue(session, output), messages


def test_acp_completed_stream_closes_with_end_turn() -> None:
    stop_reason, messages = _drain_messages(
        {"type": "chunk", "text": "Hello", "source": "agent", "turn": 1},
        _terminal("completed", "completion_certificate", "Hello"),
    )

    assert stop_reason == "end_turn"
    assert messages[0]["params"]["update"]["content"] == {"type": "text", "text": "Hello"}
    assert all("terminalStatus" not in message["params"]["update"] for message in messages)


def test_acp_cancelled_uses_cancelled_stop_reason() -> None:
    stop_reason, messages = _drain_messages(
        _terminal("cancelled", "user_cancelled", "partial"),
    )

    assert stop_reason == "cancelled"
    assert messages == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("waiting", "human_intervention"),
        ("failed", "RuntimeError"),
        ("budget_exhausted", "max_turns"),
        ("protocol_error", "invalid_step_outcome"),
    ],
)
def test_acp_unsupported_terminal_status_emits_structured_fallback(
    status: str,
    reason: str,
) -> None:
    stop_reason, messages = _drain_messages(_terminal(status, reason, "terminal text"))

    assert stop_reason == "end_turn"
    update = messages[-1]["params"]["update"]
    assert update["terminalStatus"] == status
    assert update["reason"] == reason
    assert update["content"] == {"type": "text", "text": "terminal text"}
