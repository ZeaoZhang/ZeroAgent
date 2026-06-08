"""Tests for core/agent.py — ZeroAgent orchestrator with model switching."""

import os

import pytest

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.hooks import HookSystem
from zero_agent.llm.base import MockFunction, MockResponse, MockToolCall
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


@pytest.fixture
def multi_backend_config() -> AgentConfig:
    """创建多后端配置用于测试."""
    return AgentConfig(
        llm_backends={
            "backend_a": LLMBackendConfig(
                name="backend_a",
                provider="openai",
                api_key="test-key-a",
                api_base="https://api.a.com",
                model="model-a",
            ),
            "backend_b": LLMBackendConfig(
                name="backend_b",
                provider="openai",
                api_key="test-key-b",
                api_base="https://api.b.com",
                model="model-b",
            ),
        },
        default_backend="backend_a",
        max_turns=10,
        workspace_dir="/tmp/test-workspace",
        memory_dir="/tmp/test-memory",
    )


class TestZeroAgentBackends:
    """Model switching tests."""

    def test_creates_all_sessions(self, multi_backend_config: AgentConfig) -> None:
        """ZeroAgent 创建所有配置的 session."""
        agent = ZeroAgent(config=multi_backend_config)
        assert len(agent._sessions) == 2
        assert "backend_a" in agent._sessions
        assert "backend_b" in agent._sessions

    def test_list_backends(self, multi_backend_config: AgentConfig) -> None:
        """list_backends 返回正确的后端列表."""
        agent = ZeroAgent(config=multi_backend_config)
        backends = agent.list_backends()
        assert len(backends) == 2
        names = {b[0] for b in backends}
        assert names == {"backend_a", "backend_b"}
        # 第一个是活跃的（default_backend）
        active = [b for b in backends if b[2]]
        assert len(active) == 1
        assert active[0][0] == "backend_a"

    def test_switch_backend(self, multi_backend_config: AgentConfig) -> None:
        """switch_backend 切换到指定后端."""
        agent = ZeroAgent(config=multi_backend_config)
        old_client = agent.client

        agent.switch_backend("backend_b")

        assert agent.client is not old_client
        assert agent.client is agent._sessions["backend_b"]

    def test_switch_backend_preserves_history(self, multi_backend_config: AgentConfig) -> None:
        """switch_backend 迁移对话历史."""
        agent = ZeroAgent(config=multi_backend_config)

        # 写入一些历史到当前 client
        agent.client.history = [{"role": "user", "content": "hello"}]
        agent.client.system = "test system"

        agent.switch_backend("backend_b")

        # 历史应该被迁移
        assert agent.client.history == [{"role": "user", "content": "hello"}]
        assert agent.client.system == "test system"
        assert agent.client.last_tools == ""

    def test_switch_backend_invalid_name(self, multi_backend_config: AgentConfig) -> None:
        """切换不存在的后端抛出 ValueError."""
        agent = ZeroAgent(config=multi_backend_config)
        with pytest.raises(ValueError, match="不存在"):
            agent.switch_backend("nonexistent")

    def test_switch_backend_no_history(self, multi_backend_config: AgentConfig) -> None:
        """切换时旧 client 无 history 属性也能正常工作."""
        agent = ZeroAgent(config=multi_backend_config)

        # 模拟无 history 属性的 client
        class MinimalClient:
            pass

        agent.client = MinimalClient()
        agent.switch_backend("backend_b")
        assert agent.client.history == []

    def test_get_active_backend_name(self, multi_backend_config: AgentConfig) -> None:
        """_get_active_backend_name 返回当前后端名."""
        agent = ZeroAgent(config=multi_backend_config)
        # 单 session 时通过对象匹配找到名称
        name = agent._get_active_backend_name()
        assert name == "backend_a"

    def test_default_backend_selected_by_name_when_models_match(self) -> None:
        """相同 model 的多个 backend 不能通过 model 字符串误选默认后端."""
        config = AgentConfig(
            llm_backends={
                "backend_a": LLMBackendConfig(
                    name="backend_a",
                    provider="openai",
                    api_key="test-key-a",
                    api_base="https://api.a.com",
                    model="same-model",
                ),
                "backend_b": LLMBackendConfig(
                    name="backend_b",
                    provider="openai",
                    api_key="test-key-b",
                    api_base="https://api.b.com",
                    model="same-model",
                ),
            },
            default_backend="backend_b",
            workspace_dir="/tmp/test-workspace",
            memory_dir="/tmp/test-memory",
        )

        agent = ZeroAgent(config=config)

        assert agent.client is agent._sessions["backend_b"]
        assert agent._get_active_backend_name() == "backend_b"


