"""ZeroAgent compatibility entrypoint for legacy frontend modules.

Older GenericAgent frontends instantiate ``GeneraticAgent``/``GenericAgent``
and then talk to a queue-based API.  Keep that shape here while routing all
runtime work through ``ZeroAgent`` and ``AgentRunner``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_project_importable() -> None:
    """Allow this module to work when a frontend is run as a script."""
    project_root = Path(__file__).resolve().parents[2]
    root_text = str(project_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


_ensure_project_importable()

from zero_agent.adapters.agent_runner import AgentRunner
from zero_agent.core.agent import ZeroAgent


def create_agent() -> AgentRunner:
    """Create a frontend-ready ZeroAgent runner."""
    return AgentRunner(ZeroAgent())


class GeneraticAgent(AgentRunner):
    """Legacy misspelled class name retained for old frontends."""

    def __init__(self, *args, **kwargs) -> None:
        if args or kwargs:
            agent = ZeroAgent(*args, **kwargs)
        else:
            agent = ZeroAgent()
        super().__init__(agent)


class GenericAgent(GeneraticAgent):
    """Legacy correctly-spelled alias used by TUI variants."""


__all__ = ["AgentRunner", "GenericAgent", "GeneraticAgent", "ZeroAgent", "create_agent"]
