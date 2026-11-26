"""全权限模式：full 免去人类审批，但确定性拦截仍然生效."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from zero_agent.core.config import AgentConfig, normalize_permission_mode
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
        ("code_run", ("execute",), ("write",), ("script",)),
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


def _runtime(home: Path, mode: str | None) -> ExtensionRuntime:
    config = None if mode is None else SimpleNamespace(permission_mode=mode)
    return ExtensionRuntime(registry=_registry(), home=home, config=config)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_agent_config_defaults_to_full() -> None:
    assert AgentConfig().permission_mode == "full"


def test_normalize_permission_mode_falls_back_to_ask() -> None:
    assert normalize_permission_mode("FULL") == "full"
    assert normalize_permission_mode(" ask ") == "ask"
    assert normalize_permission_mode("fulll") == "ask"
    assert normalize_permission_mode(None, default="ask") == "ask"


def test_full_mode_skips_protected_write_approval(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "full")
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    target = str(tmp_path / "skills" / "x" / "SKILL.md")

    verdict = runtime.evaluate_tool_call("file_write", {"path": target, "content": "x"})

    assert verdict.is_allow


def test_full_mode_keeps_dangerous_shell_block(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "full")
    permission_gate.setup(ExtensionContext("permission_gate", runtime))

    verdict = runtime.evaluate_tool_call("code_run", {"script": "rm -rf /"})

    assert verdict.is_block


def test_full_mode_keeps_missing_declaration_block(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="opaque_run",
        description="",
        parameters={"type": "object", "properties": {}},
        handler=_noop,
        effects=("execute",),
    ))
    runtime = ExtensionRuntime(
        registry=registry,
        home=tmp_path,
        config=SimpleNamespace(permission_mode="full"),
    )
    permission_gate.setup(ExtensionContext("permission_gate", runtime))

    assert runtime.evaluate_tool_call("opaque_run", {}).is_block


def test_full_mode_keeps_third_party_veto(tmp_path: Path) -> None:
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

    assert verdict.is_block


def test_full_mode_downgrades_third_party_approval(tmp_path: Path) -> None:
    _write(
        tmp_path / "extensions" / "asker.py",
        (
            "def setup(ctx):\n"
            "    @ctx.on_tool_call\n"
            "    def guard(call):\n"
            "        return ctx.confirm('ok?')\n"
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


def test_runtime_without_config_defaults_to_ask(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, None)
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    target = str(tmp_path / "skills" / "x" / "SKILL.md")

    assert runtime.permission_mode == "ask"
    assert runtime.evaluate_tool_call(
        "file_write", {"path": target, "content": "x"},
    ).needs_approval


def test_gate_callback_still_reports_approval_in_full_mode(tmp_path: Path) -> None:
    # 模式开关只在运行时：门控本身仍然给出 confirm，便于审计与测试
    runtime = _runtime(tmp_path, "full")
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    _owner, guard = runtime._tool_call[-1]
    call = ToolCall(
        tool_name="file_write",
        args={"path": str(tmp_path / "skills" / "a.md"), "content": "x"},
        owner="",
        definition=runtime.registry.get("file_write"),
    )

    assert guard(call) is not None
