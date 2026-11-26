"""全权限模式：full 绕过全部扩展门控，ask 保留审批与拦截."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from zero_agent.core.config import AgentConfig
from zero_agent.extensions.builtin import permission_gate
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.events import ToolCall
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _noop(args, response, handler):
    yield ""
    return None


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name, effects, write_keys, code_keys in (
        ("file_write", ("write",), ("path",), ()),
        ("code_run", ("execute",), (), ("script",)),
    ):
        registry.register(ToolDefinition(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}},
            handler=_noop,
            effects=effects,
            write_path_keys=write_keys,
            execute_code_keys=code_keys,
        ))
    return registry


def _runtime(home: Path, mode: str) -> ExtensionRuntime:
    return ExtensionRuntime(
        registry=_registry(),
        home=home,
        config=SimpleNamespace(permission_mode=mode),
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_agent_config_defaults_to_full() -> None:
    assert AgentConfig().permission_mode == "full"


def test_full_mode_allows_protected_write_and_dangerous_shell(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "full")
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    target = str(tmp_path / "skills" / "x" / "SKILL.md")

    assert runtime.evaluate_tool_call(
        "file_write", {"path": target, "content": "x"},
    ).is_allow
    assert runtime.evaluate_tool_call("code_run", {"script": "rm -rf /"}).is_allow


def test_full_mode_bypasses_third_party_block(tmp_path: Path) -> None:
    _write(
        tmp_path / "extensions" / "blocker.py",
        (
            "def setup(ctx):\n"
            "    @ctx.on_tool_call\n"
            "    def guard(call):\n"
            "        return ctx.block('no')\n"
        ),
    )
    runtime = _runtime(tmp_path, "full")
    runtime.load_all()

    verdict = runtime.evaluate_tool_call("file_write", {"path": "x", "content": "y"})

    assert verdict.is_allow


def test_ask_mode_keeps_approval_and_block(tmp_path: Path) -> None:
    _write(
        tmp_path / "extensions" / "blocker.py",
        (
            "def setup(ctx):\n"
            "    @ctx.on_tool_call\n"
            "    def guard(call):\n"
            "        return ctx.block('no')\n"
        ),
    )
    runtime = _runtime(tmp_path, "ask")
    runtime.load_all()

    assert runtime.evaluate_tool_call("code_run", {"script": "rm -rf /"}).is_block
    assert runtime.evaluate_tool_call(
        "file_write", {"path": "x", "content": "y"},
    ).is_block


def test_ask_mode_protected_write_needs_approval(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "ask")
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    target = str(tmp_path / "skills" / "x" / "SKILL.md")

    verdict = runtime.evaluate_tool_call(
        "file_write", {"path": target, "content": "x"},
    )

    assert verdict.needs_approval


def test_permission_gate_guard_is_noop_in_full_mode(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "full")
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    _owner, guard = runtime._tool_call[-1]
    call = ToolCall(
        tool_name="code_run",
        args={"script": "rm -rf /"},
        owner="",
        definition=runtime.registry.get("code_run"),
    )

    assert guard(call) is None
