"""Tests for core/agent.py — ZeroAgent orchestrator with model switching."""

import os
from importlib import resources
from types import SimpleNamespace

import pytest
from zero_agent.core.agent import PromptAssembly, PromptBlock, ZeroAgent
from zero_agent.core.handler import BaseHandler
from zero_agent.core.config import AgentConfig, LLMBackendConfig, _config_mtime
from zero_agent.core.exceptions import ConfigError
from zero_agent.core.hooks import HookSystem
from zero_agent.core.localization import PROMPT_CAPABILITY_FILE_DELIVERY
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

    def test_all_sessions_share_the_configured_langfuse_tracer(
        self,
        multi_backend_config: AgentConfig,
        monkeypatch,
    ) -> None:
        class FakeTracer:
            enabled = True

            def __init__(self) -> None:
                self.reconfigured = []

            def reconfigure(self, config) -> None:
                self.reconfigured.append(config)

        tracer = FakeTracer()
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing.LangfuseTracer.from_config",
            lambda config: tracer,
        )
        registered = []

        def register(hooks, **kwargs):
            registered.append(kwargs["tracer"])
            return True

        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing.register",
            register,
        )
        from zero_agent.llm.factory import LLMFactory

        original_create_all = LLMFactory.create_all_sessions
        captured = []

        def create_all(config, **kwargs):
            captured.append(kwargs["tracer"])
            return original_create_all(config, **kwargs)

        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            create_all,
        )

        agent = ZeroAgent(config=multi_backend_config)

        assert registered == [tracer]


        assert captured == [tracer]
        assert all(
            getattr(session, "_tracer", None) is tracer
            for session in agent._sessions.values()
        )


    def test_config_reload_reconfigures_existing_langfuse_tracer(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        _write_reload_config(config_path, workspace=tmp_path / "workspace", model="model-a")
        config = AgentConfig.from_yaml(config_path)

        class FakeTracer:
            enabled = True

            def __init__(self) -> None:
                self.reconfigured = []

            def reconfigure(self, new_config) -> None:
                self.reconfigured.append(new_config)

        tracer = FakeTracer()
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing.LangfuseTracer.from_config",
            lambda current: tracer,
        )
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing.register",
            lambda hooks, **kwargs: True,
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda current, **kwargs: {
                "primary": _ReloadClient(current.llm_backends["primary"]),
            },
        )
        agent = ZeroAgent(config=config)
        baseline = _config_mtime[str(config_path)]

        _write_reload_config(
            config_path,
            workspace=tmp_path / "workspace",
            model="model-b",
        )
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert tracer.reconfigured == [agent.config]

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

    def test_tool_schema_language_follows_active_backend(self) -> None:
        config = AgentConfig(
            language="auto",
            llm_backends={
                "international": LLMBackendConfig(
                    name="international",
                    provider="openai",
                    api_key="key-a",
                    api_base="https://api.a.invalid",
                    model="gpt-4o",
                ),
                "chinese": LLMBackendConfig(
                    name="chinese",
                    provider="openai",
                    api_key="key-b",
                    api_base="https://api.b.invalid",
                    model="qwen-max",
                ),
            },
            default_backend="international",
            workspace_dir="/tmp/zero-agent-schema-workspace",
            memory_dir="/tmp/zero-agent-schema-memory",
        )
        agent = ZeroAgent(config=config)
        agent.loop = SimpleNamespace(
            client=agent.client,
            tools_schema=agent.registry.generate_openai_schema(),
        )

        international_schema = agent.loop.tools_schema
        assert any(
            tool["function"]["name"] == "code_run"
            and "Code executor" in tool["function"]["description"]
            for tool in international_schema
        )

        agent.switch_backend("chinese")

        chinese_schema = agent.loop.tools_schema
        assert any(
            tool["function"]["name"] == "code_run"
            and "执行" in tool["function"]["description"]
            for tool in chinese_schema
        )

    def test_live_loop_switch_updates_schema_without_replacing_custom_prompt(
        self,
    ) -> None:
        config = AgentConfig(
            language="auto",
            llm_backends={
                "international": LLMBackendConfig(
                    name="international",
                    provider="openai",
                    api_key="key-a",
                    api_base="https://api.a.invalid",
                    model="gpt-4o",
                ),
                "chinese": LLMBackendConfig(
                    name="chinese",
                    provider="openai",
                    api_key="key-b",
                    api_base="https://api.b.invalid",
                    model="qwen-max",
                ),
            },
            default_backend="international",
            workspace_dir="/tmp/zero-agent-live-workspace",
            memory_dir="/tmp/zero-agent-live-memory",
        )
        agent = ZeroAgent(config=config)
        agent.client.system = "custom prompt"
        agent.loop = SimpleNamespace(
            client=agent.client,
            tools_schema=agent.registry.generate_openai_schema(),
            system_prompt_factory=None,
        )

        agent.switch_backend("chinese")

        assert agent.client.system == "custom prompt"
        assert agent.loop.client is agent.client
        assert any(
            tool["function"]["name"] == "code_run"
            and "执行" in tool["function"]["description"]
            for tool in agent.loop.tools_schema
        )

    def test_switch_backend_updates_active_loop_client(
        self,
        multi_backend_config: AgentConfig,
    ) -> None:
        """切换后端时运行中的 loop 也必须立即使用新 session."""
        agent = ZeroAgent(config=multi_backend_config)
        old_client = agent.client
        agent.loop = SimpleNamespace(client=old_client)

        agent.switch_backend("backend_b")

        assert agent.loop.client is agent.client

    def test_custom_handler_registry_is_not_replaced_on_switch(
        self,
        multi_backend_config: AgentConfig,
    ) -> None:
        custom_registry = ToolRegistry()
        custom_handler = BaseHandler(
            registry=custom_registry,
            cwd=multi_backend_config.workspace_dir,
        )
        agent = ZeroAgent(
            config=multi_backend_config,
            handler=custom_handler,
        )

        agent.switch_backend("backend_b")

        assert agent.handler.registry is custom_registry

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


