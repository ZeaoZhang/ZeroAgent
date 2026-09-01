"""Shared channel definitions and App-link state for bot frontends."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_LOCK = threading.RLock()


@dataclass(frozen=True)
class ChannelDefinition:
    """Static metadata for one supported bot frontend."""

    id: str
    label: str
    source: str
    module: str
    required_keys: tuple[str, ...]


_CHANNELS = (
    ChannelDefinition("wechat", "微信", "wechat", "wechat_app.py", ()),
    ChannelDefinition(
        "wecom",
        "企业微信",
        "wecom",
        "wecom_app.py",
        ("wecom_bot_id", "wecom_secret"),
    ),
    ChannelDefinition(
        "dingtalk",
        "钉钉",
        "dingtalk",
        "dingtalk_app.py",
        ("dingtalk_client_id", "dingtalk_client_secret"),
    ),
    ChannelDefinition(
        "qq",
        "QQ",
        "qq",
        "qq_app.py",
        ("qq_app_id", "qq_app_secret"),
    ),
    ChannelDefinition(
        "feishu",
        "飞书",
        "feishu",
        "feishu_app.py",
        ("fs_app_id", "fs_app_secret"),
    ),
    ChannelDefinition(
        "telegram",
        "Telegram",
        "telegram",
        "telegram_app.py",
        ("tg_bot_token",),
    ),
    ChannelDefinition(
        "discord",
        "Discord",
        "discord",
        "discord_app.py",
        ("discord_bot_token",),
    ),
)
_CHANNELS_BY_ID = {definition.id: definition for definition in _CHANNELS}


def channel_definitions() -> tuple[ChannelDefinition, ...]:
    """Return the fixed list of bot channels in UI order."""

    return _CHANNELS


def channel_definition(channel_id: str) -> ChannelDefinition:
    """Return a channel definition or raise ``KeyError`` for unknown IDs."""

    key = str(channel_id or "").strip()
    try:
        return _CHANNELS_BY_ID[key]
    except KeyError as exc:
        raise KeyError(f"unknown channel: {key or '<empty>'}") from exc


def channel_settings_path() -> Path:
    """Return the shared JSON path used by the bridge and bot processes."""
    configured = os.environ.get("ZA_CHANNEL_SETTINGS_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else _PROJECT_ROOT / path
    return _PROJECT_ROOT / "temp" / "channel_settings.json"


def _read_raw_settings() -> dict:
    path = channel_settings_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalise_settings(data: Mapping) -> dict[str, dict[str, bool]]:
    settings: dict[str, dict[str, bool]] = {}
    for definition in _CHANNELS:
        value = data.get(definition.id)
        if isinstance(value, dict) and isinstance(value.get("linked"), bool):
            settings[definition.id] = {"linked": value["linked"]}
    return settings


def get_channel_settings() -> dict[str, dict[str, bool]]:
    """Read valid persisted settings without applying implicit defaults."""

    with _SETTINGS_LOCK:
        return _normalise_settings(_read_raw_settings())


def is_channel_linked(channel_id: str) -> bool:
    """Return whether a channel may enqueue new work in ZeroAgent.

    Missing or damaged settings intentionally default to ``True`` so existing
    bot installations keep their previous behavior.
    """

    definition = channel_definition(channel_id)
    return get_channel_settings().get(definition.id, {"linked": True})["linked"]


def set_channel_linked(channel_id: str, linked: bool) -> dict[str, dict[str, bool]]:
    """Persist one channel's App-link flag and return the normalized snapshot."""

    definition = channel_definition(channel_id)
    if not isinstance(linked, bool):
        raise TypeError("linked must be a bool")

    with _SETTINGS_LOCK:
        settings = get_channel_settings()
        settings[definition.id] = {"linked": linked}
        path = channel_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(settings, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise
        return settings


def _wechat_token_present() -> bool:
    path = Path.home() / ".wxbot" / "token.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(str(data.get("bot_token") or "").strip())


def missing_configuration(
    definition: ChannelDefinition,
    keys: Mapping | None = None,
) -> tuple[str, ...]:
    """Return names of required values that are not configured."""

    if definition.id == "wechat":
        return () if _wechat_token_present() else ("wechat_bot_token",)
    if keys is None:
        from zero_agent.bots.common import load_keys

        keys = load_keys()
    return tuple(
        key for key in definition.required_keys if not str(keys.get(key) or "").strip()
    )


def channel_is_configured(
    definition: ChannelDefinition,
    keys: Mapping | None = None,
) -> bool:
    """Return whether a channel has enough credentials to start."""

    return not missing_configuration(definition, keys)
