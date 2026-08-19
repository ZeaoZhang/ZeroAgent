"""Tests for runners/agent_runner.py — AgentRunner background worker wrapper."""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock

import pytest

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.types import TaskMode, TerminalEvent, TerminalStatus
from zero_agent.runners.agent_runner import AgentRunner, _consume_agent_run


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

    def test_llmclients_expose_frontend_shape(self, real_agent: ZeroAgent) -> None:
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


class TestConsumeAgentRun:
    @staticmethod
    def _generator(terminal: TerminalEvent, *chunks):
        yield from chunks
        return terminal

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (TerminalStatus.COMPLETED, "completion_certificate"),
            (TerminalStatus.WAITING, "human_intervention"),
            (TerminalStatus.BUDGET_EXHAUSTED, "max_turns"),
            (TerminalStatus.PROTOCOL_ERROR, "text_tool_protocol_limit"),
        ],
    )
    def test_retains_typed_terminal_return(self, status, reason) -> None:
        chunks = []
        terminal = _consume_agent_run(
            self._generator(TerminalEvent(status=status, reason=reason), "hello"),
            chunks.append,
        )

        assert chunks == ["hello"]
        assert terminal.status == status
        assert terminal.reason == reason

    def test_converts_invalid_return_to_failed(self) -> None:
        def gen():
            if False:
                yield None
            return {"status": "completed"}

        terminal = _consume_agent_run(gen(), lambda _chunk: None)

        assert terminal.status == TerminalStatus.FAILED
        assert terminal.reason == "invalid_terminal_return"

    def test_converts_exception_to_failed(self) -> None:
        def gen():
            yield "before"
            raise ValueError("broken")

        terminal = _consume_agent_run(gen(), lambda _chunk: None)

        assert terminal.status == TerminalStatus.FAILED
        assert terminal.reason == "ValueError"
        assert terminal.text == "broken"

    def test_cancellation_after_next_closes_generator(self) -> None:
        cancel_event = threading.Event()
        closed = threading.Event()

        def gen():
            try:
                cancel_event.set()
                yield "discarded"
            finally:
                closed.set()

        chunks = []
        terminal = _consume_agent_run(
            gen(),
            chunks.append,
            cancel_event=cancel_event,
        )

        assert terminal.status == TerminalStatus.CANCELLED
        assert terminal.reason == "user_cancelled"
        assert chunks == []
        assert closed.is_set()

    def test_cancellation_before_next_closes_generator(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        started = threading.Event()

        def gen():
            started.set()
            yield "discarded"

        chunks = []
        terminal = _consume_agent_run(
            gen(),
            chunks.append,
            cancel_event=cancel_event,
            cancel_reason="runner_cancelled",
        )

        assert terminal.status == TerminalStatus.CANCELLED
        assert terminal.reason == "runner_cancelled"
        assert chunks == []
        assert not started.is_set()

    def test_cancellation_after_terminal_return_wins(self) -> None:
        cancel_event = threading.Event()

        def gen():
            if False:
                yield None
            cancel_event.set()
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
            )

        terminal = _consume_agent_run(
            gen(),
            lambda _chunk: None,
            cancel_event=cancel_event,
        )

        assert terminal.status == TerminalStatus.CANCELLED
        assert terminal.reason == "user_cancelled"


