"""Optional Langfuse tracing for Agent, tool, and LLM lifecycles."""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from typing import Any, Dict, Optional

from zero_agent.core.hooks import (
    EVENT_AGENT_AFTER,
    EVENT_AGENT_BEFORE,
    EVENT_TOOL_AFTER,
    EVENT_TOOL_BEFORE,
    EVENT_TURN_AFTER,
    EVENT_TURN_BEFORE,
)


_logger = logging.getLogger("zero_agent.plugins.langfuse")


def _get_langfuse() -> Any:
    """Return the Langfuse client class when the optional package is installed."""
    try:
        from langfuse import Langfuse
    except ImportError:
        return None
    return Langfuse


def _normalize_config(value: Any) -> Optional[dict[str, str]]:
    """Normalize a Langfuse mapping without exposing credentials."""
    if not isinstance(value, dict):
        return None
    public_key = value.get("public_key") or value.get("LANGFUSE_PUBLIC_KEY")
    secret_key = value.get("secret_key") or value.get("LANGFUSE_SECRET_KEY")
    host = (
        value.get("host")
        or value.get("base_url")
        or value.get("LANGFUSE_HOST")
        or value.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )
    if not isinstance(public_key, str) or not public_key.strip():
        return None
    if not isinstance(secret_key, str) or not secret_key.strip():
        return None
    if not isinstance(host, str) or not host.strip():
        return None
    return {
        "public_key": public_key,
        "secret_key": secret_key,
        "host": host,
    }


def _get_config(config: Optional[Any] = None) -> Optional[dict[str, str]]:
    """Read Langfuse settings, preferring an explicitly supplied AgentConfig."""
    if config is not None:
        return _normalize_config(getattr(config, "langfuse", None))

    try:
        from zero_agent.utils.keychain import Keychain

        stored = Keychain().langfuse_config
        if hasattr(stored, "use"):
            parsed = json.loads(stored.use())
            normalized = _normalize_config(parsed)
            if normalized is not None:
                return normalized
    except Exception:
        _logger.debug("Unable to read Langfuse settings from keychain", exc_info=True)

    return _normalize_config(
        {
            "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY"),
            "secret_key": os.environ.get("LANGFUSE_SECRET_KEY"),
            "host": os.environ.get("LANGFUSE_HOST")
            or os.environ.get("LANGFUSE_BASE_URL"),
        }
    )


def _usage_details(usage: Any) -> dict[str, int]:
    """Map ZeroAgent's canonical usage fields to Langfuse fields."""
    try:
        from zero_agent.llm.base import extract_usage_metrics

        metrics = extract_usage_metrics(usage)
    except Exception:
        metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        ):
            if key in usage:
                try:
                    metrics[key] = max(int(usage[key]), 0)
                except (TypeError, ValueError, OverflowError):
                    pass

    details = {
        "prompt_tokens": metrics["input_tokens"],
        "completion_tokens": metrics["output_tokens"],
        "total_tokens": metrics["input_tokens"] + metrics["output_tokens"],
    }
    if metrics["cache_read_tokens"]:
        details["input_cached"] = metrics["cache_read_tokens"]
    if metrics["cache_creation_tokens"]:
        details["input_cache_creation"] = metrics["cache_creation_tokens"]
    return details


