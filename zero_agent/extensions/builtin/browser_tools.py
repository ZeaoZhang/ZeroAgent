"""Optional browser-control capability extension.

The browser toolset is intentionally not part of ``ToolRegistry.with_builtins()``:
it adds a large amount of tool schema and depends on the optional ``[browser]``
extra.  ZeroAgent installs this built-in capability extension when extensions
are enabled and ``extensions.browser`` is not ``off``.

Modes:
    auto: register only when the browser runtime can be imported.
    on:   require the browser runtime, fail installation otherwise.
    off:  skip entirely.
"""

from __future__ import annotations

import logging

from zero_agent.extensions.context import ExtensionContext

_logger = logging.getLogger(__name__)


def _mode(config: object) -> str:
    """Normalize ``config.browser_tools`` to auto/on/off."""
    raw = getattr(config, "browser_tools", "auto")
    if isinstance(raw, bool):
        return "on" if raw else "off"
    value = str(raw or "auto").strip().lower()
    if value in {"on", "true", "yes", "1"}:
        return "on"
    if value in {"off", "false", "no", "0"}:
        return "off"
    return "auto"


def _runtime_available() -> bool:
    """Return whether the optional browser runtime dependencies import."""
    try:
        import zero_agent.browser.tm_webdriver  # noqa: F401
    except Exception:  # noqa: BLE001 - auto mode treats any import failure as absent
        return False
    return True


def setup(ctx: ExtensionContext) -> None:
    """Register the browser toolset when configured and available."""
    mode = _mode(ctx.runtime.config)
    if mode == "off":
        return

    available = _runtime_available()
    if not available:
        if mode == "on":
            raise RuntimeError(
                "browser tools enabled but runtime is unavailable; "
                "install zero-agent[browser] first",
            )
        _logger.info(
            "browser tools skipped: optional [browser] runtime is not installed",
        )
        return

    from zero_agent.tools.builtin.web import build_web_tools

    for definition in build_web_tools(ctx.runtime.config):
        ctx.runtime.register_tool(ctx.name, definition)
