"""Canonical bot-channel configuration loading and persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml

from zero_agent.bots import channel_control


BOT_CONFIG_ENV = "ZA_BOT_CONFIG_PATH"
_CONFIG_ENV = "ZA_CONFIG_PATH"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TOKEN_FILE = Path.home() / ".wxbot" / "token.json"
_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ChannelConfigError(ValueError):
    """Raised when channel configuration cannot be safely read or written."""


# Keep this map compatible with the long-standing bot environment interface.
_ENV_MAP = {
    "tg_bot_token": "TG_BOT_TOKEN",
    "tg_allowed_users": "TG_ALLOWED_USERS",
    "discord_bot_token": "DISCORD_BOT_TOKEN",
    "discord_allowed_users": "DISCORD_ALLOWED_USERS",
    "fs_app_id": "FS_APP_ID",
    "fs_app_secret": "FS_APP_SECRET",
    "fs_allowed_users": "FS_ALLOWED_USERS",
    "wecom_bot_id": "WECOM_BOT_ID",
    "wecom_secret": "WECOM_SECRET",
    "wecom_welcome_message": "WECOM_WELCOME_MESSAGE",
    "wecom_allowed_users": "WECOM_ALLOWED_USERS",
    "dingtalk_client_id": "DINGTALK_CLIENT_ID",
    "dingtalk_client_secret": "DINGTALK_CLIENT_SECRET",
    "dingtalk_allowed_users": "DINGTALK_ALLOWED_USERS",
    "qq_app_id": "QQ_APP_ID",
    "qq_app_secret": "QQ_APP_SECRET",
    "qq_allowed_users": "QQ_ALLOWED_USERS",
    "proxy": "BOT_PROXY",
}
_CONFIG_KEYS = frozenset(_ENV_MAP)


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


def bot_config_path() -> Path:
    """Return the active YAML/JSON file containing bot configuration."""
    explicit = os.environ.get(BOT_CONFIG_ENV, "").strip()
    if explicit:
        return _resolve_path(explicit)
    configured = os.environ.get(_CONFIG_ENV, "").strip()
    if configured:
        return _resolve_path(configured)
    return _PROJECT_ROOT / "config.yaml"


def bot_config_source() -> str:
    """Return a user-facing label for the active bot configuration source."""
    if os.environ.get(BOT_CONFIG_ENV, "").strip():
        return str(bot_config_path())
    if os.environ.get(_CONFIG_ENV, "").strip():
        return str(bot_config_path())
    return "config.yaml"


def _is_yaml(path: Path) -> bool:
    return path.suffix.lower() in {".yaml", ".yml"}


def _read_document(path: Path, *, missing_ok: bool = True) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise ChannelConfigError(f"config path error: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) if _is_yaml(path) else json.load(handle)
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise ChannelConfigError(f"config parse error: {path}") from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ChannelConfigError(f"config document error: {path}")
    return document


def _expand_exact_reference(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    match = _ENV_REFERENCE.fullmatch(value)
    return os.environ.get(match.group(1), "") if match else value


def _file_keys(document: Mapping[str, Any]) -> dict[str, Any]:
    keys: dict[str, Any] = {}
    nested = document.get("bots")
    if isinstance(nested, Mapping):
        keys.update({key: value for key, value in nested.items() if key in _CONFIG_KEYS})
    # Legacy standalone bot config keeps top-level keys working. It has the
    # historical precedence over a nested duplicate.
    keys.update({key: value for key, value in document.items() if key in _CONFIG_KEYS})
    return {key: _expand_exact_reference(value) for key, value in keys.items()}


def _environment_value(key: str) -> Any:
    env_name = _ENV_MAP[key]
    raw = os.environ.get(env_name, "")
    if not raw.strip():
        return None
    if key.endswith("_allowed_users"):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _effective_keys(document: Mapping[str, Any]) -> dict[str, Any]:
    keys = _file_keys(document)
    for key in _CONFIG_KEYS:
        value = _environment_value(key)
        if value is not None:
            keys[key] = value
    return keys


def load_keys() -> dict:
    """Load bot keys from the active file, then non-empty environment values."""
    return _effective_keys(_read_document(bot_config_path()))


def _wechat_configured() -> bool:
    try:
        document = _read_document(_TOKEN_FILE)
    except ChannelConfigError:
        return False
    return bool(str(document.get("bot_token") or "").strip())


def _field_metadata(definition: channel_control.ChannelDefinition, file_keys: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for field in definition.config_fields:
        environment_value = _environment_value(field.key)
        if environment_value is not None:
            value = environment_value
            source = "environment"
            editable = False
        elif field.key in file_keys:
            value = file_keys[field.key]
            source = "file"
            editable = True
        else:
            value = None
            source = "unset"
            editable = True
        configured = not channel_control._value_is_missing(value)
        fields.append({
            "key": field.key,
            "label": field.label,
            "kind": field.kind,
            "required": field.required,
            "configured": configured,
            "source": source,
            "editable": editable,
        })
    return fields


def channel_config_snapshot(channel_id: str) -> dict:
    """Return non-sensitive metadata for a channel's editable configuration."""
    definition = channel_control.channel_definition(channel_id)
    if definition.id == "wechat":
        return {
            "channelId": definition.id,
            "source": "~/.wxbot/token.json",
            "fields": [],
            "configured": _wechat_configured(),
        }
    document = _read_document(bot_config_path())
    file_keys = _file_keys(document)
    fields = _field_metadata(definition, file_keys)
    return {
        "channelId": definition.id,
        "source": bot_config_source(),
        "fields": fields,
        "configured": not channel_control.missing_configuration(
            definition,
            _effective_keys(document),
        ),
    }


def _validate_values(
    definition: channel_control.ChannelDefinition,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {field.key: field for field in definition.config_fields}
    unknown = [key for key in values if key not in fields]
    if unknown:
        raise ChannelConfigError(f"unknown field: {unknown[0]}")
    validated: dict[str, Any] = {}
    for key, value in values.items():
        field = fields[key]
        if _environment_value(key) is not None:
            raise ChannelConfigError(f"environment-managed field: {key}")
        if value is None:
            validated[key] = None
            continue
        if field.kind == "list":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ChannelConfigError(f"invalid field: {key}")
            validated[key] = [item.strip() for item in value]
            continue
        if not isinstance(value, str) or not value.strip():
            raise ChannelConfigError(f"invalid field: {key}")
        validated[key] = value.strip()
    return validated


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    temporary_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            os.chmod(temporary_name, 0o600)
            if _is_yaml(path):
                yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False)
            else:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise ChannelConfigError(f"config write error: {path}") from exc


def update_channel_config(channel_id: str, values: Mapping[str, Any]) -> dict:
    """Validate and atomically update file-backed channel values."""
    definition = channel_control.channel_definition(channel_id)
    if definition.id == "wechat":
        if values:
            raise ChannelConfigError("wechat has no editable fields")
        return channel_config_snapshot(definition.id)
    if not isinstance(values, Mapping):
        raise ChannelConfigError("values must be an object")
    validated = _validate_values(definition, values)
    path = bot_config_path()
    document = _read_document(path)
    bots = document.get("bots")
    if bots is None:
        bots = {}
        document["bots"] = bots
    elif not isinstance(bots, dict):
        raise ChannelConfigError(f"config bots section error: {path}")
    for key, value in validated.items():
        if value is None:
            bots.pop(key, None)
        else:
            bots[key] = value
    _write_document(path, document)
    return channel_config_snapshot(definition.id)
