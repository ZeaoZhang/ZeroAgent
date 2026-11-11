"""针对审阅发现的回归测试：注册归属、半注册清理、门控集成、路径判定."""

from __future__ import annotations

import os
from pathlib import Path

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

        class Stub:
            extensions = runtime
            registry = new_registry
            loop = None

        ZeroAgent._replay_extension_registrations(Stub())

        assert new_registry.get("echo_tool") is not None
        assert runtime.registry is new_registry


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

    def test_symlink_into_protected_dir_is_blocked(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path)
        protected = Path(runtime.home) / "skills"
        protected.mkdir(parents=True, exist_ok=True)
        link = tmp_path / "link-to-skills"
        try:
            os.symlink(protected, link)
        except OSError:
            pytest.skip("当前环境不支持符号链接")

        reason = runtime.run_tool_call(
            "file_write", {"path": str(link / "demo" / "SKILL.md")}
        )
        assert reason is not None

    def test_empty_path_is_blocked(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path)
        assert runtime.run_tool_call("file_write", {"path": ""}) is not None


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

        teardown = mcp_client.setup(ExtensionContext("mcp_client", runtime))

        assert callable(teardown)
        assert runtime.registry.get("mcp__fake__echo") is not None
        teardown()
        assert len(mcp_client._live_clients) == 0
