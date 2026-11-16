"""扩展运行时测试：发现、加载、三类扩展点语义与注册归属."""

from __future__ import annotations

from pathlib import Path

import pytest

from zero_agent.extensions.builtin import permission_gate, skills_index
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.events import ExtensionVeto, LlmRequest
from zero_agent.extensions.runtime import ExtensionRuntime, discover
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _make_registry() -> ToolRegistry:
    """构造一个空注册表，测试不依赖内置工具."""
    return ToolRegistry()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _runtime(home: Path, registry: ToolRegistry | None = None) -> ExtensionRuntime:
    return ExtensionRuntime(registry=registry or _make_registry(), home=home)


def _tool_result(handler, args: dict):
    """消费工具 handler generator，返回其 return 值."""
    generator = handler(args, None, None)
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


class TestDiscover:
    def test_only_one_level_and_skips_private(self, tmp_path: Path) -> None:
        _write(tmp_path / "extensions" / "single.py", "def setup(ctx):\n    pass\n")
        _write(tmp_path / "extensions" / "pkg" / "__init__.py", "def setup(ctx):\n    pass\n")
        _write(tmp_path / "extensions" / "_hidden.py", "def setup(ctx):\n    pass\n")
        _write(tmp_path / "extensions" / "_shared" / "lib.py", "x = 1\n")
        _write(tmp_path / "extensions" / "noinit" / "other.py", "x = 1\n")

        found = discover(tmp_path)
        names = {name for name, _path, _is_pkg in found}

        assert names == {"single", "pkg"}
        assert all(is_pkg for name, _path, is_pkg in found if name == "pkg")

    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        assert discover(tmp_path) == []


class TestLoading:
    def test_plugin_registers_tool(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "hello.py",
            (
                "def setup(ctx):\n"
                "    @ctx.tool(name='hello', description='打招呼', parameters={'type': 'object', 'properties': {}})\n"
                "    def hello(args):\n"
                "        return 'hi'\n"
            ),
        )
        registry = _make_registry()
        runtime = _runtime(tmp_path, registry)

        assert runtime.load_all() == 1
        tool = registry.get("hello")
        assert tool is not None
        assert registry.owner_of("hello") == "hello"

    def test_broken_plugin_is_skipped(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "broken.py",
            "def setup(ctx):\n    raise RuntimeError('boom')\n",
        )
        _write(tmp_path / "extensions" / "ok.py", "def setup(ctx):\n    pass\n")
        runtime = _runtime(tmp_path)

        assert runtime.load_all() == 1
        assert runtime.loaded_names == ["ok"]

    def test_plugin_without_setup_is_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path / "extensions" / "nosetup.py", "VALUE = 1\n")
        runtime = _runtime(tmp_path)

        assert runtime.load_all() == 0


class TestToolRegistryOwnership:
    def test_release_restores_previous_definition(self) -> None:
        registry = _make_registry()
        registry.register(ToolDefinition(
            name="shared",
            description="builtin",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, response, handler: None,
            evidence_kind="read",
        ))

        override = ToolDefinition(
            name="shared",
            description="plugin",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, response, handler: None,
            evidence_kind="read",
        )
        registry.register(override, owner="demo")

        assert registry.get("shared").description == "plugin"
        assert registry.release_owner("demo") == 1
        assert registry.get("shared").description == "builtin"
        assert registry.owner_of("shared") is None

    def test_release_removes_new_tool(self) -> None:
        registry = _make_registry()
        registry.register(ToolDefinition(
            name="only_plugin",
            description="plugin",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, response, handler: None,
        ), owner="demo")

        assert registry.release_owner("demo") == 1
        assert registry.get("only_plugin") is None


