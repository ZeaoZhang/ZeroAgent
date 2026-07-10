"""/export command: export session conversation history to a markdown file.

Reads the conversation from agent.client.history (list of OpenAI-format
message dicts) and formats it as a readable markdown transcript.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent

# Project root is 3 levels above this file:
#   .../zero_agent/frontends/commands/export_cmd.py
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TEMP_DIR = str(_PROJECT_ROOT / "temp")


def _extract_text(content: object) -> str:
    """Extract plain text from a message content field.

    Content may be a plain string or a list of content blocks
    (e.g. [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]).

    Args:
        content: the content field of an OpenAI-format message.

    Returns:
        Joined plain text, or empty string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text", "")
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    return str(content) if content else ""


def _format_tool_calls(tool_calls: list[dict]) -> str:
    """Format tool_calls array as markdown."""
    lines: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "unknown")
        args_str = fn.get("arguments", "{}")
        try:
            args_obj = json.loads(args_str) if isinstance(args_str, str) else args_str
            args_pretty = json.dumps(args_obj, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            args_pretty = str(args_str)
        lines.append(f"**Tool Call: `{name}`**\n\n```json\n{args_pretty}\n```\n")
    return "\n".join(lines)


def _format_history(agent: "ZeroAgent") -> str:
    """Build a markdown transcript from the agent's session history.

    Args:
        agent: the ZeroAgent instance whose client.history is exported.

    Returns:
        A markdown string with the full conversation.
    """
    history = agent.client.history  # type: list[dict]
    if not history:
        en = agent.config.resolved_language == "en"
        return (
            "*（空会话，无对话历史）*"
            if not en
            else "*(Empty session — no conversation history)*"
        )

    lines = [
        "# Session Export",
        f"*Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        f"*Model: {agent.client.name}*",
        "",
    ]

    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if role == "system":
            text = _extract_text(content)
            if text:
                lines.append("---")
                lines.append("### System")
                lines.append("")
                lines.append(f"```\n{text}\n```")
                lines.append("")
        elif role == "user":
            text = _extract_text(content)
            if text:
                # Skip huge injected system prompts
                if len(text) > 12000:
                    text = text[:500] + "\n\n… *(truncated — system-injected prompt)*"
                lines.append("---")
                lines.append("### User")
                lines.append("")
                lines.append(text)
                lines.append("")
        elif role == "assistant":
            text = _extract_text(content)
            tool_calls = msg.get("tool_calls")
            if text or tool_calls:
                lines.append("---")
                lines.append("### Assistant")
                lines.append("")
                if text:
                    lines.append(text)
                    lines.append("")
                if tool_calls:
                    lines.append(_format_tool_calls(tool_calls))
        elif role == "tool":
            tool_name = msg.get("name", "tool")
            text = _extract_text(content)
            lines.append(f"**Tool Result: `{tool_name}`**")
            lines.append("")
            if text:
                # Truncate very long tool results
                if len(text) > 8000:
                    text = text[:500] + f"\n\n… *(truncated — {len(text)} chars total)*"
                lines.append(f"```\n{text}\n```")
            lines.append("")

    return "\n".join(lines)


def _export_to_path(text: str, target: str) -> str:
    """Write text to the target path.

    Creates parent directories if needed.  Appends ``.md`` suffix when
    the target has no extension.

    Args:
        text: the markdown content to write.
        target: a relative or absolute file path.

    Returns:
        The resolved absolute path of the written file.
    """
    path = Path(target).expanduser().resolve()
    if not path.suffix:
        path = path.with_suffix(".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def handle(args: str, agent: "ZeroAgent") -> str:
    """Handle /export slash command.

    Args:
        args: optional export path (relative or absolute).
              Empty → writes to ``temp/session_export_<timestamp>.md``.
        agent: the ZeroAgent instance.

    Returns:
        A status message with the exported file path.
    """
    text = _format_history(agent)

    target = args.strip()
    if target:
        path = _export_to_path(text, target)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"session_export_{ts}.md"
        path = _export_to_path(text, os.path.join(_TEMP_DIR, fname))

    en = agent.config.resolved_language == "en"
    if en:
        return f"✅ Session exported: {path}"
    return f"✅ 会话已导出: {path}"
