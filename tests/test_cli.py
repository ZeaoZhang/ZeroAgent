"""Tests for runners/cli.py — CLI config overrides."""

import argparse

from zero_agent.runners.cli import _load_config


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