class TestGates:
    def test_block_returns_reason(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "gate.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        return ctx.block('no')\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        assert runtime.run_tool_call("code_run", {}) == "no"

    def test_exception_fails_closed(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "badgate.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        raise RuntimeError('gate exploded')\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        reason = runtime.run_tool_call("code_run", {})
        assert reason is not None
        assert "拒绝" in reason

    def test_gate_can_modify_args(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "rewrite.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        call.args['injected'] = True\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()
        args = {"value": 1}

        assert runtime.run_tool_call("file_read", args) is None
        assert args["injected"] is True

    def test_veto_exception_is_caught(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "veto.py",
            (
                "from zero_agent.extensions.events import ExtensionVeto\n"
                "def setup(ctx):\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        raise ExtensionVeto('raised')\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        assert runtime.run_tool_call("code_run", {}) == "raised"


class TestTransforms:
    def test_tool_result_chain(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "result.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_result\n"
                "    def first(payload):\n"
                "        payload.result = str(payload.result) + '-a'\n"
                "        return payload\n"
                "    @ctx.on_tool_result\n"
                "    def second(payload):\n"
                "        payload.result = str(payload.result) + '-b'\n"
                "        return payload\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        assert runtime.run_tool_result("file_read", {}, "x") == "x-a-b"

    def test_tool_result_error_is_ignored(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "badresult.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_result\n"
                "    def explode(payload):\n"
                "        raise RuntimeError('nope')\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        assert runtime.run_tool_result("file_read", {}, "original") == "original"

    def test_llm_request_transform(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "req.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_llm_request\n"
                "    def tag(request):\n"
                "        return request.replace(system_prompt=(request.system_prompt or '') + '|tag')\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        request = LlmRequest(model="m", messages=[], tools=[], system_prompt="base")
        updated = runtime.run_llm_request(request)

        assert updated.system_prompt == "base|tag"
        assert updated.model == "m"

    def test_prompt_blocks_are_ordered_and_empty_skipped(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "prompts.py",
            (
                "def setup(ctx):\n"
                "    ctx.prompt_block('empty', lambda: '')\n"
                "    ctx.prompt_block('body', lambda: 'hello')\n"
            ),
        )
        runtime = _runtime(tmp_path)
        runtime.load_all()

        blocks = runtime.collect_prompt_blocks()
        assert [block.name for block in blocks] == ["prompts:body"]
        assert blocks[0].text == "hello"


class TestAttachRegistry:
    def test_replays_tool_registrations(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "tool.py",
            (
                "def setup(ctx):\n"
                "    @ctx.tool(name='echo', description='d', parameters={'type': 'object', 'properties': {}})\n"
                "    def echo(args):\n"
                "        return 'ok'\n"
            ),
        )
        registry = _make_registry()
        runtime = _runtime(tmp_path, registry)
        runtime.load_all()
        assert registry.get("echo") is not None

        new_registry = _make_registry()
        runtime.attach_registry(new_registry)

        assert new_registry.get("echo") is not None
        assert new_registry.owner_of("echo") == "tool"


class TestBuiltins:
    def test_skill_index_and_load(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "demo" / "SKILL.md",
            "---\nname: demo\ndescription: 演示技能\n---\n\n正文内容\n",
        )
        runtime = _runtime(tmp_path)
        skills_index.setup(ExtensionContext("skills_index", runtime))

        blocks = runtime.collect_prompt_blocks()
        assert any("demo" in block.text for block in blocks)

        tool = runtime.registry.get("skill_load")
        assert tool is not None
        result = _tool_result(tool.handler, {"name": "demo"})
        assert "正文内容" in result

    def test_skill_load_unknown_name(self, tmp_path: Path) -> None:
        _write(tmp_path / "skills" / "demo" / "SKILL.md", "# demo\n")
        runtime = _runtime(tmp_path)
        skills_index.setup(ExtensionContext("skills_index", runtime))

        tool = runtime.registry.get("skill_load")
        result = _tool_result(tool.handler, {"name": "missing"})
        assert "未找到" in result

    def test_permission_gate_blocks_dangerous_shell(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))

        reason = runtime.run_tool_call("code_run", {"code": "rm -rf /"})
        assert reason is not None
        assert "拦截" in reason

    def test_permission_gate_allows_normal_shell(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))

        assert runtime.run_tool_call("code_run", {"code": "ls -la"}) is None

    def test_permission_gate_asks_for_protected_write(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))

        target = str(Path(runtime.home) / "skills" / "demo" / "SKILL.md")
        verdict = runtime.evaluate_tool_call("file_write", {"path": target})
        # 受保护写入必须是“等待批准”，而不是直接拦截
        assert verdict.needs_approval
        assert "是否允许" in verdict.reason
        assert verdict.fingerprint

    def test_permission_gate_allows_workspace_write(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))

        assert runtime.run_tool_call("file_write", {"path": "/tmp/notes.txt"}) is None


class TestRelease:
    def test_release_clears_everything(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "full.py",
            (
                "def setup(ctx):\n"
                "    @ctx.tool(name='plug', description='d', parameters={'type': 'object', 'properties': {}})\n"
                "    def plug(args):\n"
                "        return 'ok'\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        return ctx.block('no')\n"
                "    ctx.prompt_block('p', lambda: 'x')\n"
            ),
        )
        registry = _make_registry()
        runtime = _runtime(tmp_path, registry)
        runtime.load_all()
        assert registry.get("plug") is not None

        runtime.release()

        assert registry.get("plug") is None
        assert runtime.loaded_names == []
        assert runtime.run_tool_call("code_run", {}) is None
        assert runtime.collect_prompt_blocks() == []
