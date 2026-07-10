"""Tests for zero_agent/frontends/commands/model_cmd.py."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from zero_agent.frontends.commands.model_cmd import (
    EFFORT_LEVELS,
    _fetch_model_list,
    _is_backend_name,
    current_model,
    handle,
    list_backends,
    set_effort,
    set_runtime_model,
    set_temperature,
    switch_model,
)


# ── helpers ─────────────────────────────────────────────────────────────


def _make_mock_agent(*, backends=None, current="default"):
    """Build a mock ZeroAgent with configurable backends."""
    agent = MagicMock()
    if backends is None:
        backends = [("default", "gpt-4o", True), ("claude", "claude-3-opus", False)]
    agent.list_backends.return_value = backends
    agent.get_llm_name.return_value = "gpt-4o"
    agent._sessions = {name: MagicMock() for name, _, _ in backends}
    agent.client = MagicMock()
    agent.client.config = MagicMock(
        model="gpt-4o",
        temperature=0.7,
        reasoning_effort="none",
    )

    def _switch(name):
        if name not in agent._sessions:
            raise ValueError(f"未知后端: {name}")
        agent.client.config.model = agent._sessions[name].config.model

    agent.switch_backend.side_effect = _switch
    return agent


# ── EFFORT_LEVELS ───────────────────────────────────────────────────────


def test_effort_levels_all_six():
    """EFFORT_LEVELS contains exactly the six valid reasoning-effort levels."""
    assert EFFORT_LEVELS == ["none", "minimal", "low", "medium", "high", "xhigh"]
    assert len(EFFORT_LEVELS) == 6
    for lvl in EFFORT_LEVELS:
        assert isinstance(lvl, str)


# ── list_backends ───────────────────────────────────────────────────────


def test_list_backends_returns_with_current_marked():
    """list_backends returns the agent's backend list including current flag."""
    backends = [
        ("default", "gpt-4o", True),
        ("claude", "claude-3-opus", False),
        ("gemini", "gemini-pro", False),
    ]
    agent = _make_mock_agent(backends=backends, current="default")
    result = list_backends(agent)
    assert result == backends
    assert result[0][2] is True
    assert result[1][2] is False
    assert result[2][2] is False


# ── current_model ───────────────────────────────────────────────────────


def test_current_model_returns_llm_name():
    """current_model delegates to agent.get_llm_name()."""
    agent = _make_mock_agent()
    assert current_model(agent) == "gpt-4o"


# ── switch_model ────────────────────────────────────────────────────────


def test_switch_model_valid_backend():
    """switch_model switches to a valid configured backend name."""
    agent = _make_mock_agent()
    result = switch_model(agent, "claude")
    agent.switch_backend.assert_called_once_with("claude")
    assert "claude" in result