class TestAgentRunnerTaskDispatch:
    """put_task / abort / 生命周期."""

    def test_put_task_returns_queue(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        dq = runner.put_task("hello")
        import queue
        assert isinstance(dq, queue.Queue)

    def test_abort_sets_cancel_event(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.abort()
        assert runner._cancel_event.is_set()

    def test_stop_joins_thread(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.put_task("quick task")
        time.sleep(0.1)
        runner.stop()
        assert not runner.is_running

    def test_multiple_tasks_sequential(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        def fake_run(prompt, **kwargs):
            yield prompt
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
            )

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)
        dq1 = runner.put_task("task 1")
        dq2 = runner.put_task("task 2")
        for dq in (dq1, dq2):
            assert dq.get(timeout=3)["type"] == "chunk"
            assert dq.get(timeout=3)["type"] == "terminal"
        assert not runner.is_running

    def test_put_task_worker_auto_starts(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        assert runner._worker_thread is None
        runner.put_task("test")
        assert runner._worker_thread is not None
        assert runner._worker_thread.is_alive()

    def test_background_run_consumes_put_task(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        def fake_run(prompt, **kwargs):
            assert prompt == "hello"
            yield {"turn": 1}
            yield "Hel"
            yield "lo"
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
            )

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()

        dq = runner.put_task("hello")

        assert dq.get(timeout=3) == {"type": "chunk", "text": "Hel", "source": "user", "turn": 1}
        assert dq.get(timeout=3) == {"type": "chunk", "text": "Hello", "source": "user", "turn": 1}
        terminal = dq.get(timeout=3)
        assert terminal["type"] == "terminal"
        assert terminal["status"] == "completed"
        assert terminal["reason"] == "completion_certificate"
        assert terminal["text"] == "Hello"
        runner.task_queue.put("EXIT")
        thread.join(timeout=3)
        assert not thread.is_alive()

    def test_inc_out_emits_incremental_chunks(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        def fake_run(_prompt, **kwargs):
            yield "Hel"
            yield "lo"
            return TerminalEvent(status=TerminalStatus.WAITING, reason="human_intervention")

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)
        runner.inc_out = True

        dq = runner.put_task("hello")

        assert dq.get(timeout=3)["text"] == "Hel"
        assert dq.get(timeout=3)["text"] == "lo"
        terminal = dq.get(timeout=3)
        assert terminal["status"] == "waiting"
        assert terminal["text"] == "Hello"

    def test_slash_hook_can_consume_commands(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        runner = AgentRunner(mock_agent)

        def fake_slash(raw_query, display_queue):
            assert raw_query == "/help"
            display_queue.put(TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="slash_command",
                text="handled",
                source="system",
            ).to_dict())
            return None

        monkeypatch.setattr(runner, "_handle_slash_cmd", fake_slash)

        dq = runner.put_task("/help")

        terminal = dq.get(timeout=3)
        assert terminal["type"] == "terminal"
        assert terminal["status"] == "completed"
        assert terminal["text"] == "handled"
        assert terminal["source"] == "system"
        assert mock_agent.client.chat.call_count == 0

    def test_slash_hook_exception_emits_failed_terminal(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        runner = AgentRunner(mock_agent)

        def fake_slash(_raw_query, _display_queue):
            raise RuntimeError("broken slash")

        monkeypatch.setattr(runner, "_handle_slash_cmd", fake_slash)

        terminal = runner.put_task("/broken", source="system").get(timeout=3)

        assert terminal["type"] == "terminal"
        assert terminal["status"] == "failed"
        assert terminal["reason"] == "RuntimeError"
        assert terminal["text"] == "broken slash"
        assert terminal["source"] == "system"
        assert mock_agent.client.chat.call_count == 0

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (TerminalStatus.BUDGET_EXHAUSTED, "max_turns"),
            (TerminalStatus.PROTOCOL_ERROR, "invalid_step_outcome"),
        ],
    )
    def test_queue_preserves_non_success_terminal(
        self, mock_agent: ZeroAgent, monkeypatch, status, reason
    ) -> None:
        def fake_run(_prompt, **kwargs):
            if False:
                yield None
            return TerminalEvent(status=status, reason=reason)

        monkeypatch.setattr(mock_agent, "run", fake_run)
        terminal = AgentRunner(mock_agent).put_task("hello").get(timeout=3)

        assert terminal["status"] == status.value
        assert terminal["reason"] == reason

    def test_queue_preserves_terminal_answer_when_quiet_loop_emits_no_chunks(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        def fake_run(_prompt, **kwargs):
            if False:
                yield ""
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
                text="accepted answer",
            )

        monkeypatch.setattr(mock_agent, "run", fake_run)
        terminal = AgentRunner(mock_agent).put_task("hello").get(timeout=3)

        assert terminal["status"] == "completed"
        assert terminal["reason"] == "completion_certificate"
        assert terminal["text"] == "accepted answer"

    def test_queue_preserves_terminal_answer_after_quiet_progress_chunks(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        mock_agent.config.verbose = False
        def fake_run(_prompt, **kwargs):
            yield "file_read({\"path\": \"README.md\"})\n\n"
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
                text="accepted answer",
            )

        monkeypatch.setattr(mock_agent, "run", fake_run)
        queue = AgentRunner(mock_agent).put_task("hello")
        assert queue.get(timeout=3)["type"] == "chunk"
        terminal = queue.get(timeout=3)

        assert terminal["status"] == "completed"
        assert terminal["reason"] == "completion_certificate"
        assert terminal["text"] == "accepted answer"

    def test_queue_preserves_short_terminal_answer_found_in_quiet_progress(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        mock_agent.config.verbose = False

        def fake_run(_prompt, **kwargs):
            yield "Tool result says Done. but completion is pending.\n"
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
                text="Done.",
            )

        monkeypatch.setattr(mock_agent, "run", fake_run)
        queue = AgentRunner(mock_agent).put_task("hello")
        assert queue.get(timeout=3)["type"] == "chunk"
        terminal = queue.get(timeout=3)

        assert terminal["text"] == "Done."

    def test_queue_converts_generator_exception(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        def fake_run(_prompt, **kwargs):
            yield "partial"
            raise LookupError("broken")

        monkeypatch.setattr(mock_agent, "run", fake_run)
        dq = AgentRunner(mock_agent).put_task("hello")

        assert dq.get(timeout=3)["type"] == "chunk"
        terminal = dq.get(timeout=3)
        assert terminal["status"] == "failed"
        assert terminal["reason"] == "LookupError"
        assert terminal["text"] == "partial"

    def test_queue_emits_cancelled_terminal(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        yielded = threading.Event()
        release = threading.Event()

        def fake_run(_prompt, **kwargs):
            yield "partial"
            yielded.set()
            release.wait(timeout=3)
            yield "discarded"
            return TerminalEvent(status=TerminalStatus.COMPLETED)

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)
        dq = runner.put_task("hello")
        assert dq.get(timeout=3)["type"] == "chunk"
        assert yielded.wait(timeout=3)

        runner.abort()
        release.set()
        terminal = dq.get(timeout=3)

        assert terminal["status"] == "cancelled"
        assert terminal["reason"] == "user_cancelled"
        assert terminal["text"] == "partial"

    def test_put_task_after_shutdown_returns_runner_shutdown_terminal(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.stop()

        dq = runner.put_task("after shutdown", source="desktop")
        terminal = dq.get(timeout=3)

        assert terminal["type"] == "terminal"
        assert terminal["status"] == "cancelled"
        assert terminal["reason"] == "runner_shutdown"
        assert terminal["source"] == "desktop"
        assert runner._worker_thread is None

    def test_put_task_after_shutdown_does_not_restart_stopped_worker(self, mock_agent: ZeroAgent) -> None:
        runner = AgentRunner(mock_agent)
        runner.start()
        worker = runner._worker_thread
        assert worker is not None
        assert worker.is_alive()

        runner.stop()
        assert not worker.is_alive()

        dq = runner.put_task("after shutdown")
        terminal = dq.get(timeout=3)

        assert terminal["status"] == "cancelled"
        assert terminal["reason"] == "runner_shutdown"
        assert runner._worker_thread is worker
        assert not runner._worker_thread.is_alive()

    def test_stop_sends_runner_shutdown_to_queued_tasks(self, mock_agent: ZeroAgent, monkeypatch) -> None:
        active_started = threading.Event()
        release_active = threading.Event()
        calls: list[str] = []

        def fake_run(prompt, **kwargs):
            calls.append(prompt)
            active_started.set()
            yield "partial"
            release_active.wait(timeout=3)
            yield "discarded"
            return TerminalEvent(status=TerminalStatus.COMPLETED)

        monkeypatch.setattr(mock_agent, "run", fake_run)
        runner = AgentRunner(mock_agent)

        active_queue = runner.put_task("active")
        assert active_queue.get(timeout=3)["type"] == "chunk"
        assert active_started.wait(timeout=3)
        queued_queue = runner.put_task("queued")

        stop_thread = threading.Thread(target=runner.stop)
        stop_thread.start()

        queued_terminal = queued_queue.get(timeout=3)
        assert queued_terminal["type"] == "terminal"
        assert queued_terminal["status"] == "cancelled"
        assert queued_terminal["reason"] == "runner_shutdown"
        assert calls == ["active"]

        release_active.set()
        active_terminal = active_queue.get(timeout=3)
        stop_thread.join(timeout=3)

        assert not stop_thread.is_alive()
        assert active_terminal["status"] == "cancelled"
        assert active_terminal["reason"] == "user_cancelled"
        assert active_terminal["text"] == "partial"

    def test_stop_racing_put_task_cancels_task_without_starting_worker(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        run_mock = MagicMock()
        monkeypatch.setattr(mock_agent, "run", run_mock)
        original_ensure_worker = AgentRunner._ensure_worker

        def stop_before_worker_start(runner: AgentRunner) -> None:
            runner.stop()
            original_ensure_worker(runner)

        monkeypatch.setattr(AgentRunner, "_ensure_worker", stop_before_worker_start)
        runner = AgentRunner(mock_agent)

        dq = runner.put_task("raced", source="race")
        terminal = dq.get(timeout=3)

        assert terminal["type"] == "terminal"
        assert terminal["status"] == "cancelled"
        assert terminal["reason"] == "runner_shutdown"
        assert terminal["source"] == "race"
        assert runner._worker_thread is None
        run_mock.assert_not_called()

    def test_put_task_forwards_task_mode_and_plan_path(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        captured = {}

        def fake_run(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["initial_mode"] = kwargs.get("initial_mode")
            captured["plan_path"] = kwargs.get("plan_path")
            yield "ok"
            return TerminalEvent(status=TerminalStatus.COMPLETED, reason="completion_certificate")

        monkeypatch.setattr(mock_agent, "run", fake_run)
        dq = AgentRunner(mock_agent).put_task(
            "hello", task_mode=TaskMode.EXECUTING, plan_path="plan.md"
        )

        assert dq.get(timeout=3)["type"] == "chunk"
        assert dq.get(timeout=3)["type"] == "terminal"
        assert captured["prompt"] == "hello"
        assert captured["initial_mode"] is TaskMode.EXECUTING
        assert captured["plan_path"] == "plan.md"

    def test_put_task_defaults_to_open_contract(
        self, mock_agent: ZeroAgent, monkeypatch
    ) -> None:
        captured = {}

        def fake_run(prompt, **kwargs):
            captured["initial_mode"] = kwargs.get("initial_mode")
            captured["plan_path"] = kwargs.get("plan_path")
            yield "ok"
            return TerminalEvent(status=TerminalStatus.COMPLETED, reason="completion_certificate")

        monkeypatch.setattr(mock_agent, "run", fake_run)
        dq = AgentRunner(mock_agent).put_task("hello")

        assert dq.get(timeout=3)["type"] == "chunk"
        assert dq.get(timeout=3)["type"] == "terminal"
        assert captured["initial_mode"] is TaskMode.OPEN
        assert captured["plan_path"] is None
