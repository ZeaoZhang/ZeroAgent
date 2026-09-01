"""Tests for canonical bot configuration loading and safe persistence."""

from __future__ import annotations

import json
import stat

import pytest
import yaml

from zero_agent.bots.channel_config import (
    ChannelConfigError,
    channel_config_snapshot,
    load_keys,
    update_channel_config,
)


def test_load_keys_uses_za_config_path_when_bot_path_is_absent(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "bots:\n  tg_bot_token: file-token\n  tg_allowed_users: ['1001']\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ZA_BOT_CONFIG_PATH", raising=False)
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)

    assert load_keys() == {
        "tg_bot_token": "file-token",
        "tg_allowed_users": ["1001"],
    }


def test_environment_values_override_yaml_and_expand_exact_env_references(
    monkeypatch, tmp_path
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "bots:\n  tg_bot_token: ${TG_BOT_TOKEN}\n  tg_allowed_users: ['1001']\n"
        "  discord_bot_token: ${MISSING_DISCORD_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ZA_BOT_CONFIG_PATH", raising=False)
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))
    monkeypatch.setenv("TG_BOT_TOKEN", "env-token")

    keys = load_keys()

    assert keys["tg_bot_token"] == "env-token"
    assert keys["discord_bot_token"] == ""


def test_update_channel_config_preserves_other_config_and_masks_secrets(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_backend: default\nllm_backends: {}\nbots:\n  old_key: keep\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ZA_BOT_CONFIG_PATH", raising=False)
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)

    snapshot = update_channel_config(
        "telegram",
        {"tg_bot_token": "new-token", "tg_allowed_users": ["1001"]},
    )
    document = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert document["default_backend"] == "default"
    assert document["bots"]["old_key"] == "keep"
    assert document["bots"]["tg_bot_token"] == "new-token"
    assert snapshot["fields"][0]["configured"] is True
    assert "new-token" not in json.dumps(snapshot)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_unknown_channel_field_is_rejected_without_writing(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("bots: {}\n", encoding="utf-8")
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))

    with pytest.raises(ChannelConfigError):
        update_channel_config("telegram", {"not_a_field": "value"})

    assert config.read_text(encoding="utf-8") == "bots: {}\n"


def test_list_fields_reject_scalar_values(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("bots: {}\n", encoding="utf-8")
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))

    with pytest.raises(ChannelConfigError):
        update_channel_config("telegram", {"tg_allowed_users": "1001"})


def test_environment_managed_field_returns_conflict(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("bots: {}\n", encoding="utf-8")
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))
    monkeypatch.setenv("TG_BOT_TOKEN", "environment-token")

    with pytest.raises(ChannelConfigError, match="environment"):
        update_channel_config("telegram", {"tg_bot_token": "file-token"})


def test_snapshot_contains_metadata_only(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "bots:\n  tg_bot_token: file-token\n  tg_allowed_users: ['1001']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config))
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)

    snapshot = channel_config_snapshot("telegram")

    assert snapshot["channelId"] == "telegram"
    assert snapshot["configured"] is True
    assert [field["key"] for field in snapshot["fields"]] == [
        "tg_bot_token",
        "tg_allowed_users",
    ]
    assert all("value" not in field for field in snapshot["fields"])
    assert "file-token" not in json.dumps(snapshot)
