"""Tests for adapters/agent_runner.py — AgentRunner background worker wrapper."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.adapters.agent_runner import AgentRunner


@pytest.fixture
def multi_backend_config() -> AgentConfig:
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
        max_turns=5,
        workspace_dir="/tmp/test-agentrunner-workspace",
        memory_dir="/tmp/test-agentrunner-memory",
    )


@pytest.fixture
def real_agent(multi_backend_config: AgentConfig) -> ZeroAgent:
    """不 mock client 的 agent, 用于测试 LLM 管理接口."""
    return ZeroAgent(config=multi_backend_config)


@pytest.fixture
def mock_agent(multi_backend_config: AgentConfig) -> ZeroAgent:
    """mock 掉 client.chat 的 agent, 用于测试任务调度."""
    agent = ZeroAgent(config=multi_backend_config)
    agent.client.chat = MagicMock()
    agent.client.history = []
    return agent


class TestAgentRunnerConstruction:
    """AgentRunner 构造和属性."""

    def test_create_with_agent(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner._agent is mock_agent
        assert runner.is_running is False

    def test_handler_property(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner.handler is mock_agent.handler

    def test_history_property(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert isinstance(runner.history, list)

    def test_is_running_starts_false(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner.is_running is False


class TestAgentRunnerLLMManagement:
    """LLM 管理接口 (list_llms, next_llm, get_llm_name)."""

    def test_list_llms_returns_backends(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        llms = runner.list_llms()
        assert len(llms) == 2
        assert llms[0] == (0, "backend_a/model-a", True)
        assert llms[1] == (1, "backend_b/model-b", False)

    def test_get_llm_name_active(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        assert runner.get_llm_name() == "backend_a/model-a"

    def test_next_llm_cycles(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        runner.next_llm()
        assert runner.get_llm_name() == "backend_b/model-b"
        runner.next_llm()
        assert runner.get_llm_name() == "backend_a/model-a"

    def test_next_llm_with_index(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        runner.next_llm(1)
        assert runner.get_llm_name() == "backend_b/model-b"


class TestAgentRunnerTaskDispatch:
    """put_task / abort / 生命周期."""

    def test_put_task_returns_queue(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        dq = runner.put_task("hello")
        import queue
        assert isinstance(dq, queue.Queue)

    def test_abort_sets_stop_signal(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.abort()
        assert runner._stop_sig is True

    def test_stop_joins_thread(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.put_task("quick task")
        time.sleep(0.1)
        runner.stop()
        assert not runner.is_running

    def test_multiple_tasks_sequential(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        dq1 = runner.put_task("task 1")
        dq2 = runner.put_task("task 2")
        for dq in (dq1, dq2):
            try:
                while True:
                    item = dq.get(timeout=3)
                    if "done" in item:
                        break
            except Exception:
                pass
        assert not runner.is_running

    def test_put_task_worker_auto_starts(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner._worker_thread is None
        runner.put_task("test")
        assert runner._worker_thread is not None
        assert runner._worker_thread.is_alive()
