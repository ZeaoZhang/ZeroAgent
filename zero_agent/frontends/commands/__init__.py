"""ZeroAgent slash command system.

Central dispatcher for /slash commands with a pluggable command registry.
Each command module exports a ``handle(args: str, agent: ZeroAgent) -> str``
function and is registered in ``slash_commands.py``.
"""

from __future__ import annotations

from zero_agent.frontends.commands.slash_commands import (
    COMMANDS,
    CommandDef,
    handle_command,
    is_exit_command,
)

__all__ = ["COMMANDS", "CommandDef", "handle_command", "is_exit_command"]
