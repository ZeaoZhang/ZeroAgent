"""Tests for core/agent.py — ZeroAgent orchestrator with model switching."""

import os
from importlib import resources

import pytest

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig, _config_mtime
from zero_agent.core.exceptions import ConfigError
from zero_agent.core.hooks import HookSystem
from zero_agent.core.types import (
    EvidenceLedger,
    EvidenceRecord,
    PendingTaskState,
    TaskContract,
    TaskMode,
    TerminalEvent,
    TerminalStatus,
)
from zero_agent.llm.base import MockFunction, MockResponse, MockToolCall
from zero_agent.llm.failover import AutoFailoverSession
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

    def test_system_prompt_template_loads_from_assets(self) -> None:
        """系统提示词模板只从 zero_agent.assets 读取."""
        zh = resources.files("zero_agent.assets").joinpath("sys_prompt.txt").read_text(encoding="utf-8")
        en = resources.files("zero_agent.assets").joinpath("sys_prompt_en.txt").read_text(encoding="utf-8")

        assert ZeroAgent._load_system_prompt_template("zh") == zh
        assert ZeroAgent._load_system_prompt_template("en") == en

    def test_system_prompt_template_missing_asset_fails(self, monkeypatch) -> None:
        """系统提示词资产缺失时直接失败，不使用代码内 fallback."""
        class MissingAsset:
            def joinpath(self, _filename: str):
                raise FileNotFoundError("missing")

        monkeypatch.setattr(resources, "files", lambda _package: MissingAsset())

        with pytest.raises(ConfigError, match="System prompt asset is required"):
            ZeroAgent._load_system_prompt_template("zh")


