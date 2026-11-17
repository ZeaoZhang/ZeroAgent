"""针对审阅发现的回归测试：注册归属、半注册清理、门控集成、路径判定."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from zero_agent.core.types import TaskContract, TaskMode
from zero_agent.extensions.builtin import mcp_client, permission_gate
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _tool(name: str, description: str = "d") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        handler=lambda args, response, handler: None,
    )


def _registry() -> ToolRegistry:
    return ToolRegistry()


def _runtime(home: Path, registry: ToolRegistry | None = None) -> ExtensionRuntime:
    return ExtensionRuntime(registry=registry or _registry(), home=home)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestReleaseOwnerOrdering:
    def test_release_earlier_owner_keeps_later_definition(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "builtin"))
        registry.register(_tool("t", "A"), owner="A")
        registry.register(_tool("t", "B"), owner="B")

        registry.release_owner("A")

        assert registry.get("t").description == "B"
        assert registry.owner_of("t") == "B"

    def test_release_all_restores_baseline(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "builtin"))
        registry.register(_tool("t", "A"), owner="A")
        registry.register(_tool("t", "B"), owner="B")

        registry.release_owner("A")
        registry.release_owner("B")

        assert registry.get("t").description == "builtin"
        assert registry.owner_of("t") is None

    def test_sequential_release_does_not_resurrect(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "A"), owner="A")
        registry.register(_tool("t", "B"), owner="B")

        registry.release_owner("A")
        registry.release_owner("B")

        assert registry.get("t") is None

    def test_plain_register_takes_over_ownership(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "A"), owner="A")
        registry.register(_tool("t", "plain"))

        registry.release_owner("A")

        assert registry.get("t").description == "plain"
        assert registry.owner_of("t") is None

    def test_remove_drops_override_chain(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "builtin"))
        registry.register(_tool("t", "A"), owner="A")
        registry.remove("t")

        registry.release_owner("A")

        assert registry.get("t") is None


class TestHalfRegisteredPlugin:
    def test_failed_setup_is_fully_rolled_back(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "half.py",
            (
                "def setup(ctx):\n"
                "    @ctx.tool(name='half_tool', description='d', parameters={'type': 'object', 'properties': {}})\n"
                "    def half_tool(args):\n"
                "        return 'x'\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        return ctx.block('always')\n"
                "    ctx.prompt_block('p', lambda: 'leftover')\n"
                "    raise RuntimeError('boom after registering')\n"
            ),
        )
        registry = _registry()
        runtime = _runtime(tmp_path, registry)

        assert runtime.load_all() == 0

        assert registry.get("half_tool") is None
        assert runtime.run_tool_call("code_run", {}) is None
        assert runtime.collect_prompt_blocks() == []
        assert "half" not in runtime.loaded_names


class TestGateIntegration:
    def _handler(self, runtime: ExtensionRuntime):
        from zero_agent.core.handler import BaseHandler

        class Parent:
            def __init__(self) -> None:
                self.extensions = runtime
                self.loop = None
                self.config = None

        handler = BaseHandler(registry=runtime.registry, cwd=".")
        handler.parent = Parent()
        handler.task_contract = TaskContract(
            task_id="t1",
            user_request="do something",
            mode=TaskMode.OPEN,
        )
        return handler

    def test_blocked_call_does_not_promote_task_and_records_error(
        self,
        tmp_path: Path,
    ) -> None:
        _write(
            tmp_path / "extensions" / "deny.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        return ctx.block('not allowed')\n"
            ),
        )
        registry = _registry()
        registry.register(ToolDefinition(
            name="code_run",
            description="run",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, response, handler: None,
            evidence_kind="execute",
        ), )
        runtime = _runtime(tmp_path, registry)
        runtime.load_all()

        handler = self._handler(runtime)
        outcome = None
        for _ in handler.dispatch("code_run", {"code": "ls"}, None):
            pass
        # dispatch 是 generator，需要取 return 值
        generator = handler.dispatch("code_run", {"code": "ls"}, None)
        try:
            while True:
                next(generator)
        except StopIteration as stop:
            outcome = stop.value

        assert outcome is not None
        assert outcome.data["blocked"] is True
        # 关键：被拦截不应推进任务状态
        assert handler.task_contract.mode is TaskMode.OPEN
        # 证据记录为 error，不能用于完成判定
        records = handler.evidence_ledger.records
        assert records and records[-1].status == "error"


class TestReplayOnRegistryRebuild:
    def test_switch_path_replays_extensions(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "tool.py",
            (
                "def setup(ctx):\n"
                "    @ctx.tool(name='echo_tool', description='d', parameters={'type': 'object', 'properties': {}})\n"
                "    def echo(args):\n"
                "        return 'ok'\n"
            ),
        )
        old_registry = _registry()
        runtime = _runtime(tmp_path, old_registry)
        runtime.load_all()
        assert old_registry.get("echo_tool") is not None

        # 模拟 registry 重建后调用统一的 replayer
        from zero_agent.core.agent import ZeroAgent

        new_registry = _registry()
        new_registry.register(_tool("builtin_tool", "builtin"))
        new_config = object()

        class Stub:
            extensions = runtime
            registry = new_registry
            loop = None
            config = new_config

        ZeroAgent._replay_extension_registrations(Stub())

        assert new_registry.get("echo_tool") is not None
        assert runtime.registry is new_registry
        # 热重载换了配置对象后，运行时必须跟着换：
        # permission_gate 用 runtime.config 解析 workspace 基准
        assert runtime.config is new_config


class TestPermissionGateHardening:
    def _runtime(self, tmp_path: Path) -> ExtensionRuntime:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        return runtime

    def test_separate_rm_flags_are_blocked(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path)
        for code in (
            "rm -r -f /tmp/x",
            "rm --recursive --force /tmp/x",
            "rm -R -F /tmp/x",
            "cd /tmp && rm -rf build",
        ):
            assert runtime.run_tool_call("code_run", {"code": code}) is not None, code

    def test_plain_force_remove_is_allowed(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path)
        assert runtime.run_tool_call("code_run", {"code": "rm -f a.txt"}) is None

    def test_symlink_into_protected_dir_requires_approval(
        self, tmp_path: Path,
    ) -> None:
        runtime = self._runtime(tmp_path)
        protected = Path(runtime.home) / "skills"
        protected.mkdir(parents=True, exist_ok=True)
        link = tmp_path / "link-to-skills"
        try:
            os.symlink(protected, link)
        except OSError:
            pytest.skip("当前环境不支持符号链接")

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": str(link / "demo" / "SKILL.md")}
        )
        # 软链解析后落在受保护目录：应是“等待批准”，不是静默放行
        assert verdict.needs_approval

    def test_empty_path_is_blocked(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path)
        assert runtime.run_tool_call("file_write", {"path": ""}) is not None


class TestShellScope:
    """递归删除按 workspace 作用域判定：内放行、越界或范围不明拒绝."""

    def _runtime(self, tmp_path: Path) -> ExtensionRuntime:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        config = SimpleNamespace(
            memory_dir=str(tmp_path / "mem"),
            workspace_dir=str(workspace),
            extensions_protected_writes=[],
        )
        runtime = ExtensionRuntime(registry=_registry(), home=home, config=config)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        return runtime

    @pytest.mark.parametrize(
        "code",
        [
            "rm -rf build/",
            "rm -rf node_modules",
            "rm -r -f build",
            "rm -rf ./a ./b",
            "cd build && rm -rf node_modules",
            "find . -name '*.pyc' -delete",
            "rm -f a.txt",
        ],
    )
    def test_workspace_scoped_removal_is_allowed(
        self, tmp_path: Path, code: str
    ) -> None:
        runtime = self._runtime(tmp_path)

        verdict = runtime.evaluate_tool_call("code_run", {"script": code})

        assert verdict.is_allow, (code, verdict.reason)

    @pytest.mark.parametrize(
        "code",
        [
            "rm -rf /",
            "sudo rm -rf /",
            "sudo -u root rm -rf /",
            "env FOO=1 rm -rf ../x",
            "rm -rf build /etc",
            'rm -rf "$DIR"',
            "xargs rm -rf",
            "cd /tmp && rm -rf build",
            "find / -delete",
            "mkfs.ext4 /dev/sda",
        ],
    )
    def test_out_of_workspace_removal_is_blocked(
        self, tmp_path: Path, code: str
    ) -> None:
        runtime = self._runtime(tmp_path)

        verdict = runtime.evaluate_tool_call("code_run", {"script": code})

        assert verdict.is_block, (code, verdict)


class TestProtectedPrefixScope:
    """workspace 落在扩展根目录下时，workspace 内写入不应触发审批."""

    def _setup(
        self,
        tmp_path: Path,
        extra: tuple[str, ...] = (),
    ) -> tuple[ExtensionRuntime, Path, Path]:
        home = tmp_path / "home"
        home.mkdir()
        workspace = home / "workspace"
        workspace.mkdir()
        config = SimpleNamespace(
            memory_dir=str(tmp_path / "mem"),
            workspace_dir=str(workspace),
            extensions_protected_writes=list(extra),
        )
        runtime = ExtensionRuntime(registry=_registry(), home=home, config=config)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        return runtime, home, workspace

    def test_workspace_inside_home_is_not_protected(self, tmp_path: Path) -> None:
        runtime, home, workspace = self._setup(tmp_path)

        inside = runtime.evaluate_tool_call(
            "file_write", {"path": str(workspace / "a.txt"), "content": "x"},
        )
        assert inside.is_allow

        specific = runtime.evaluate_tool_call(
            "file_write",
            {"path": str(home / "skills" / "x" / "SKILL.md"), "content": "x"},
        )
        assert specific.needs_approval

        other = runtime.evaluate_tool_call(
            "file_write", {"path": str(home / "notes.txt"), "content": "x"},
        )
        assert other.needs_approval

    def test_explicit_protected_path_inside_workspace_still_asks(
        self, tmp_path: Path
    ) -> None:
        protected = tmp_path / "home" / "workspace" / "protected"
        runtime, _home, _workspace = self._setup(tmp_path, extra=(str(protected),))

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": str(protected / "c.txt"), "content": "x"},
        )

        assert verdict.needs_approval

    def test_scope_uses_normalized_path(self, tmp_path: Path) -> None:
        runtime, home, workspace = self._setup(tmp_path)
        link = workspace / "skills-link"
        try:
            os.symlink(home / "skills", link)
        except OSError:
            pytest.skip("当前环境不支持符号链接")

        via_link = runtime.evaluate_tool_call(
            "file_write", {"path": str(link / "demo" / "SKILL.md"), "content": "x"},
        )
        direct = runtime.evaluate_tool_call(
            "file_write",
            {"path": str(home / "skills" / "demo" / "SKILL.md"), "content": "x"},
        )

        assert via_link.needs_approval and direct.needs_approval
        assert via_link.scope == direct.scope


class TestMcpLifecycle:
    def test_handshake_failure_does_not_leak_process(self, tmp_path: Path) -> None:
        silent = tmp_path / "silent.py"
        silent.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        client = mcp_client.StdioMcpClient(
            "python3",
            [str(silent)],
            timeout=0.5,
        )
        before = len(mcp_client._live_clients)
        try:
            with pytest.raises(Exception):
                client.start()
        finally:
            client.close()

        assert len(mcp_client._live_clients) == before

    def test_setup_returns_teardown_for_live_servers(self, tmp_path: Path) -> None:
        from tests.test_mcp_client import FAKE_SERVER

        server = tmp_path / "fake.py"
        server.write_text(FAKE_SERVER, encoding="utf-8")
        config = {
            "mcpServers": {"fake": {"command": "python3", "args": [str(server)]}}
        }
        (tmp_path / "mcp.json").write_text(
            __import__("json").dumps(config), encoding="utf-8"
        )
        runtime = _runtime(tmp_path)
        before = len(mcp_client._live_clients)

        teardown = mcp_client.setup(ExtensionContext("mcp_client", runtime))

        assert callable(teardown)
        assert runtime.registry.get("mcp__fake__echo") is not None
        teardown()
        # 断言相对变化，避免被其它用例的残留状态影响
        assert len(mcp_client._live_clients) == before


class TestEnabledSemantics:
    """extensions.enabled=false 只关闭插件与能力扩展，安全门仍生效."""

    def _agent(self, tmp_path: Path, enabled: bool):
        from zero_agent.core.agent import ZeroAgent
        from zero_agent.core.config import AgentConfig, LLMBackendConfig

        home = tmp_path / "home"
        (home / "extensions").mkdir(parents=True)
        (home / "extensions" / "hello.py").write_text(
            "def setup(ctx):\n"
            "    @ctx.tool(name='hello_ext', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def hello(args):\n"
            "        return 'hi'\n",
            encoding="utf-8",
        )
        config = AgentConfig(
            llm_backends={
                "d": LLMBackendConfig(
                    name="d",
                    provider="openai",
                    api_key="k",
                    api_base="https://api.openai.com/v1",
                    model="gpt-4o",
                )
            },
            default_backend="d",
            workspace_dir=str(tmp_path / "ws"),
            memory_dir=str(tmp_path / "mem"),
            sessions_dir=str(tmp_path / "sessions"),
            extensions_home=str(home),
            extensions_enabled=enabled,
        )
        return ZeroAgent(config)

    def test_disabled_skips_plugins_but_keeps_safety_gate(
        self, tmp_path: Path
    ) -> None:
        agent = self._agent(tmp_path, enabled=False)

        assert "hello_ext" not in [tool.name for tool in agent.registry.list_all()]
        assert agent.extensions is not None
        # code_run 的真实参数是 script，历史代码也接受 code，两种都要拦
        for args in ({"code": "rm -rf /"}, {"script": "rm -rf /"}):
            assert agent.extensions.run_tool_call("code_run", args) is not None, args

    def test_enabled_loads_plugins(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path, enabled=True)

        assert "hello_ext" in [tool.name for tool in agent.registry.list_all()]


class TestCustomHandlerRegistry:
    """自定义 handler 自带 registry 时，扩展必须注册进最终生效的那个 registry."""

    def test_extension_tools_follow_handler_registry(self, tmp_path: Path) -> None:
        from zero_agent.core.agent import ZeroAgent
        from zero_agent.core.config import AgentConfig, LLMBackendConfig
        from zero_agent.core.handler import BaseHandler

        home = tmp_path / "home"
        (home / "extensions").mkdir(parents=True)
        (home / "extensions" / "hello.py").write_text(
            "def setup(ctx):\n"
            "    @ctx.tool(name='hello_ext', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def hello(args):\n"
            "        return 'hi'\n",
            encoding="utf-8",
        )
        config = AgentConfig(
            llm_backends={
                "d": LLMBackendConfig(
                    name="d",
                    provider="openai",
                    api_key="k",
                    api_base="https://api.openai.com/v1",
                    model="gpt-4o",
                )
            },
            default_backend="d",
            workspace_dir=str(tmp_path / "ws"),
            memory_dir=str(tmp_path / "mem"),
            sessions_dir=str(tmp_path / "sessions"),
            extensions_home=str(home),
        )
        own_registry = ToolRegistry()
        own_registry.register(_tool("only_tool"))
        handler = BaseHandler(registry=own_registry, cwd=str(tmp_path / "ws"))

        agent = ZeroAgent(config=config, handler=handler)

        assert agent.registry is own_registry
        assert own_registry.get("hello_ext") is not None
        assert own_registry.get("skill_load") is not None


class TestNotifyDispatch:
    """通知型事件必须由 hook 生命周期真正触发（此前 notify() 无调用者）."""

    def test_hook_events_map_to_notify(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        seen: list = []
        runtime.subscribe_notify("agent_start", "p", lambda payload: seen.append(payload))
        runtime.subscribe_notify("turn_end", "p", lambda payload: seen.append(payload))

        runtime.dispatch_hook_notify("agent_before", {"turn": 1})
        runtime.dispatch_hook_notify("turn_after", {"turn": 2})
        runtime.dispatch_hook_notify("tool_before", {"ignored": True})

        assert seen == [{"turn": 1}, {"turn": 2}]

    def test_payload_is_copied_per_callback(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        seen: list = []
        runtime.subscribe_notify(
            "turn_start", "a", lambda payload: payload.update({"polluted": True}),
        )
        runtime.subscribe_notify(
            "turn_start", "b", lambda payload: seen.append(dict(payload)),
        )

        runtime.dispatch_hook_notify("turn_before", {"turn": 1})

        assert seen == [{"turn": 1}]

    def test_ctx_llm_after_receives_event(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        seen: list = []
        ExtensionContext("p", runtime).on_llm_after(
            lambda payload: seen.append(payload),
        )

        runtime.dispatch_hook_notify("llm_after", {"turn": 1})

        assert seen == [{"turn": 1}]


class TestGateFailClosed:
    """门控无法识别的返回值必须按 fail-closed 拒绝."""

    def test_unknown_return_value_blocks(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.subscribe_tool_call("g", lambda call: "deny")

        assert runtime.evaluate_tool_call("t", {}).is_block

    def test_none_still_allows(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.subscribe_tool_call("g", lambda call: None)

        assert runtime.evaluate_tool_call("t", {}).is_allow

    def test_false_still_blocks(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.subscribe_tool_call("g", lambda call: False)

        assert runtime.evaluate_tool_call("t", {}).is_block


class TestApprovalTaskScope:
    def _fingerprint(self, tmp_path: Path) -> str:
        from zero_agent.extensions.runtime import approval_fingerprint

        return approval_fingerprint("t", {"path": "x"})

    def test_granted_for_task_not_consumed_by_empty_task(
        self, tmp_path: Path,
    ) -> None:
        runtime = _runtime(tmp_path)
        fingerprint = self._fingerprint(tmp_path)
        runtime.grant_approval(fingerprint, task_id="task-A")

        assert runtime.consume_approval(fingerprint, task_id="") is False

    def test_granted_for_task_consumed_by_same_task(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        fingerprint = self._fingerprint(tmp_path)
        runtime.grant_approval(fingerprint, task_id="task-A")

        assert runtime.consume_approval(fingerprint, task_id="task-A") is True

    def test_global_grant_still_consumed_by_any_task(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        fingerprint = self._fingerprint(tmp_path)
        runtime.grant_approval(fingerprint, task_id="")

        assert runtime.consume_approval(fingerprint, task_id="task-A") is True


class TestReleaseCleansBuiltinOwners:
    def test_release_removes_tracked_builtin_registrations(
        self, tmp_path: Path,
    ) -> None:
        runtime = _runtime(tmp_path)
        context = ExtensionContext("permission_gate", runtime)

        @context.tool(
            name="inner_tool",
            description="d",
            parameters={"type": "object", "properties": {}},
        )
        def inner(args):  # noqa: ANN001 - 测试用最小 handler
            return "x"

        runtime.track_owner("permission_gate")
        runtime.release()

        assert runtime.registry.get("inner_tool") is None
        assert runtime.registry.owner_of("inner_tool") is None

    def test_unregister_owner_cleans_half_installed_extension(
        self, tmp_path: Path,
    ) -> None:
        runtime = _runtime(tmp_path)
        runtime.register_tool("partial", _tool("partial_tool", "partial"))
        runtime.track_owner("partial")

        runtime.unregister_owner("partial")

        assert runtime.registry.get("partial_tool") is None
        assert "partial" not in runtime._builtin_owners


class TestPromptBlockCap:
    def test_oversized_block_is_truncated(self, tmp_path: Path) -> None:
        from zero_agent.extensions import runtime as runtime_mod

        runtime = _runtime(tmp_path)
        ExtensionContext("p", runtime).prompt_block("huge", lambda: "x" * 100_000)

        blocks = runtime.collect_prompt_blocks()

        assert blocks
        assert len(blocks[0].text) <= runtime_mod._MAX_PROMPT_BLOCK_CHARS + 10
        assert blocks[0].text.endswith("…（已截断）")

    def test_normal_block_is_untouched(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        ExtensionContext("p", runtime).prompt_block("small", lambda: "hello")

        blocks = runtime.collect_prompt_blocks()

        assert blocks[0].text == "hello"


class TestDiscoverFailureIsolation:
    def test_discover_failure_does_not_block_startup(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        from zero_agent.extensions import runtime as runtime_mod

        def boom(home):  # noqa: ANN001 - 模拟不可读的扩展目录
            raise PermissionError("unreadable")

        monkeypatch.setattr(runtime_mod, "discover", boom)
        runtime = _runtime(tmp_path)

        assert runtime.load_all() == 0


class TestRegistryDeepOverride:
    def test_three_layer_release_restores_baseline(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "builtin"))
        for owner in ("A", "B", "C"):
            registry.register(_tool("t", owner), owner=owner)

        for owner in ("A", "B", "C"):
            registry.release_owner(owner)

        assert registry.get("t").description == "builtin"
        assert registry.owner_of("t") is None

    def test_releasing_middle_layer_keeps_top_definition(self) -> None:
        registry = _registry()
        registry.register(_tool("t", "builtin"))
        for owner in ("A", "B", "C"):
            registry.register(_tool("t", owner), owner=owner)

        registry.release_owner("B")
        assert registry.get("t").description == "C"

        registry.release_owner("A")
        assert registry.get("t").description == "C"

        registry.release_owner("C")
        assert registry.get("t").description == "builtin"


class TestAgentClose:
    """ZeroAgent.close() 必须真正撤销扩展注册，且可重复调用."""

    def _agent(self, tmp_path: Path):
        from zero_agent.core.agent import ZeroAgent
        from zero_agent.core.config import AgentConfig, LLMBackendConfig

        home = tmp_path / "home"
        (home / "extensions").mkdir(parents=True)
        (home / "extensions" / "hello.py").write_text(
            "def setup(ctx):\n"
            "    @ctx.tool(name='hello_ext', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def hello(args):\n"
            "        return 'hi'\n",
            encoding="utf-8",
        )
        config = AgentConfig(
            llm_backends={
                "d": LLMBackendConfig(
                    name="d",
                    provider="openai",
                    api_key="k",
                    api_base="https://api.openai.com/v1",
                    model="gpt-4o",
                )
            },
            default_backend="d",
            workspace_dir=str(tmp_path / "ws"),
            memory_dir=str(tmp_path / "mem"),
            sessions_dir=str(tmp_path / "sessions"),
            extensions_home=str(home),
        )
        return ZeroAgent(config)

    def test_close_releases_registrations_and_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        agent = self._agent(tmp_path)
        runtime = agent.extensions
        assert runtime is not None
        assert agent.registry.get("hello_ext") is not None
        assert agent.registry.get("skill_load") is not None

        agent.close()

        assert agent.extensions is None
        assert agent.registry.get("hello_ext") is None
        assert agent.registry.get("skill_load") is None
        assert runtime.loaded_names == []
        assert runtime._tool_call == []

        agent.close()
        assert agent.extensions is None