class TestPromptAssembly:
    def test_render_preserves_order_and_exposes_dynamic_blocks(self) -> None:
        assembly = PromptAssembly((
            PromptBlock("base", "base"),
            PromptBlock("runtime", "runtime", dynamic=True),
            PromptBlock("tools", "tools"),
        ))

        assert assembly.render() == "baseruntimetools"
        assert assembly.names == ("base", "runtime", "tools")
        assert assembly.dynamic_names == ("runtime",)


class TestZeroAgentSystemPrompt:
    @pytest.mark.parametrize(
        ("language", "required", "forbidden"),
        [
            (
                "zh",
                (
                    "使用用户当前使用的语言回复，除非用户明确指定其他语言。",
                    '<task_control state="open">',
                    "无需工具即可完整回答时可直接给出最终答复。",
                    "今天：",
                    "[Peer] 用户提及其他会话/后台任务状态时:",
                ),
                (
                    "Reply in the language the user is currently using",
                    '<task_control state="executing">',
                    '<task_control state="plan">',
                    "Today:",
                    "[Peer] When the user mentions other sessions",
                ),
            ),
            (
                "en",
                (
                    "Reply in the language the user is currently using unless",
                    '<task_control state="open">',
                    "When no tools are needed, provide the complete final answer directly.",
                    "Today:",
                    "[Peer] When the user mentions other sessions",
                ),
                (
                    "使用用户当前使用的语言回复",
                    '<task_control state="executing">',
                    '<task_control state="plan">',
                    "今天：",
                    "[Peer] 用户提及其他会话/后台任务状态时:",
                ),
            ),
        ],
    )
    def test_build_system_prompt_localizes_framework_text(
        self,
        multi_backend_config: AgentConfig,
        language: str,
        required: tuple[str, ...],
        forbidden: tuple[str, ...],
    ) -> None:
        multi_backend_config.language = language
        multi_backend_config.peer_hint = True
        agent = ZeroAgent(config=multi_backend_config)

        prompt = agent._build_system_prompt()

        for text in required:
            assert text in prompt
        for text in forbidden:
            assert text not in prompt

    @pytest.mark.parametrize("language", ["zh", "en"])
    def test_core_prompt_keeps_protocol_structure_without_tool_specific_rules(
        self,
        multi_backend_config: AgentConfig,
        language: str,
    ) -> None:
        multi_backend_config.language = language
        agent = ZeroAgent(config=multi_backend_config)

        prompt = agent._build_system_prompt()

        for tag in (
            "<role>",
            "</role>",
            "<operating_contract>",
            "</operating_contract>",
            "<interaction_protocol>",
            "</interaction_protocol>",
        ):
            assert tag in prompt
        for duplicated_tool_detail in (
            "script_path",
            "scripts/za_tmp.py",
            "4 KiB",
            "20 行",
            "provider-native",
        ):
            assert duplicated_tool_detail not in prompt

    def test_build_system_prompt_auto_uses_resolved_language(
        self,
        multi_backend_config: AgentConfig,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "locale.getlocale",
            lambda: ("zh_CN", "UTF-8"),
        )
        multi_backend_config.language = "auto"
        agent = ZeroAgent(config=multi_backend_config)

        prompt = agent._build_system_prompt()

        assert "使用用户当前使用的语言回复" in prompt
        assert "Reply in the language the user is currently using" not in prompt


    def test_empty_custom_system_prompt_is_preserved(
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
            max_turns=1,
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(
            config.llm_backends["default"],
            [MockResponse(content="done")],
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        agent = ZeroAgent(config=config)
        terminal = _exhaust(agent.run("hello", system_prompt=""))

        assert terminal.status is TerminalStatus.COMPLETED
        assert fake_client.system_snapshots == [""]

class TestZeroAgentConfigReload:
    """Atomic hot reload and task-boundary runtime config tests."""

    def test_reload_enabling_langfuse_wires_sessions_and_hooks(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        import zero_agent.plugins.langfuse_tracing as langfuse_plugin

        config_path = tmp_path / "config.yaml"
        _write_reload_config(config_path, workspace=tmp_path / "workspace")
        config = AgentConfig.from_yaml(config_path)

        class FakeTracer:
            enabled = False

            def __init__(self) -> None:
                self.reconfigured = []

            def reconfigure(self, new_config) -> None:
                self.reconfigured.append(new_config)
                self.enabled = bool(new_config.langfuse)

            def start_agent(self, context) -> None:
                pass

            def finish_agent(self, context) -> None:
                pass

            def start_turn_metadata(self, context) -> None:
                pass

            def finish_turn_metadata(self, context) -> None:
                pass

            def start_tool(self, context):
                return None

            def finish_tool(self, observation, context) -> None:
                pass

        tracer = FakeTracer()
        register_results = []
        original_register = langfuse_plugin.register

        def register(hooks, **kwargs):
            result = original_register(hooks, **kwargs)
            register_results.append(result)
            return result

        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing.LangfuseTracer.from_config",
            lambda current: tracer,
        )
        monkeypatch.setattr("zero_agent.plugins.langfuse_tracing.register", register)
        factory_tracers = []
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda current, **kwargs: (
                factory_tracers.append(kwargs.get("tracer"))
                or {"primary": _ReloadClient(current.llm_backends["primary"])}
            ),
        )

        agent = ZeroAgent(config=config)
        baseline = _config_mtime[str(config_path)]

        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + "\nlangfuse:\n"
            + "  public_key: pk-reload\n"
            + "  secret_key: sk-reload\n"
            + "  host: https://us.cloud.langfuse.com\n",
            encoding="utf-8",
        )
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert factory_tracers == [None, tracer]
        assert tracer.reconfigured == [agent.config]
        assert register_results == [False, True]
        assert getattr(tracer, "_hook_handlers", None) is not None

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
        contract = TaskContract("task-1", "inspect", TaskMode.EXECUTING)
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

    def test_close_response_log_survives_config_reload(self, monkeypatch, tmp_path) -> None:
        config_path = tmp_path / "config.yaml"
        _write_reload_config(config_path, workspace=tmp_path / "workspace")
        config = AgentConfig.from_yaml(config_path)
        created_paths = []

        class ClosableReloadClient(_ReloadClient):
            def __init__(self, backend):
                super().__init__(backend)
                self.closed = False

            def close_response_log(self):
                self.closed = True

        def make_sessions(current, session_log_path=None):
            created_paths.append(session_log_path)
            return {"primary": ClosableReloadClient(current.llm_backends["primary"])}

        monkeypatch.setattr("zero_agent.core.agent.LLMFactory.create_all_sessions", make_sessions)
        agent = ZeroAgent(config=config, session_log_path=str(tmp_path / "owned.log"))
        baseline = _config_mtime[str(config_path)]
        agent.close_response_log()
        _write_reload_config(config_path, workspace=tmp_path / "workspace", temperature=0.7)
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        assert created_paths == [str(tmp_path / "owned.log"), None]
        assert agent.client.closed is True

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

    def test_reload_rebuilds_tool_schema_when_model_language_changes(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        workspace = tmp_path / "workspace"
        _write_reload_config(config_path, workspace=workspace, model="gpt-4o")
        config = AgentConfig.from_yaml(config_path)
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda current: {
                "primary": _ReloadClient(current.llm_backends["primary"]),
            },
        )
        agent = ZeroAgent(config=config)
        baseline = _config_mtime[str(config_path)]

        _write_reload_config(config_path, workspace=workspace, model="qwen-max")
        _bump_mtime(config_path, baseline)

        assert agent.reload_config() is True
        code_run = next(
            tool for tool in agent.registry.generate_openai_schema()
            if tool["function"]["name"] == "code_run"
        )
        assert "执行" in code_run["function"]["description"]

    def test_apply_pending_runtime_config_is_noop_without_pending(
        self,
        tmp_path,
    ) -> None:
        config = AgentConfig(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider="openai",
                    api_key="key",
                    api_base="https://api.invalid",
                    model="gpt-4o",
                ),
            },
            default_backend="default",
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        agent = ZeroAgent(config=config)
        old_registry = agent.registry
        old_memory = agent.memory
        old_cwd = agent.handler.cwd

        agent._apply_pending_runtime_config()

        assert agent.registry is old_registry
        assert agent.memory is old_memory
        assert agent.handler.cwd == old_cwd

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
        contract = TaskContract("task-1", "inspect", TaskMode.EXECUTING)
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


