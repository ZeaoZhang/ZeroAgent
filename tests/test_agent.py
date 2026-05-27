"""Tests for core/agent.py — ZeroAgent orchestrator with model switching."""

import os

import pytest

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig


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
