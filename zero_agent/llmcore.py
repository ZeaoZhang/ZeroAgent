"""Compatibility shim for legacy GenericAgent frontend modules.

ZeroAgent frontends should prefer ``zero_agent.bots.common.load_keys`` and
``AgentRunner`` directly. Some GA-style frontend files still import
``llmcore.mykeys`` or allow ``cost_tracker`` to patch ``_record_usage`` during
their gradual migration, so this module provides the small surface they need
without reintroducing the old LLM runtime.
"""

from __future__ import annotations

from typing import Any

from zero_agent.bots.common import load_keys

mykeys = load_keys()


def reload_mykeys() -> dict:
    """Reload bot/frontend credential keys from supported ZeroAgent sources."""
    mykeys.clear()
    mykeys.update(load_keys())
    return mykeys


def _record_usage(usage: Any, api_mode: str | None = None) -> None:
    """No-op hook kept for legacy cost-tracker monkey patches."""
    return None