def _complete_response(answer: str, evidence_refs: list[int]) -> MockResponse:
    import json

    return MockResponse(tool_calls=[MockToolCall(
        function=MockFunction(
            name="complete_task",
            arguments=json.dumps({"answer": answer, "evidence_refs": evidence_refs}),
        ),
        id="call_complete",
    )])

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
                                name="file_read",
                                arguments='{"path": "config.py"}',
                            ),
                            id="call_1",
                        ),
                    ],
                ),
                _complete_response("Done.", [1]),
            ],
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        registry = ToolRegistry()

        def file_read_handler(args, _response, _handler):
            yield "read\n"
            return f"content from {args['path']}"

        registry.register(ToolDefinition(
            name="file_read",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=file_read_handler,
            evidence_kind="read",
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
        assert tool_after_ctx["result"] == "content from config.py"
        assert '<task_control state="open">' in fake_client.system_snapshots[0]
        assert '<task_control state="executing">' in fake_client.system_snapshots[1]
        assert '<task_control state="open">' not in fake_client.system_snapshots[1]

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
                _complete_response("Done.", [1]),
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

    def test_repeated_code_run_missing_args_fail_with_protocol_error(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """模型反复漏传 script 时，loop 必须在预算耗尽后受控结束."""
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
            max_turns=10,
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        missing_code = MockResponse(
            content="执行检查。",
            tool_calls=[MockToolCall(
                function=MockFunction(
                    name="code_run",
                    arguments='{"cwd": "."}',
                ),
                id="call_missing",
            )],
        )
        fake_client = _FakeClient(
            config.llm_backends["default"],
            [missing_code, missing_code, missing_code, missing_code],
        )
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )

        agent = ZeroAgent(config=config)
        terminal = _exhaust(agent.run("inspect the project"))

        assert terminal.status is TerminalStatus.PROTOCOL_ERROR
        assert terminal.reason == "code_run_argument_retry_limit"
        assert fake_client._call_count == 4

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
    def test_new_tasks_start_open_without_text_classification(self) -> None:
        requests = (
            "hello",
            "解释一下什么是事件循环",
            "check the repository",
            "你能帮我检查项目配置吗？",
            "告诉我并修改 config.py",
        )
        assert requests
        assert TaskMode.OPEN.value == "open"

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
        assert pending.contract.mode is TaskMode.OPEN
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
                seen["mode"] = self.handler.task_contract.mode
                seen["plan_path"] = self.handler.task_contract.plan_path
                seen["max_turns"] = self.handler.max_turns
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
        assert seen["mode"] is TaskMode.PLAN
        assert seen["plan_path"] == "plan.md"
        assert seen["max_turns"] == 120

    def test_resumed_plan_passes_extended_budget_to_real_loop(
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
            max_turns=5,
            workspace_dir=str(tmp_path / "workspace"),
            memory_dir=str(tmp_path / "memory"),
        )
        fake_client = _FakeClient(config.llm_backends["default"], [])
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )
        seen = {}

        class CapturingRealLoop:
            def __init__(self, *, max_turns, handler, **_kwargs):
                seen["loop_max_turns"] = max_turns
                self.handler = handler

            def run(self, **_kwargs):
                self.handler.max_turns = seen["loop_max_turns"]
                seen["handler_max_turns"] = self.handler.max_turns
                if False:
                    yield None
                return TerminalEvent(status=TerminalStatus.FAILED, reason="blocked")

        monkeypatch.setattr("zero_agent.core.agent.AgentLoop", CapturingRealLoop)
        agent = ZeroAgent(config=config)
        agent._pending_task_state = PendingTaskState(
            contract=TaskContract("task-plan", "finish", TaskMode.PLAN, "plan.md"),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="ask_user",
        )

        _exhaust(agent.run("continue"))

        assert seen == {"loop_max_turns": 120, "handler_max_turns": 120}

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
            contract=TaskContract("pending", "original", TaskMode.EXECUTING),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="ask_user",
        )
        gen = agent.run("answer")

        assert next(gen) == "partial"
        gen.close()

        assert agent._pending_task_state is None


