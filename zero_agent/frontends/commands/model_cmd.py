"""ZeroAgent /model command — runtime model inspection and switching.

Provides sub-commands for listing backends, switching models, tuning
inference parameters (temperature, reasoning effort), and fetching
remote model lists — all operating on the active session in memory.

Changes made via this command are ephemeral: they affect the running
process but do NOT persist to config.yaml across restarts.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from zero_agent.core.config import LLMBackendConfig

_logger = logging.getLogger("zero_agent.commands.model")

EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh"]


# ── public API ──────────────────────────────────────────────────────────


def current_model(agent: "ZeroAgent") -> str:
    """Return the current active model display string (name/model)."""
    return agent.get_llm_name()


def list_backends(agent: "ZeroAgent") -> list[tuple[str, str, bool]]:
    """List all configured LLM backends as [(name, model, is_current), ...]."""
    return agent.list_backends()


def switch_model(agent: "ZeroAgent", backend_name: str) -> str:
    """Switch the active backend to *backend_name* (the alias in config).

    Raises:
        ValueError: if *backend_name* is not a configured backend.
    """
    agent.switch_backend(backend_name)
    return f"已切换到后端: {backend_name}"


def set_runtime_model(agent: "ZeroAgent", model: str) -> str:
    """Replace the model ID on the current session's config in memory.

    Does NOT persist to config.yaml; only affects the running session
    until the next config reload or process restart.
    """
    agent.client.config.model = model
    return f"当前会话模型已设置为: {model}"


def set_effort(agent: "ZeroAgent", level: str) -> str:
    """Set reasoning_effort on the current session's config.

    Valid levels: none, minimal, low, medium, high, xhigh.
    """
    level = level.lower().strip()
    if level not in EFFORT_LEVELS:
        return (
            f"无效的 reasoning_effort: '{level}'。"
            f"有效值: {', '.join(EFFORT_LEVELS)}"
        )
    agent.client.config.reasoning_effort = level
    return f"reasoning_effort 已设置为: {level}"


def set_temperature(agent: "ZeroAgent", temp: float) -> str:
    """Set temperature on the current session (config + runtime attr).

    For LiteLLMSession the runtime `.temperature` drives the actual
    completion call; for AutoFailoverSession it syncs across all
    wrapped sessions.
    """
    agent.client.config.temperature = temp
    try:
        agent.client.temperature = temp
    except AttributeError:
        pass
    return f"temperature 已设置为: {temp}"


# ── helpers ─────────────────────────────────────────────────────────────


def _is_backend_name(agent: "ZeroAgent", candidate: str) -> bool:
    """Return True if *candidate* matches a configured backend name."""
    return candidate in agent._sessions


def _fetch_model_list(client: Any) -> str:
    """Fetch available models from the current backend's /models or /v1/models.

    Args:
        client: the active LLM session (LiteLLMSession or AutoFailoverSession).

    Returns:
        A formatted string listing model IDs, capped at 50 entries.
    """
    api_base = client.config.api_base.rstrip("/")
    api_key = client.config.api_key

    endpoints = [f"{api_base}/models", f"{api_base}/v1/models"]
    last_error = ""

    for url in endpoints:
        try:
            req = urllib.request.Request(url)
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode()
                data = json.loads(body)
        except Exception as exc:
            last_error = str(exc)
            continue

        models: list[dict[str, Any]] = data.get("data", data.get("models", []))
        if not models:
            return f"端点 {url} 返回了空模型列表"

        lines: list[str] = []
        for m in models[:50]:
            mid = m.get("id", m.get("model", str(m)))
            lines.append(f"  {mid}")
        if len(models) > 50:
            lines.append(f"  ... 还有 {len(models) - 50} 个模型")
        return f"可用模型 ({url}):\n" + "\n".join(lines)

    return f"获取模型列表失败: {last_error}"


# ── main handler ────────────────────────────────────────────────────────


def handle(args: str, agent: "ZeroAgent") -> str:
    """Handle the /model slash command.

    Usage:

        /model              → list all configured backends with current marked
        /model <name>       → switch active backend to *name*
        /model set <model>  → change model ID on the current session in memory
        /model effort <lvl> → set reasoning_effort (none/minimal/low/medium/high/xhigh)
        /model temp <float> → set temperature (0.0–2.0)
        /model list         → fetch remote model list from the API

    Args:
        args:  everything after ``/model`` (may be empty).
        agent: the live ZeroAgent instance.

    Returns:
        A human-readable string describing the result.
        An empty string means the action was handled with no output needed.
    """
    parts = args.strip().split()
    if not parts:
        # ── list all configured backends ──
        lines = ["配置的后端:"]
        for name, model, is_current in agent.list_backends():
            marker = " *" if is_current else "  "
            lines.append(f"{marker} {name}: {model}")
        return "\n".join(lines)

    first = parts[0]

    # ── backend name first: so a backend literally named "set" still works ──
    if _is_backend_name(agent, first):
        try:
            return switch_model(agent, first)
        except ValueError as exc:
            return str(exc)

    # ── sub-commands ──
    if first == "set":
        if len(parts) < 2:
            return "用法: /model set <model_id>"
        return set_runtime_model(agent, " ".join(parts[1:]))

    if first == "effort":
        if len(parts) < 2:
            return (
                "用法: /model effort <level>  "
                f"({', '.join(EFFORT_LEVELS)})"
            )
        return set_effort(agent, parts[1])

    if first == "temp":
        if len(parts) < 2:
            return "用法: /model temp <float>"
        try:
            return set_temperature(agent, float(parts[1]))
        except ValueError:
            return f"无效的温度值: '{parts[1]}'"

    if first == "list":
        return _fetch_model_list(agent.client)

    # Unknown argument — it's not a backend name and not a sub-command
    return f"未知子命令或后端: '{first}'。输入 /model 查看可用后端。"
