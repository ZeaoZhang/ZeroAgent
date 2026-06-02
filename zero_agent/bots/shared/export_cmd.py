"""/export command: export last assistant reply / locate full conversation log."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime

from zero_agent.bots.shared.continue_cmd import _pairs, _assistant_text

_TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "temp")
_BACKTICK_RUN_RE = re.compile(r"`+")


def wrap_for_clipboard(text: str, language: str = "markdown") -> str:
    """Wrap text in a markdown code fence that survives nested fences."""
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def last_assistant_text(runner) -> str | None:
    """Last assistant reply as joined plain text from the LLM log.

    Returns None when the backend history is empty or the log is unreadable.
    """
    try:
        client = runner.llmclient
    except AttributeError:
        return None
    if client is None:
        return None
    backend = getattr(client, "backend", None) or client
    if not getattr(backend, "history", None):
        return None

    log_path = _resolve_log_path(runner)
    if not log_path or not os.path.isfile(log_path):
        return None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        print(f"[export_cmd] failed to read {log_path}: {e}", file=sys.stderr)
        return None
    pairs = _pairs(content)
    if not pairs:
        return None
    text = _assistant_text(pairs[-1][1])
    return text if text.strip() else None


def _resolve_log_path(runner) -> str | None:
    log_path = getattr(runner, "log_path", None)
    if log_path:
        return log_path
    config = getattr(runner, "config", None)
    sessions_dir = getattr(config, "sessions_dir", None)
    if not sessions_dir:
        return None
    return os.path.join(
        os.path.abspath(sessions_dir),
        f"model_responses_{os.getpid()}.txt",
    )


def export_to_temp(text: str, name: str) -> str:
    """Write text to temp/<name>.md, overwriting on collision. Returns full path."""
    os.makedirs(_TEMP_DIR, exist_ok=True)
    safe = os.path.basename((name or "").strip())
    if not safe:
        safe = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not os.path.splitext(safe)[1]:
        safe = safe + ".md"
    path = os.path.join(_TEMP_DIR, safe)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path
