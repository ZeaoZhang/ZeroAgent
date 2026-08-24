"""Tests for Langfuse tracing configuration and lifecycle."""

from __future__ import annotations

from types import SimpleNamespace

import litellm

import zero_agent.plugins.langfuse_tracing as plugin
from zero_agent.core.hooks import HookSystem


class FakeObservation:
    _next_id = 0

    def __init__(self, name, as_type, **kwargs):
        type(self)._next_id += 1
        self.id = f"obs-{type(self)._next_id}"
        self.trace_id = "trace-1"
        self.name = name
        self.as_type = as_type
        self.kwargs = kwargs
        self.updates = []
        self.ended = False

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        self.ended = True


class FakeLangfuse:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.observations = []
        self.flush_count = 0
        type(self).instances.append(self)

    def start_observation(self, **kwargs):
        observation = FakeObservation(**kwargs)
        self.observations.append(observation)
        return observation

    def flush(self):
        self.flush_count += 1


class RecordingTracer:
    enabled = True

    def __init__(self):
        self.agent_contexts = []
        self.tool_contexts = []

    def start_agent(self, context):
        self.agent_contexts.append(context)

    def finish_agent(self, context):
        self.agent_contexts.append(context)

    def start_turn_metadata(self, context):
        return None

    def finish_turn_metadata(self, context):
        return None

    def start_tool(self, context):
        self.tool_contexts.append(("before", context))
        return object()

    def finish_tool(self, observation, context):
        self.tool_contexts.append(("after", context))


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        langfuse={
            "public_key": "pk-config",
            "secret_key": "sk-config",
            "host": "https://us.cloud.langfuse.com",
        }
    )