class TestZeroAgentConfigReload:
    """Atomic hot reload and task-boundary runtime config tests."""

    def test_invalid_yaml_and_factory_failure_roll_back_all_state(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        _write_reload_config(config_path, workspace=tmp_path / "workspace")
        config = AgentConfig.from_yaml(config_path)
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda current: {
                "primary": _ReloadClient(current.llm_backends["primary"]),
            },
        )
        agent = ZeroAgent(config=config)
        baseline = _config_mtime[str(config_path)]
        original = (
            agent.config,
            agent._sessions,
            agent.client,
            agent.handler,
        )

        config_path.write_text("llm_backends: [", encoding="utf-8")
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is False
        assert (
            agent.config,
            agent._sessions,
            agent.client,
            agent.handler,
        ) == original
        assert _config_mtime[str(config_path)] == baseline

        _write_reload_config(
            config_path,
            workspace=tmp_path / "workspace",
            model="factory-failure",
        )
        _bump_mtime(config_path, baseline + 1_000_000)

        def fail_factory(current: AgentConfig):
            if current.llm_backends["primary"].model == "factory-failure":
                raise RuntimeError("factory failed")
            return {"primary": _ReloadClient(current.llm_backends["primary"])}

        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            fail_factory,
        )

        assert agent.reload_config() is False
        assert (
            agent.config,
            agent._sessions,
            agent.client,
            agent.handler,
        ) == original
        assert _config_mtime[str(config_path)] == baseline

    def test_reload_preserves_compatible_cache_usage_and_handler_contract(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        workspace = tmp_path / "workspace"
        _write_reload_config(config_path, workspace=workspace, temperature=0.2)
        config = AgentConfig.from_yaml(config_path)
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda current: {
                "primary": _ReloadClient(current.llm_backends["primary"]),
            },
        )
        agent = ZeroAgent(config=config)
        old_client = agent.client
        old_handler = agent.handler
        contract = TaskContract("task-1", "inspect", TaskMode.EXECUTION)
        ledger = EvidenceLedger()
        old_handler.task_contract = contract
        old_handler.evidence_ledger = ledger
        old_client.history = [{"role": "user", "content": "hello"}]
        old_client.system = "system"
        old_client.last_tools = "cached tools"
        old_client._last_tools_json = "cached json"
        old_client._total_input_tokens = 10
        old_client._total_output_tokens = 20
        old_client._total_cached_tokens = 4
        old_client._total_requests = 2
        baseline = _config_mtime[str(config_path)]

        _write_reload_config(config_path, workspace=workspace, temperature=0.7)
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert agent.client is not old_client
        assert agent.client.history == [{"role": "user", "content": "hello"}]
        assert agent.client.history is not old_client.history
        assert agent.client.system == "system"
        assert agent.client.last_tools == "cached tools"
        assert agent.client._last_tools_json == "cached json"
        assert agent.client.usage_stats == old_client.usage_stats
        assert agent.handler is old_handler
        assert agent.handler.task_contract is contract
        assert agent.handler.evidence_ledger is ledger
        assert agent.handler.client is agent.client
        assert _config_mtime[str(config_path)] == config_path.stat().st_mtime_ns

    def test_reload_resets_incompatible_cache_and_usage(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        workspace = tmp_path / "workspace"
        _write_reload_config(config_path, workspace=workspace, model="model-a")
        config = AgentConfig.from_yaml(config_path)

        def make_sessions(current: AgentConfig):
            client = _ReloadClient(current.llm_backends["primary"])
            if client.config.model == "model-b":
                client.last_tools = "factory cache"
                client._last_tools_json = "factory json"
                client._total_requests = 99
                client._total_input_tokens = 999
            return {"primary": client}

        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            make_sessions,
        )
        agent = ZeroAgent(config=config)
        agent.client.history = [{"role": "user", "content": "hello"}]
        agent.client.system = "system"
        agent.client.last_tools = "old cache"
        agent.client._last_tools_json = "old json"
        agent.client._total_requests = 3
        baseline = _config_mtime[str(config_path)]

        _write_reload_config(config_path, workspace=workspace, model="model-b")
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert agent.client.history == [{"role": "user", "content": "hello"}]
        assert agent.client.system == "system"
        assert agent.client.last_tools == ""
        assert agent.client._last_tools_json == ""
        assert agent.client.usage_stats == {
            "total_requests": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
        }

    def test_reload_preserves_actual_active_backup_identity(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        workspace = tmp_path / "workspace"
        _write_reload_config(
            config_path,
            workspace=workspace,
            include_backup=True,
        )
        config = AgentConfig.from_yaml(config_path)
        factory_calls = 0

        def make_sessions(current: AgentConfig):
            nonlocal factory_calls
            factory_calls += 1
            primary = _ReloadClient(current.llm_backends["primary"])
            backup = _ReloadClient(current.llm_backends["backup"])
            wrapper = AutoFailoverSession(primary, [backup])
            if factory_calls == 1:
                wrapper._active = backup
                wrapper._active_name = backup.name
                wrapper._is_fallback_active = True
            return {"primary": wrapper, "backup": backup}

        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            make_sessions,
        )
        agent = ZeroAgent(config=config)
        assert agent._get_active_backend_name() == "backup"
        baseline = _config_mtime[str(config_path)]

        _write_reload_config(
            config_path,
            workspace=workspace,
            include_backup=True,
            temperature=0.8,
        )
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert agent._get_active_backend_name() == "backup"
        assert agent.client is agent._sessions["backup"]

    def test_runtime_workspace_reload_is_deferred_until_task_boundary(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        old_workspace = tmp_path / "old-workspace"
        new_workspace = tmp_path / "new-workspace"
        _write_reload_config(config_path, workspace=old_workspace)
        config = AgentConfig.from_yaml(config_path)
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda current: {
                "primary": _ReloadClient(current.llm_backends["primary"]),
            },
        )
        agent = ZeroAgent(config=config)
        old_handler = agent.handler
        old_registry = agent.registry
        old_memory = agent.memory
        contract = TaskContract("task-1", "inspect", TaskMode.EXECUTION)
        ledger = EvidenceLedger()
        old_handler.task_contract = contract
        old_handler.evidence_ledger = ledger
        agent._is_running_task = True
        baseline = _config_mtime[str(config_path)]

        _write_reload_config(config_path, workspace=new_workspace)
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert agent.handler is old_handler
        assert agent.handler.client is agent.client
        assert agent.handler.cwd == str(old_workspace)
        assert agent.registry is old_registry
        assert agent.memory is old_memory
        assert agent.pending_runtime_config is agent.config
        assert agent.handler.task_contract is contract
        assert agent.handler.evidence_ledger is ledger

        agent._is_running_task = False
        agent._apply_pending_runtime_config()

        assert agent.pending_runtime_config is None
        assert agent.handler.cwd == str(new_workspace)
        assert agent.handler.registry is agent.registry
        assert agent.registry is not old_registry
        assert agent.memory is not old_memory


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
        exit_reason = _exhaust(agent.run("hello"))
        event_names = [event for event, _ctx in events]

        assert exit_reason.status is TerminalStatus.COMPLETED
        assert exit_reason.certificate is not None
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

        assert exit_reason.status is TerminalStatus.COMPLETED
        assert exit_reason.certificate is not None
        assert fake_client.calls[1][0]["role"] == "user"
        tool_result = fake_client.calls[1][0]["tool_results"][0]["content"]
        assert "after abort" in tool_result
        assert '"status": "success"' in tool_result

    def test_run_creates_fresh_handler_and_ages_key_info(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """每个任务应使用新 handler，但继承并老化 key_info."""
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
            [MockResponse(content="Done. <summary>done</summary>")],
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        agent = ZeroAgent(config=config)
        old_handler = agent.handler
        old_handler.history_info = ["[USER]: old task", "[Agent] old summary"]
        old_handler.working["key_info"] = (
            "Important context\n"
            "[SYSTEM] 此为 8 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n"
        )
        old_handler.working["related_sop"] = "plan_sop.md"
        old_handler.working["passed_sessions"] = 8

        exit_reason = _exhaust(agent.run("hello"))

        assert exit_reason.status is TerminalStatus.COMPLETED
        assert exit_reason.certificate is not None
        assert agent.handler is not old_handler
        assert agent.handler.history_info[0] == "[USER]: hello"
        assert "[USER]: old task" not in agent.handler.history_info
        assert agent.handler.working["related_sop"] == "plan_sop.md"
        assert agent.handler.working["passed_sessions"] == 9
        assert agent.handler.working["key_info"] == (
            "Important context\n"
            "[SYSTEM] 此为 9 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n"
        )


class TestZeroAgentTaskLifecycle:
    def test_task_mode_classifier_defaults_ambiguous_requests_to_execution(self) -> None:
        assert ZeroAgent._classify_task_mode("hello") is TaskMode.CHAT
        assert ZeroAgent._classify_task_mode("解释一下什么是事件循环") is TaskMode.CHAT
        assert ZeroAgent._classify_task_mode("只回答，不要执行：什么是缓存？") is TaskMode.CHAT
        assert ZeroAgent._classify_task_mode("check the repository") is TaskMode.EXECUTION
        assert ZeroAgent._classify_task_mode("帮我看看") is TaskMode.EXECUTION

    def test_waiting_task_restores_contract_and_evidence_then_clears_on_failure(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
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
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(config.llm_backends["default"], [])
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )
        observations = []
        terminals = [
            TerminalEvent(
                status=TerminalStatus.WAITING,
                reason="human_intervention",
                data={"data": {"question": "Which?", "candidates": ["A", "B"]}},
            ),
            TerminalEvent(status=TerminalStatus.FAILED, reason="blocked"),
        ]

        class CapturingLoop:
            def __init__(self, *, handler, **_kwargs):
                self.handler = handler

            def run(self, *, user_input, initial_user_content, **_kwargs):
                observations.append((
                    self.handler.task_contract,
                    list(self.handler.evidence_ledger.records),
                    user_input,
                    initial_user_content,
                ))
                if not self.handler.evidence_ledger.records:
                    self.handler.evidence_ledger.records.append(EvidenceRecord(
                        turn=1,
                        tool_name="file_read",
                        status="success",
                        kind="read",
                        summary="read config",
                    ))
                if False:
                    yield None
                return terminals.pop(0)

        monkeypatch.setattr("zero_agent.core.agent.AgentLoop", CapturingLoop)
        agent = ZeroAgent(config=config)

        first = _exhaust(agent.run("inspect the repository"))
        assert first.status is TerminalStatus.WAITING
        pending = agent._pending_task_state
        assert pending is not None
        assert pending.contract.mode is TaskMode.EXECUTION
        assert pending.contract.user_request == "inspect the repository"
        assert pending.waiting_kind == "ask_user"
        assert len(pending.ledger.records) == 1

        second = _exhaust(agent.run("A"))
        assert second.status is TerminalStatus.FAILED
        assert observations[1][0].task_id == observations[0][0].task_id
        assert observations[1][0].user_request == "inspect the repository"
        assert observations[1][1][0].summary == "read config"
        assert observations[1][2:] == ("A", "A")
        assert agent._pending_task_state is None

    def test_partial_acceptance_restores_plan_and_marks_status(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
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
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(config.llm_backends["default"], [])
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )
        seen = {}

        class CapturingLoop:
            def __init__(self, *, handler, **_kwargs):
                self.handler = handler

            def run(self, *, initial_user_content, **_kwargs):
                seen["status"] = self.handler.plan_verify_status
                seen["content"] = initial_user_content
                if False:
                    yield None
                return TerminalEvent(status=TerminalStatus.FAILED, reason="blocked")

        monkeypatch.setattr("zero_agent.core.agent.AgentLoop", CapturingLoop)
        agent = ZeroAgent(config=config)
        agent._pending_task_state = PendingTaskState(
            contract=TaskContract("task-plan", "finish the plan", TaskMode.PLAN, "plan.md"),
            ledger=EvidenceLedger(),
            plan_verify_status="partial",
            waiting_kind="plan_partial_acceptance",
            waiting_data={},
        )

        _exhaust(agent.run("接受 PARTIAL 并完成"))

        assert seen["status"] == "partial_accepted"
        assert "explicitly accepted" in seen["content"]

    def test_resumed_pending_state_is_consumed_before_cancellation(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
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
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(config.llm_backends["default"], [])
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        class PausingLoop:
            def __init__(self, **_kwargs):
                pass

            def run(self, **_kwargs):
                yield "partial"
                return TerminalEvent(status=TerminalStatus.COMPLETED)

        monkeypatch.setattr("zero_agent.core.agent.AgentLoop", PausingLoop)
        agent = ZeroAgent(config=config)
        agent._pending_task_state = PendingTaskState(
            contract=TaskContract("pending", "original", TaskMode.EXECUTION),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="ask_user",
        )
        gen = agent.run("answer")

        assert next(gen) == "partial"
        gen.close()

        assert agent._pending_task_state is None



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


class _ReloadClient:
    """State-bearing fake session used by atomic reload tests."""

    def __init__(self, config: LLMBackendConfig) -> None:
        self.config = config
        self.name = config.name
        self.history = []
        self.system = ""
        self.last_tools = ""
        self._last_tools_json = ""
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cached_tokens = 0
        self._total_requests = 0

    def reset_tool_protocol_cache(self) -> None:
        self.last_tools = ""
        self._last_tools_json = ""

    @property
    def usage_stats(self) -> dict[str, int]:
        return {
            "total_requests": self._total_requests,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cached_tokens": self._total_cached_tokens,
        }


def _write_reload_config(
    path,
    *,
    workspace,
    model: str = "model-a",
    temperature: float = 0.2,
    include_backup: bool = False,
) -> None:
    backup = """
  backup:
    provider: openai
    api_key: sk-backup
    api_base: https://backup.invalid/v1
    model: backup-model
""" if include_backup else ""
    failover = "\nfailover_backends:\n  - backup" if include_backup else ""
    path.write_text(
        f"""
default_backend: primary
workspace_dir: {workspace}
memory_dir: {workspace}/memory
llm_backends:
  primary:
    provider: openai
    api_key: sk-primary
    api_base: https://primary.invalid/v1
    model: {model}
    temperature: {temperature}
{backup}{failover}
""".lstrip(),
        encoding="utf-8",
    )


def _bump_mtime(path, previous: int) -> None:
    stat = path.stat()
    changed = max(stat.st_mtime_ns, previous + 1_000_000)
    os.utime(path, ns=(stat.st_atime_ns, changed))


def _exhaust(gen):
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
