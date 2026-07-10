"""Built-in slash command handlers.

Each handler receives `(args: str, agent: ZeroAgent) -> str` and returns
a string for the REPL to display (empty = already printed by side-effects).
"""

from __future__ import annotations

import datetime
import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent


# ── /help ─────────────────────────────────────────────────────────────


def handle_help(args: str, agent: "ZeroAgent") -> str:
    """Display all registered commands with descriptions."""
    # Avoid circular import — lazily pull COMMANDS from the registry.
    from zero_agent.frontends.commands.slash_commands import COMMANDS

    lines = ["Commands:"]
    max_name = 0
    for cmd in COMMANDS:
        width = len(cmd.name) + (1 if cmd.arg_hint else 0) + len(cmd.arg_hint)
        if width > max_name:
            max_name = width

    for cmd in sorted(COMMANDS, key=lambda c: c.name):
        hint = f" {cmd.arg_hint}" if cmd.arg_hint else ""
        lhs = f"  /{cmd.name}{hint}"
        aliases = (
            f" ({', '.join('/' + a for a in cmd.aliases)})"
            if cmd.aliases
            else ""
        )
        rhs = f"{cmd.description}{aliases}"
        lines.append(f"{lhs:<{max_name + 4}}{rhs}")

    return "\n".join(lines)


# ── /exit ─────────────────────────────────────────────────────────────


def handle_exit(args: str, agent: "ZeroAgent") -> str:
    """Exit the REPL."""
    print("Bye.")
    return ""


# ── /new ──────────────────────────────────────────────────────────────


def handle_new(args: str, agent: "ZeroAgent") -> str:
    """Start a new session, clearing history but keeping backend config."""
    agent.client.history = []
    agent.client.system = ""
    agent.handler.working = {}
    agent.handler.history_info = []
    agent.handler._empty_ct = 0
    return "  新会话已开始（后端配置保留）"


# ── /save ─────────────────────────────────────────────────────────────


def handle_save(args: str, agent: "ZeroAgent") -> str:
    """Save the current session as a JSON snapshot."""
    snapshot_dir = os.path.join(
        os.path.expanduser("~"), ".zero_agent", "snapshots"
    )
    os.makedirs(snapshot_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(snapshot_dir, f"session_{timestamp}.json")

    snapshot = {
        "timestamp": timestamp,
        "system": getattr(agent.client, "system", ""),
        "history": getattr(agent.client, "history", []),
        "model": getattr(agent.client, "name", "unknown"),
    }

    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return f"  会话快照已保存: {snapshot_path}"


# ── /llms ─────────────────────────────────────────────────────────────


def handle_llms(args: str, agent: "ZeroAgent") -> str:
    """List all available LLM backends."""
    backends = agent.list_backends()
    if not backends:
        return "  没有可用的 LLM 后端"

    lines = []
    for name, model, is_active in backends:
        marker = " *" if is_active else "  "
        lines.append(f"  {marker} {name}: {model}")
    return "\n".join(lines)


# ── /resume ───────────────────────────────────────────────────────────


def handle_resume(args: str, agent: "ZeroAgent") -> str:
    """Build a resume prompt from the workspace."""
    from zero_agent.runners.cli import _build_resume_prompt

    return _build_resume_prompt(agent)


# ── /stop ─────────────────────────────────────────────────────────────


def handle_stop(args: str, agent: "ZeroAgent") -> str:
    """Abort the currently running task."""
    agent.abort()
    return "  已发送中止信号"


# ── /tools ────────────────────────────────────────────────────────────


def handle_tools(args: str, agent: "ZeroAgent") -> str:
    """List all available tools."""
    tools = agent.registry.list_all()
    if not tools:
        return "  没有注册的工具"

    lines = []
    for tool in tools:
        desc = tool.description[:80]
        lines.append(f"  {tool.name} — {desc}")
    return "\n".join(lines)


# ── /session ──────────────────────────────────────────────────────────


def handle_session(raw: str, agent: "ZeroAgent") -> str:
    """Handle /session.xxx=yyy dynamic attribute setting.

    The *raw* argument is the full text after the leading `/`, e.g.
    ``"session.max_tokens=8192"`` or ``"session.temperature=0.5"``.
    """
    if "=" not in raw:
        return "  用法: /session.<属性>=<值>  例如 /session.max_tokens=8192"

    attr_path, value = raw.split("=", 1)
    attr_path = attr_path.strip()
    value = value.strip()

    # Try to parse as JSON first, fall back to string.
    try:
        parsed_value = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed_value = value

    try:
        # Support nested attributes: session.max_tokens → client.max_tokens
        parts = attr_path.split(".")
        target = agent.client
        for seg in parts[1:]:
            target = getattr(target, seg) if hasattr(target, seg) else target
        if hasattr(target, "__setattr__"):
            key = parts[-1] if len(parts) > 1 else "session"
            setattr(target, key, parsed_value)
        return f"  {attr_path} = {parsed_value}"
    except Exception as exc:
        return f"  设置失败: {exc}"