class TestZeroAgentInitialMode:
    """ZeroAgent.run initial_mode / plan_path contract construction."""

    @staticmethod
    def _make_config(tmp_path) -> AgentConfig:
        return AgentConfig(
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

    def _make_agent(self, tmp_path, monkeypatch) -> ZeroAgent:
        config = self._make_config(tmp_path)
        fake_client = _FakeClient(config.llm_backends["default"], [])
        monkeypatch.setattr(
            "zero_agent.core.agent.LLMFactory.create_all_sessions",
            lambda _config: {"default": fake_client},
        )
        return ZeroAgent(config=config)

    def _capture_loop(self, tmp_path, monkeypatch):
        agent = self._make_agent(tmp_path, monkeypatch)
        seen = {}

        class CapturingLoop:
            def __init__(self, *, handler, max_turns, **_kwargs):
                self.handler = handler
                self.max_turns = max_turns

            def run(self, **kwargs):
                seen["mode"] = self.handler.task_contract.mode
                seen["plan_path"] = self.handler.task_contract.plan_path
                seen["task_id"] = self.handler.task_contract.task_id
                seen["plan_verify_status"] = self.handler.plan_verify_status
                seen["system_prompt_factory"] = kwargs["system_prompt_factory"]
                seen["ledger_records"] = list(self.handler.evidence_ledger.records)
                seen["loop_max_turns"] = self.max_turns
                seen["system_prompt"] = kwargs["system_prompt"]
                seen["user_input"] = kwargs["user_input"]
                if False:
                    yield None
                return TerminalEvent(status=TerminalStatus.FAILED, reason="blocked")

        monkeypatch.setattr("zero_agent.core.agent.AgentLoop", CapturingLoop)
        return agent, seen

    def test_plan_contract_sets_mode_path_and_extended_budget(
        self, tmp_path, monkeypatch
    ) -> None:
        agent, seen = self._capture_loop(tmp_path, monkeypatch)
        _exhaust(agent.run("draft the plan", initial_mode=TaskMode.PLAN, plan_path="plan.md"))
        assert seen["mode"] is TaskMode.PLAN
        assert seen["plan_path"] == "plan.md"
        assert seen["task_id"].startswith("task-")
        assert seen["plan_verify_status"] == "missing"
        assert seen["ledger_records"] == []
        assert callable(seen["system_prompt_factory"])
        assert seen["loop_max_turns"] == 120
        assert '<task_control state="plan">' in seen["system_prompt"]
        assert '<task_control state="open">' not in seen["system_prompt"]

    def test_executing_contract_sets_mode_and_path(self, tmp_path, monkeypatch) -> None:
        agent, seen = self._capture_loop(tmp_path, monkeypatch)
        _exhaust(agent.run("execute the plan", initial_mode=TaskMode.EXECUTING, plan_path="plan.md"))
        assert seen["mode"] is TaskMode.EXECUTING
        assert seen["plan_path"] == "plan.md"
        assert callable(seen["system_prompt_factory"])
        assert seen["loop_max_turns"] == 5
        assert '<task_control state="executing">' in seen["system_prompt"]
        assert '<task_control state="open">' not in seen["system_prompt"]

    def test_open_default_contract_unchanged(self, tmp_path, monkeypatch) -> None:
        agent, seen = self._capture_loop(tmp_path, monkeypatch)
        _exhaust(agent.run("hello"))
        assert callable(seen["system_prompt_factory"])
        assert seen["mode"] is TaskMode.OPEN
        assert seen["plan_path"] is None
        assert '<task_control state="open">' in seen["system_prompt"]
        assert "[FILE:relative-path]" not in seen["system_prompt"]

    @pytest.mark.parametrize(
        ("language", "localized_rule"),
        [
            (
                "zh",
                "要在当前渠道发送图片或其他文件，请在最终回复中为每个文件仅写一个",
            ),
            (
                "en",
                "To deliver an image or other file in this channel, include exactly one",
            ),
        ],
    )
    def test_file_delivery_capability_is_localized_and_keeps_user_text_raw(
        self,
        tmp_path,
        monkeypatch,
        language,
        localized_rule,
    ) -> None:
        agent, seen = self._capture_loop(tmp_path, monkeypatch)
        agent.config.language = language
        original_text = "Please send the generated image"

        _exhaust(agent.run(
            original_text,
            prompt_capabilities=(PROMPT_CAPABILITY_FILE_DELIVERY,),
        ))

        assert seen["user_input"] == original_text
        assert localized_rule in seen["system_prompt"]
        assert localized_rule in seen["system_prompt_factory"]()

    def test_explicit_system_prompt_remains_static(self, tmp_path, monkeypatch) -> None:
        agent, seen = self._capture_loop(tmp_path, monkeypatch)

        _exhaust(agent.run("hello", system_prompt="custom system"))

        assert seen["system_prompt"] == "custom system"
        assert seen["system_prompt_factory"] is None

    @pytest.mark.parametrize("mode", [TaskMode.PLAN, TaskMode.EXECUTING])
    def test_missing_plan_path_rejected(self, tmp_path, monkeypatch, mode) -> None:
        agent = self._make_agent(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            _exhaust(agent.run("no plan path", initial_mode=mode))





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
        self.system_snapshots = []

    def chat(self, messages, tools=None):
        self.system_snapshots.append(self.system)
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
