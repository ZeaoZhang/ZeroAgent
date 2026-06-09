"""Tests for runners/cli.py — CLI config overrides."""

import argparse

import pytest

from zero_agent.core.exceptions import LLMError
from zero_agent.runners.cli import (
    _build_parser,
    _format_llm_error,
    _load_config,
    _parse_reflect_args,
)


def test_load_config_writes_max_turns_override(monkeypatch, tmp_path) -> None:
    """CLI --max-turns 必须写入 agent.config.max_turns."""
    monkeypatch.setenv("ZA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ZA_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("ZA_LLM_API_BASE", "https://api.openai.com/v1")
    monkeypatch.setenv("ZA_LLM_MODEL", "test-model")
    monkeypatch.setenv("ZA_MAX_TURNS", "80")

    config = _load_config(argparse.Namespace(
        config=None,
        model=None,
        workspace=str(tmp_path / "workspace"),
        verbose=False,
        quiet=False,
        max_turns=37,
    ))

    assert config.max_turns == 37


def test_parser_accepts_llm_no_and_reflect_args() -> None:
    args = _build_parser().parse_args([
        "--reflect", "zero_agent/reflect/goal_mode.py",
        "--llm-no", "1",
        "--reflect-arg", "goal_state=temp/goal.json",
    ])

    assert args.llm_no == 1
    assert args.reflect_arg == ["goal_state=temp/goal.json"]


def test_parse_reflect_args() -> None:
    parsed = _parse_reflect_args(["base_url=http://127.0.0.1:8000", "name=w1"])

    assert parsed == {"base_url": "http://127.0.0.1:8000", "name": "w1"}


def test_parse_reflect_args_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        _parse_reflect_args(["not-a-pair"])


def test_format_llm_error_removes_litellm_noise() -> None:
    err = LLMError(
        "LLM 调用失败 [default]: litellm.APIError: APIError: "
        "OpenAIException - Your request was blocked."
    )

    message = _format_llm_error(err)

    assert "Give Feedback" not in message
    assert "litellm.APIError" not in message
    assert "Your request was blocked." in message
    assert "服务端已拒绝该请求" in message
