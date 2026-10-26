"""Errors shared by logical context URI and service operations."""

from __future__ import annotations


from zero_agent.core.exceptions import ZeroAgentError


class ContextError(ZeroAgentError):
    """A context operation failed with a stable, model-safe error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContextConflictError(ContextError):
    """An object revision changed before a conditional write."""

    def __init__(self, message: str = "The context object revision changed.") -> None:
        super().__init__("REVISION_CONFLICT", message)
