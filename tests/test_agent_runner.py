"""Tests for adapters/agent_runner.py — AgentRunner background worker wrapper."""

from __future__ import annotations

import os
import threading
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

    def test_config_property(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner.config is mock_agent.config

    def test_log_path_uses_config_sessions_dir(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner.log_path == os.path.join(
            os.path.abspath(mock_agent.config.sessions_dir),
            f"model_responses_{os.getpid()}.txt",
        )

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

    def test_llmclients_expose_genericagent_shape(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        clients = runner.llmclients

        assert len(clients) == 2
        assert clients[0].name == "backend_a"
        assert clients[0].backend.name == "backend_a"
        assert clients[0].backend.model == "model-a"
        assert clients[0].backend.history is real_agent._sessions["backend_a"].history
        assert runner.llmclient.name == "backend_a"

    def test_setting_llmclient_switches_active_backend(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)

        runner.llmclient = runner.llmclients[1]

        assert runner.llm_no == 1
        assert runner.get_llm_name() == "backend_b/model-b"

    def test_private_runtime_attrs_forward_to_zeroagent(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        hook = object()

        runner._pet_req = "value"
        runner._turn_end_hooks = {"pet": hook}

        assert mock_agent._pet_req == "value"
        assert mock_agent._turn_end_hooks["pet"] is hook

    def test_task_dir_runtime_attr_forwards_to_zeroagent(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)

        runner.task_dir = "/tmp/za-task"

        assert mock_agent.task_dir == "/tmp/za-task"
        assert runner.task_dir == "/tmp/za-task"

    def test_list_llm_profiles_returns_frontend_dto(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        profiles = runner.list_llm_profiles()

        assert profiles == [
            {
                "index": 0,
                "llmNo": 0,
                "id": "backend_a",
                "name": "backend_a",
                "model": "model-a",
                "displayName": "backend_a/model-a",
                "active": True,
            },
            {
                "index": 1,
                "llmNo": 1,
                "id": "backend_b",
                "name": "backend_b",
                "model": "model-b",
                "displayName": "backend_b/model-b",
                "active": False,
            },
        ]

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

    def test_switch_llm_with_index(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        runner.switch_llm(1)
        assert runner.get_llm_name() == "backend_b/model-b"

    def test_switch_llm_with_numeric_string(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        runner.switch_llm("1")
        assert runner.get_llm_name() == "backend_b/model-b"

    def test_switch_llm_with_backend_id(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        runner.switch_llm("backend_b")
        assert runner.get_llm_name() == "backend_b/model-b"

    def test_switch_llm_unknown_backend_raises(self, real_agent: ZeroAgent) -> None:
        runner = AgentRunner(real_agent)
        with pytest.raises(ValueError, match="missing"):
            runner.switch_llm("missing")


class TestAgentRunnerHistoryHelpers:
    """History/config helpers used by frontends and shared commands."""

    def test_history_snapshot_returns_deep_copy(self, mock_agent: ZeroAgent) -> None:
        mock_agent.client.history = [{"role": "user", "content": [{"text": "hello"}]}]
        runner = AgentRunner(mock_agent)

        snapshot = runner.history_snapshot()
        snapshot[0]["content"][0]["text"] = "changed"

        assert mock_agent.client.history[0]["content"][0]["text"] == "hello"

    def test_replace_history_sets_active_history_copy(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        history = [{"role": "assistant", "content": "ok"}]
        runner.replace_history(history)
        history[0]["content"] = "mutated"

        assert mock_agent.client.history == [{"role": "assistant", "content": "ok"}]

    def test_clear_history(self, mock_agent: ZeroAgent) -> None:
        mock_agent.client.history = [{"role": "user", "content": "hello"}]
        runner = AgentRunner(mock_agent)

        runner.clear_history()

        assert mock_agent.client.history == []

    def test_config_snapshot_returns_copy(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)

        snapshot = runner.config_snapshot()
        snapshot.default_backend = "changed"

        assert mock_agent.config.default_backend == "backend_a"

    def test_append_history_entries_copies_entries(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        entries = [{"role": "user", "content": "hello"}]

        runner.append_history_entries(entries)
        entries[0]["content"] = "mutated"

        assert mock_agent.client.history == [{"role": "user", "content": "hello"}]

    def test_clear_last_tools(self, mock_agent: ZeroAgent) -> None:
        mock_agent.client.last_tools = "tools"
        runner = AgentRunner(mock_agent)

        runner.clear_last_tools()

        assert mock_agent.client.last_tools == ""

    def test_set_runtime_attr(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.set_runtime_attr("_pet_req", "value")
        assert mock_agent._pet_req == "value"

    def test_set_turn_end_hook(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        hook = object()
        runner.set_turn_end_hook("pet", hook)
        assert mock_agent._turn_end_hooks["pet"] is hook


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

    def test_background_run_consumes_put_task(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        def fake_run(prompt):
            assert prompt == "hello"
            yield {"turn": 1}
            yield "Hel"
            yield "lo"

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()

        dq = runner.put_task("hello")

        assert dq.get(timeout=3) == {"next": "Hel", "source": "user", "turn": 1}
        assert dq.get(timeout=3) == {"next": "Hello", "source": "user", "turn": 1}
        assert dq.get(timeout=3) == {"done": "Hello", "source": "user", "turn": 1}
        runner.task_queue.put("EXIT")
        thread.join(timeout=3)
        assert not thread.is_alive()

    def test_inc_out_emits_incremental_next_chunks(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        def fake_run(_prompt):
            yield "Hel"
            yield "lo"

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)
        runner.inc_out = True

        dq = runner.put_task("hello")

        assert dq.get(timeout=3)["next"] == "Hel"
        assert dq.get(timeout=3)["next"] == "lo"
        assert dq.get(timeout=3)["done"] == "Hello"

    def test_slash_hook_can_consume_legacy_commands(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        runner = AgentRunner(mock_agent)

        def fake_slash(raw_query, display_queue):
            assert raw_query == "/help"
            display_queue.put({"done": "handled", "source": "system"})
            return None

        monkeypatch.setattr(runner, "_handle_slash_cmd", fake_slash)

        dq = runner.put_task("/help")

        assert dq.get(timeout=3) == {"done": "handled", "source": "system"}
        assert mock_agent.client.chat.call_count == 0
