"""Shared interruption classification for LLM responses.

The agent loop, ``do_no_tool`` and failover logic must agree on what counts as
an interrupted response. Keep marker matching here instead of duplicating
slightly different string checks across layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


INCOMPLETE_RETRY_PROMPT = "[System] Incomplete response. Regenerate and tooluse."
MAX_TOKENS_RETRY_PROMPT = (
    "[System] max_tokens limit reached. Use multi small steps to do it."
)


@dataclass(frozen=True)
class Interruption:
    """Classification result for a retryable incomplete LLM response."""

    kind: str
    retry_prompt: str
    reason: str
    partial: bool = False


def classify_interruption(
    response: Any = None,
    *,
    content: Optional[str] = None,
    stop_reason: Optional[str] = None,
) -> Optional[Interruption]:
    """Return the retryable interruption classification, if any.

    Args:
        response: Optional MockResponse-like object.
        content: Optional explicit content override.
        stop_reason: Optional explicit stop reason override.

    Returns:
        ``Interruption`` when the response should be treated as interrupted,
        otherwise ``None``.
    """

    if response is not None:
        if content is None:
            content = getattr(response, "content", "") or ""
        if stop_reason is None:
            stop_reason = getattr(response, "stop_reason", "") or ""

    content = content or ""
    tail = content[-100:]
    normalized_stop = str(stop_reason or "").strip().lower()

    stripped = content.strip()
    has_partial_text = bool(stripped) and not stripped.lstrip().startswith(
        ("!!!Error:", "[Error:", "[!!! 流异常中断")
    )

    if (
        stripped.lstrip().startswith(("!!!Error:", "[Error:"))
        or "[!!! 流异常中断" in tail
        or "!!!Error:" in tail
        or normalized_stop in {"stream_interrupted", "interrupted"}
    ):
        return Interruption(
            kind="incomplete",
            retry_prompt=INCOMPLETE_RETRY_PROMPT,
            reason=normalized_stop or "incomplete_marker",
            partial=has_partial_text,
        )

    if "max_tokens !!!]" in tail or normalized_stop in {"max_tokens", "length"}:
        return Interruption(
            kind="max_tokens",
            retry_prompt=MAX_TOKENS_RETRY_PROMPT,
            reason=normalized_stop or "max_tokens_marker",
            partial=has_partial_text,
        )

    return None


def append_interruption_marker(content: str, kind: str) -> str:
    """Append the interruption marker for ``kind`` unless it is already present."""

    content = content or ""
    if kind == "max_tokens":
        marker = "\n\n[!!! Response truncated: max_tokens !!!]"
        return content if "max_tokens !!!]" in content[-100:] else content + marker
    marker = "\n[!!! 流异常中断"
    return content if "[!!! 流异常中断" in content[-100:] else content + marker