class TestLangfusePlugin:
    """Langfuse tracing behavior."""

    def test_explicit_config_wins_over_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")

        config = plugin._get_config(_config())

        assert config == {
            "public_key": "pk-config",
            "secret_key": "sk-config",
            "host": "https://us.cloud.langfuse.com",
        }

    def test_tracer_uses_yaml_credentials_and_current_sdk_api(self, monkeypatch) -> None:
        FakeLangfuse.instances.clear()
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: FakeLangfuse)

        tracer = plugin.LangfuseTracer.from_config(_config())

        assert FakeLangfuse.instances[0].kwargs == {
            "public_key": "pk-config",
            "secret_key": "sk-config",
            "host": "https://us.cloud.langfuse.com",
        }
        tracer.start_agent({"task": "inspect"})
        assert FakeLangfuse.instances[0].observations[0].as_type == "agent"

    def test_generation_observation_is_nested_and_maps_usage(self, monkeypatch) -> None:
        FakeLangfuse.instances.clear()
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: FakeLangfuse)
        tracer = plugin.LangfuseTracer.from_config(_config())

        tracer.start_agent({"task": "inspect"})
        generation = tracer.start_generation(
            name="llm-call",
            model="gpt-test",
            input={"messages": [{"role": "user", "content": "hi"}]},
            model_parameters={"stream": False},
        )
        tracer.finish_generation(
            generation,
            output={"content": "hello"},
            usage={
                "input_tokens": 12,
                "output_tokens": 7,
                "cache_read_tokens": 3,
                "cache_creation_tokens": 2,
            },
        )

        client = FakeLangfuse.instances[0]
        agent, recorded_generation = client.observations
        assert recorded_generation.as_type == "generation"
        assert recorded_generation.kwargs["trace_context"]["trace_id"] == agent.trace_id
        assert recorded_generation.kwargs["trace_context"]["parent_span_id"] == agent.id
        assert recorded_generation.updates[-1]["usage_details"] == {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "input_cached": 3,
            "input_cache_creation": 2,
        }
        assert recorded_generation.ended is True

    def test_registers_only_valid_events_and_keeps_litellm_callbacks_unchanged(
        self, monkeypatch
    ) -> None:
        tracer = RecordingTracer()
        hooks = HookSystem()
        sentinel_success = ["existing-success"]
        sentinel_failure = ["existing-failure"]
        sentinel_callbacks = ["existing-callback"]
        monkeypatch.setattr(litellm, "success_callback", sentinel_success)
        monkeypatch.setattr(litellm, "failure_callback", sentinel_failure)
        monkeypatch.setattr(litellm, "callbacks", sentinel_callbacks, raising=False)

        result = plugin.register(hooks, config=_config(), tracer=tracer)

        assert result is True
        assert set(event for event, handlers in hooks._handlers.items() if handlers) == {
            "agent_before",
            "turn_before",
            "tool_before",
            "tool_after",
            "turn_after",
            "agent_after",
        }
        assert litellm.success_callback is sentinel_success
        assert litellm.failure_callback is sentinel_failure
        assert litellm.callbacks is sentinel_callbacks

        hooks.trigger("agent_before", {"task": "inspect"})
        hooks.trigger("tool_before", {"tool_name": "file_read", "args": {}})
        hooks.trigger("tool_after", {"tool_name": "file_read", "result": "ok"})
        hooks.trigger("agent_after", {"turns": 1})
        assert len(tracer.agent_contexts) == 2
        assert len(tracer.tool_contexts) == 2

    def test_register_is_idempotent_for_same_tracer(self) -> None:
        tracer = RecordingTracer()
        hooks = HookSystem()

        assert plugin.register(hooks, config=_config(), tracer=tracer) is True
        assert plugin.register(hooks, config=_config(), tracer=tracer) is True

        assert {
            event: len(callbacks)
            for event, callbacks in hooks._handlers.items()
            if callbacks
        } == {
            "agent_before": 1,
            "turn_before": 1,
            "tool_before": 1,
            "tool_after": 1,
            "turn_after": 1,
            "agent_after": 1,
        }

    def test_finish_agent_closes_pending_tools_and_sanitizes_terminal(self, monkeypatch) -> None:
        FakeLangfuse.instances.clear()
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: FakeLangfuse)
        tracer = plugin.LangfuseTracer.from_config(_config())

        tracer.start_agent({"task": "inspect"})
        tool = tracer.start_tool({"tool_name": "file_read", "args": {}})
        terminal = SimpleNamespace(
            status="failed",
            reason="tool_error",
            text="sensitive response",
            data={"secret": "sensitive"},
        )
        tracer.finish_agent({"turns": 1, "terminal": terminal})

        client = FakeLangfuse.instances[0]
        agent, recorded_tool = client.observations
        assert recorded_tool.ended is True
        assert recorded_tool.updates[-1]["level"] == "ERROR"
        assert agent.updates[-1]["output"] == {
            "turns": 1,
            "status": "failed",
            "reason": "tool_error",
        }
        assert "sensitive response" not in str(agent.updates[-1])
        assert tool is recorded_tool
        assert client.flush_count == 1

    def test_reconfigure_disable_applies_after_active_agent(self, monkeypatch) -> None:
        FakeLangfuse.instances.clear()
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: FakeLangfuse)
        tracer = plugin.LangfuseTracer.from_config(_config())

        tracer.start_agent({"task": "inspect"})
        tracer.reconfigure(SimpleNamespace(langfuse=None))

        assert tracer.enabled is True
        tracer.finish_agent({"turns": 1})
        assert tracer.enabled is False

    def test_latest_pending_reconfigure_wins(self, monkeypatch) -> None:
        FakeLangfuse.instances.clear()
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: FakeLangfuse)
        tracer = plugin.LangfuseTracer.from_config(_config())
        tracer.start_agent({"task": "inspect"})

        tracer.reconfigure(
            SimpleNamespace(
                langfuse={
                    "public_key": "pk-next",
                    "secret_key": "sk-next",
                    "host": "https://us.cloud.langfuse.com",
                }
            )
        )
        tracer.reconfigure(_config())
        tracer.finish_agent({"turns": 1})

        assert tracer.enabled is True
        assert len(FakeLangfuse.instances) == 1

    def test_missing_sdk_or_client_failure_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: None)
        tracer = plugin.LangfuseTracer.from_config(_config())
        tracer.start_agent({"task": "inspect"})
        tracer.finish_agent({"turns": 1})
        tracer.flush()

        class BrokenLangfuse:
            def __init__(self, **kwargs):
                raise RuntimeError("client unavailable")

        monkeypatch.setattr(plugin, "_get_langfuse", lambda: BrokenLangfuse)
        tracer = plugin.LangfuseTracer.from_config(_config())
        tracer.start_agent({"task": "inspect"})
        tracer.finish_agent({"turns": 1})

    def test_register_returns_false_without_langfuse_or_config(self, monkeypatch) -> None:
        hooks = HookSystem()
        monkeypatch.setattr(plugin, "_get_langfuse", lambda: None)
        assert plugin.register(hooks) is False

        monkeypatch.setattr(plugin, "_get_langfuse", lambda: FakeLangfuse)
        monkeypatch.setattr(plugin, "_get_config", lambda *args: None)
        assert plugin.register(hooks) is False

    def test_environment_fallback_without_explicit_config(self, monkeypatch) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)

        config = plugin._get_config()

        assert config is not None
        assert config["public_key"] == "pk-env"
        assert config["secret_key"] == "sk-env"