def test_switch_model_invalid_backend_raises():
    """switch_model raises ValueError for an unconfigured backend name."""
    agent = _make_mock_agent()
    agent.switch_backend.side_effect = ValueError("未知后端: nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        switch_model(agent, "nonexistent")


# ── set_runtime_model ───────────────────────────────────────────────────


def test_set_runtime_model_changes_config_model():
    """set_runtime_model updates the model on the session config in memory."""
    agent = _make_mock_agent()
    result = set_runtime_model(agent, "gpt-4-turbo")
    assert agent.client.config.model == "gpt-4-turbo"
    assert "gpt-4-turbo" in result


# ── set_effort ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_set_effort_valid_levels(level):
    """set_effort accepts every valid reasoning-effort level."""
    agent = _make_mock_agent()
    result = set_effort(agent, level)
    assert agent.client.config.reasoning_effort == level
    assert "reasoning_effort" in result

@pytest.mark.parametrize("level", ["invalid", "", "x-high", "null", "extreme"])
def test_set_effort_rejects_invalid_levels(level):
    """set_effort returns an error message for invalid levels and does not mutate config."""
    agent = _make_mock_agent()
    agent.client.config.reasoning_effort = "none"
    result = set_effort(agent, level)
    assert "无效" in result or "无效的" in result
    assert agent.client.config.reasoning_effort == "none"


# ── set_temperature ─────────────────────────────────────────────────────


@pytest.mark.parametrize("temp", [0.0, 0.5, 1.0, 1.5, 2.0])
def test_set_temperature_valid_values(temp):
    """set_temperature sets both config.temperature and client.temperature."""
    agent = _make_mock_agent()
    result = set_temperature(agent, temp)
    assert agent.client.config.temperature == temp
    assert agent.client.temperature == temp
    assert "temperature" in result


def test_set_temperature_handles_missing_runtime_attr():
    """set_temperature gracefully skips when client has no .temperature attr."""
    agent = _make_mock_agent()
    type(agent.client).temperature = PropertyMock(
        side_effect=AttributeError("no temperature")
    )
    result = set_temperature(agent, 1.5)
    assert agent.client.config.temperature == 1.5
    assert "temperature" in result


# ── _is_backend_name ────────────────────────────────────────────────────


def test_is_backend_name_true_and_false():
    """_is_backend_name checks agent._sessions membership."""
    agent = _make_mock_agent()
    assert _is_backend_name(agent, "default") is True
    assert _is_backend_name(agent, "claude") is True
    assert _is_backend_name(agent, "nonexistent") is False


# ── handle — no args ────────────────────────────────────────────────────


def test_handle_no_args_lists_backends():
    """handle with no args returns a formatted backend listing."""
    agent = _make_mock_agent()
    result = handle("", agent)
    assert "配置的后端:" in result
    assert "default" in result
    assert "claude" in result


# ── handle — switch by backend name ─────────────────────────────────────


def test_handle_backend_name_switches():
    """handle with a known backend name delegates to switch_model."""
    agent = _make_mock_agent()
    result = handle("claude", agent)
    agent.switch_backend.assert_called_once_with("claude")
    assert "claude" in result


def test_handle_unknown_arg_is_not_backend_or_subcommand():
    """handle with an unknown arg returns a help-style error message."""
    agent = _make_mock_agent()
    result = handle("foobar", agent)
    assert "未知" in result
    assert "foobar" in result


# ── handle — set subcommand ─────────────────────────────────────────────


def test_handle_set_model():
    """handle 'set <model_id>' delegates to set_runtime_model."""
    agent = _make_mock_agent()
    result = handle("set gpt-4-turbo", agent)
    assert agent.client.config.model == "gpt-4-turbo"
    assert "gpt-4-turbo" in result


def test_handle_set_no_args():
    """handle 'set' without a model_id returns usage message."""
    agent = _make_mock_agent()
    result = handle("set", agent)
    assert "用法" in result


# ── handle — effort subcommand ──────────────────────────────────────────


def test_handle_effort_valid():
    """handle 'effort high' delegates to set_effort."""
    agent = _make_mock_agent()
    result = handle("effort high", agent)
    assert agent.client.config.reasoning_effort == "high"
    assert "reasoning_effort" in result


def test_handle_effort_no_level():
    """handle 'effort' without a level returns usage with valid levels."""
    agent = _make_mock_agent()
    result = handle("effort", agent)
    assert "用法" in result
    assert "none" in result


# ── handle — temp subcommand ────────────────────────────────────────────


def test_handle_temp_valid():
    """handle 'temp 0.3' delegates to set_temperature."""
    agent = _make_mock_agent()
    result = handle("temp 0.3", agent)
    assert agent.client.config.temperature == 0.3
    assert "temperature" in result


def test_handle_temp_non_numeric():
    """handle 'temp abc' returns an invalid-value error."""
    agent = _make_mock_agent()
    result = handle("temp abc", agent)
    assert "无效" in result


def test_handle_temp_no_value():
    """handle 'temp' without a value returns usage message."""
    agent = _make_mock_agent()
    result = handle("temp", agent)
    assert "用法" in result


# ── handle — list subcommand ────────────────────────────────────────────


def test_handle_list_fetches_remote_models():
    """handle 'list' delegates to _fetch_model_list with agent.client."""
    agent = _make_mock_agent()
    with patch(
        "zero_agent.frontends.commands.model_cmd._fetch_model_list",
        return_value="2 models found",
    ):
        result = handle("list", agent)
    assert "2 models found" == result


# ── handle — backend named "set" ────────────────────────────────────────


def test_handle_backend_named_set_still_works():
    """A backend literally named 'set' switches rather than parsing as sub-command."""
    backends = [("set", "gpt-4o", True), ("claude", "claude-opus", False)]
    agent = _make_mock_agent(backends=backends)
    result = handle("set", agent)
    agent.switch_backend.assert_called_once_with("set")
    assert "set" in result
