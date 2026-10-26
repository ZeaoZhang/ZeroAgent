"""Configuration for the local context runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from zero_agent.core.exceptions import ConfigError
from zero_agent.utils.files import path_is_within, paths_overlap


@dataclass
class ContextConfig:
    """Settings for context storage and context assembly."""

    mode: str = "local_v1"
    root: str = "./context-data"
    local_user_id: str = "local"
    auto_injection_max_bytes: int = 8000
    search_backend: str = "lexical"
    skill_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode != "local_v1":
            raise ConfigError(
                "The legacy context mode has been removed; context.mode must be 'local_v1'."
            )

    def validate(self, *, memory_dir: str) -> None:
        if not self.root or not self.local_user_id.strip():
            raise ConfigError("context.root and context.local_user_id must be non-empty")
        if self.auto_injection_max_bytes < 1:
            raise ConfigError("context.auto_injection_max_bytes must be positive")
        if self.search_backend != "lexical":
            raise ConfigError("context.search_backend currently supports only 'lexical'")
        allowed_skill_capabilities = {
            "skill:skill_search:network",
            "skill:skill_search:environment:read",
        }
        if any(capability not in allowed_skill_capabilities for capability in self.skill_capabilities):
            raise ConfigError("context.skill_capabilities contains an unsupported capability")

        root = Path(self.root).expanduser().resolve()
        memory = Path(memory_dir).expanduser().resolve()
        if paths_overlap(root, memory):
            raise ConfigError("context.root must not equal, contain, or be inside memory_dir")

        package_root = Path(__file__).resolve().parents[1]
        tests_root = package_root.parent / "tests"
        if path_is_within(root, package_root) or path_is_within(root, tests_root):
            raise ConfigError(
                "context.root must not be inside ZeroAgent source or test directories"
            )


def context_from_mapping(data: dict | None) -> ContextConfig:
    """Build context settings and apply environment values as explicit fallbacks."""

    values = dict(data or {})
    if "skill_capabilities" in values:
        raw_capabilities = values["skill_capabilities"]
        if not isinstance(raw_capabilities, (list, tuple)) or any(
            not isinstance(item, str) for item in raw_capabilities
        ):
            raise ConfigError("context.skill_capabilities must be a list of strings")
        values["skill_capabilities"] = tuple(dict.fromkeys(raw_capabilities))
    env_fields = {
        "mode": "ZA_CONTEXT_MODE",
        "root": "ZA_CONTEXT_ROOT",
        "local_user_id": "ZA_LOCAL_USER_ID",
        "search_backend": "ZA_CONTEXT_SEARCH_BACKEND",
    }
    for field_name, env_name in env_fields.items():
        env_value = os.environ.get(env_name)
        if env_value is None:
            continue
        configured = values.get(field_name)
        if configured is not None and str(configured) != env_value:
            raise ConfigError(
                f"conflicting context.{field_name} and {env_name} values"
            )
        values[field_name] = env_value
    byte_budget = os.environ.get("ZA_CONTEXT_AUTO_INJECTION_MAX_BYTES")
    if byte_budget is not None:
        try:
            parsed_budget = int(byte_budget)
        except ValueError as exc:
            raise ConfigError("ZA_CONTEXT_AUTO_INJECTION_MAX_BYTES must be an integer") from exc
        configured_budget = values.get("auto_injection_max_bytes")
        if configured_budget is not None and int(configured_budget) != parsed_budget:
            raise ConfigError(
                "conflicting context.auto_injection_max_bytes and "
                "ZA_CONTEXT_AUTO_INJECTION_MAX_BYTES values"
            )
        values["auto_injection_max_bytes"] = parsed_budget
    try:
        return ContextConfig(**values)
    except TypeError as exc:
        raise ConfigError(f"invalid context configuration: {exc}") from exc