class TestZeroAgentHooks:
    """ZeroAgent.run() hook wiring tests."""

    def test_run_passes_hooks_and_sets_loop(self, tmp_path, monkeypatch) -> None:
        """一次 mock loop 中应触发 agent/llm/tool/turn 事件."""
        config = AgentConfig(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider="openai",
                    api_key="test-key",
                    api_base="https://api.openai.com/v1",
                    model="test-model",
                ),
            },
            default_backend="default",
            max_turns=5,
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(
            config.llm_backends["default"],
            [
                MockResponse(
                    content="",
                    tool_calls=[
                        MockToolCall(
                            function=MockFunction(
                                name="echo",
                                arguments='{"message": "hello"}',
                            ),
                            id="call_1",
                        ),
                    ],
                ),
                MockResponse(content="Done. <summary>done</summary>"),
            ],
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        registry = ToolRegistry()

        def echo_handler(args, _response, _handler):
            yield "echo\n"
            return {"result": args["message"], "_za_next_prompt": "next"}

        registry.register(ToolDefinition(
            name="echo",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=echo_handler,
        ))

        events: list[tuple[str, dict]] = []
        hooks = HookSystem()
        for event in hooks._handlers:
            hooks.register(event, lambda ctx, event=event: events.append((event, ctx)))

        agent = ZeroAgent(config=config, registry=registry, hooks=hooks)
        exit_reason = _exhaust(agent.run("task"))
        event_names = [event for event, _ctx in events]

        assert exit_reason["result"] == "CURRENT_TASK_DONE"
        assert agent.loop is not None
        assert agent.loop.hooks is hooks
        assert "agent_before" in event_names
        assert "llm_before" in event_names
        assert "llm_after" in event_names
        assert "tool_before" in event_names
        assert "tool_after" in event_names
        assert "turn_before" in event_names
        assert event_names.count("turn_after") == 2
        assert event_names[-1] == "agent_after"
        tool_after_ctx = next(ctx for event, ctx in events if event == "tool_after")
        assert tool_after_ctx["result"] == {"result": "hello"}

    def test_abort_signal_does_not_poison_next_code_run(self, tmp_path, monkeypatch) -> None:
        """一次 abort 不应让后续任务的 code_run 被立刻杀死."""
        config = AgentConfig(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider="openai",
                    api_key="test-key",
                    api_base="https://api.openai.com/v1",
                    model="test-model",
                ),
            },
            default_backend="default",
            max_turns=5,
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(
            config.llm_backends["default"],
            [
                MockResponse(
                    content="",
                    tool_calls=[
                        MockToolCall(
                            function=MockFunction(
                                name="code_run",
                                arguments='{"type": "python", "script": "print(\\"after abort\\")"}',
                            ),
                            id="call_code",
                        ),
                    ],
                ),
                MockResponse(content="Done. <summary>done</summary>"),
            ],
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        agent = ZeroAgent(config=config)
        agent.abort()
        exit_reason = _exhaust(agent.run("run code after abort"))

        assert exit_reason["result"] == "CURRENT_TASK_DONE"
        assert fake_client.calls[1][0]["role"] == "user"
        tool_result = fake_client.calls[1][0]["tool_results"][0]["content"]
        assert "after abort" in tool_result
        assert '"status": "success"' in tool_result


class _FakeClient:
    """Minimal LLM client for ZeroAgent.run() tests."""

    def __init__(self, config: LLMBackendConfig, responses: list[MockResponse]) -> None:
        self.config = config
        self.name = config.name
        self.system = ""
        self.last_tools = ""
        self.history = []
        self._responses = list(responses)
        self._call_count = 0
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        if self._call_count >= len(self._responses):
            yield "Done."
            return MockResponse(content="Done. <summary>done</summary>")
        response = self._responses[self._call_count]
        self._call_count += 1
        if response.content:
            yield response.content
        return response


def _exhaust(gen):
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