class LangfuseTracer:
    """Best-effort Langfuse 4.x observation manager."""

    def __init__(self, client: Any = None, config: Optional[dict[str, str]] = None) -> None:
        self._client = client
        self._config = config
        self._active_agent: ContextVar[Any] = ContextVar(
            f"langfuse_agent_{id(self)}", default=None
        )
        self._agent_token: ContextVar[Any] = ContextVar(
            f"langfuse_agent_token_{id(self)}", default=None
        )
        self._active_tools: ContextVar[dict[str, list[Any]]] = ContextVar(
            f"langfuse_tools_{id(self)}", default={}
        )
        self._pending_config: Optional[dict[str, str]] = None
        self._pending_config_set = False
        self._hook_handlers: Optional[Dict[str, Any]] = None

    @classmethod
    def from_config(cls, config: Optional[Any] = None) -> "LangfuseTracer":
        """Build an enabled tracer or a no-op tracer from configuration."""
        try:
            normalized = _get_config(config)
            client_type = _get_langfuse()
        except Exception:
            _logger.warning("Langfuse configuration discovery failed", exc_info=True)
            return cls()
        if normalized is None or client_type is None:
            return cls()
        try:
            client = client_type(
                public_key=normalized["public_key"],
                secret_key=normalized["secret_key"],
                host=normalized["host"],
            )
        except Exception:
            _logger.warning("Langfuse client initialization failed", exc_info=True)
            return cls()
        return cls(client, normalized)

    @property
    def enabled(self) -> bool:
        """Whether this tracer has an active Langfuse client."""
        return self._client is not None

    def _start_observation(self, **kwargs: Any) -> Any:
        if self._client is None:
            return None
        try:
            return self._client.start_observation(**kwargs)
        except Exception:
            _logger.warning("Langfuse observation creation failed", exc_info=True)
            return None

    @staticmethod
    def _update(observation: Any, **kwargs: Any) -> None:
        if observation is None:
            return
        try:
            observation.update(**kwargs)
        except Exception:
            _logger.warning("Langfuse observation update failed", exc_info=True)

    @staticmethod
    def _end(observation: Any) -> None:
        if observation is None:
            return
        try:
            observation.end()
        except Exception:
            _logger.warning("Langfuse observation end failed", exc_info=True)

    def start_agent(self, context: dict) -> None:
        """Start the root Agent observation for the current execution context."""
        if not self.enabled:
            return
        observation = self._start_observation(
            name="zero-agent-task",
            as_type="agent",
            input=str(context.get("task") or context.get("user_input") or "")[:500],
            metadata={"model": context.get("model", "unknown")},
        )
        if observation is None:
            return
        token = self._active_agent.set(observation)
        self._agent_token.set(token)

    def finish_agent(self, context: dict) -> None:
        """Finish the root Agent observation and flush completed data."""
        for stack in self._active_tools.get().values():
            for tool_observation in reversed(stack):
                if tool_observation is not None:
                    self._update(
                        tool_observation,
                        level="ERROR",
                        status_message="tool observation closed with agent",
                    )
                    self._end(tool_observation)
        self._active_tools.set({})

        observation = self._active_agent.get()
        if observation is not None:
            terminal = context.get("terminal")
            status = getattr(terminal, "status", "")
            reason = getattr(terminal, "reason", "")
            self._update(
                observation,
                output={
                    "turns": context.get("turns", 0),
                    "status": str(getattr(status, "value", status)),
                    "reason": str(getattr(reason, "value", reason)),
                },
            )
            self._end(observation)
        token = self._agent_token.get()
        if token is not None:
            self._active_agent.reset(token)
            self._agent_token.set(None)
        if self._pending_config_set:
            pending = self._pending_config
            self._pending_config = None
            self._pending_config_set = False
            if pending != self._config:
                self._replace_client(pending)

    def start_turn_metadata(self, context: dict) -> None:
        """Reserve the turn hook for future metadata without creating extra spans."""

    def finish_turn_metadata(self, context: dict) -> None:
        """Reserve the turn hook for future metadata without creating extra spans."""

    def start_tool(self, context: dict) -> Any:
        """Start a child Tool observation under the active Agent."""
        parent = self._active_agent.get()
        if not self.enabled or parent is None:
            return None
        tool_name = str(context.get("tool_name") or "unknown")
        observation = self._start_observation(
            trace_context=self._trace_context(parent),
            name=f"tool:{tool_name}",
            as_type="tool",
            input={"args": str(context.get("args", {}))[:500]},
        )
        key = str(context.get("id") or tool_name)
        stacks = {
            stack_key: list(stack)
            for stack_key, stack in self._active_tools.get().items()
        }
        stacks.setdefault(key, []).append(observation)
        self._active_tools.set(stacks)
        return observation

    def finish_tool(self, observation: Any, context: dict) -> None:
        """Finish a child Tool observation."""
        if observation is not None:
            self._update(observation, output=str(context.get("result", ""))[:500])
            self._end(observation)
        key = str(context.get("id") or context.get("tool_name") or "unknown")
        stacks = {
            stack_key: list(stack)
            for stack_key, stack in self._active_tools.get().items()
        }
        stack = stacks.get(key)
        if stack:
            stack.pop()
            if not stack:
                stacks.pop(key, None)
            self._active_tools.set(stacks)

    @staticmethod
    def _trace_context(parent: Any) -> Any:
        values = {
            "trace_id": getattr(parent, "trace_id", ""),
            "parent_span_id": getattr(parent, "id", ""),
        }
        try:
            from langfuse.types import TraceContext

            return TraceContext(**values)
        except Exception:
            return values

    def start_generation(
        self,
        *,
        name: str,
        model: str,
        input: Any,
        model_parameters: dict[str, Any],
    ) -> Any:
        """Start one generation observation for one concrete LLM request."""
        parent = self._active_agent.get()
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": "generation",
            "input": input,
            "model": model,
            "model_parameters": model_parameters,
        }
        if parent is not None:
            kwargs["trace_context"] = self._trace_context(parent)
        return self._start_observation(**kwargs)

    def finish_generation(
        self,
        observation: Any,
        *,
        output: Any = None,
        usage: Any = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
    ) -> None:
        """Update and end one generation observation."""
        if observation is None:
            return
        updates: dict[str, Any] = {
            "output": output,
            "usage_details": _usage_details(usage),
        }
        if level is not None:
            updates["level"] = level
        if status_message is not None:
            updates["status_message"] = status_message
        self._update(observation, **updates)
        self._end(observation)

    def flush(self) -> None:
        """Flush the Langfuse client without affecting agent execution."""
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:
            _logger.warning("Langfuse flush failed", exc_info=True)

    def _replace_client(self, config: Optional[dict[str, str]]) -> None:
        """Replace the client used for future observations."""
        self.flush()
        self._client = None
        self._config = config
        if config is None:
            return
        client_type = _get_langfuse()
        if client_type is None:
            return
        try:
            self._client = client_type(
                public_key=config["public_key"],
                secret_key=config["secret_key"],
                host=config["host"],
            )
        except Exception:
            _logger.warning("Langfuse client reconfiguration failed", exc_info=True)

    def reconfigure(self, config: Optional[Any]) -> None:
        """Apply new config after the current Agent observation completes."""
        normalized = _get_config(config)
        if self._active_agent.get() is not None:
            self._pending_config = normalized
            self._pending_config_set = True
            return
        if normalized == self._config:
            self._pending_config = None
            self._pending_config_set = False
            return
        self._replace_client(normalized)


def _handler_map(tracer: LangfuseTracer) -> Dict[str, Any]:
    cached_handlers = getattr(tracer, "_hook_handlers", None)
    if cached_handlers is not None:
        return cached_handlers
    tool_observations: ContextVar[dict[str, list[Any]]] = ContextVar(
        f"langfuse_hook_tools_{id(tracer)}", default={}
    )

    def on_agent_before(context: dict) -> None:
        tool_observations.set({})
        tracer.start_agent(context)

    def on_agent_after(context: dict) -> None:
        tool_observations.set({})
        tracer.finish_agent(context)

    def on_tool_before(context: dict) -> None:
        observation = tracer.start_tool(context)
        key = str(context.get("id") or context.get("tool_name") or "unknown")
        stacks = {
            stack_key: list(stack)
            for stack_key, stack in tool_observations.get().items()
        }
        stacks.setdefault(key, []).append(observation)
        tool_observations.set(stacks)

    def on_tool_after(context: dict) -> None:
        key = str(context.get("id") or context.get("tool_name") or "unknown")
        stacks = {
            stack_key: list(stack)
            for stack_key, stack in tool_observations.get().items()
        }
        stack = stacks.get(key, [])
        observation = stack.pop() if stack else None
        if stack:
            stacks[key] = stack
        else:
            stacks.pop(key, None)
        tool_observations.set(stacks)
        tracer.finish_tool(observation, context)

    handlers = {
        EVENT_AGENT_BEFORE: on_agent_before,
        EVENT_TURN_BEFORE: tracer.start_turn_metadata,
        EVENT_TOOL_BEFORE: on_tool_before,
        EVENT_TOOL_AFTER: on_tool_after,
        EVENT_TURN_AFTER: tracer.finish_turn_metadata,
        EVENT_AGENT_AFTER: on_agent_after,
    }
    setattr(tracer, "_hook_handlers", handlers)
    return handlers


def register(
    hook_system: Any,
    config: Optional[Any] = None,
    tracer: Optional[LangfuseTracer] = None,
) -> bool:
    """Register Langfuse lifecycle hooks when tracing is configured."""
    active_tracer = tracer or LangfuseTracer.from_config(config)
    if not active_tracer.enabled and not getattr(active_tracer, "_pending_config_set", False):
        return False
    for event, callback in _handler_map(active_tracer).items():
        if not hook_system.has(event, callback):
            hook_system.register(event, callback)
    return True
