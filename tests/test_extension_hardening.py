"""扩展运行时硬化回归：门控漏判、内部工具误门控、路径与 shell 绕过.

这些用例针对审阅确认过的真实缺陷，每条都能在修复前复现：

- ``code_run`` 参数为空时由回复代码块执行 → 门控放行（黑名单形同虚设）.
- 循环内部信号 ``bad_json`` 与未注册工具名被当成 unknown 副作用 → 弹审批.
- 浏览器工具的可选写入目标缺失 → 被判「写入目标为空」而永久阻断.
- macOS 大小写不敏感文件系统上 ``~/.ZEROAGENT/mcp.json`` 绕过保护.
- ``\\rm``、``sh -c``、``eval``、``find -exec``、``pushd``/``env -C`` 绕过删除判定.
- ``script_path`` 门控读的文件与 ``code_run`` 执行的文件不是同一个.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepAction, TaskContract, TaskMode
from zero_agent.extensions.builtin import mcp_client, path_policy, permission_gate
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
    )


def _builtin_runtime(tmp_path: Path) -> ExtensionRuntime:
    """带内置工具与 permission_gate 的运行时."""
    config = _config(tmp_path)
    registry = ToolRegistry.with_builtins(config)
    runtime = ExtensionRuntime(
        registry=registry, home=tmp_path / "home", config=config,
    )
    permission_gate.setup(ExtensionContext("permission_gate", runtime))
    return runtime


def _fenced(text: str) -> MockResponse:
    return MockResponse(content=text)


class TestExecuteGateSeesReplyCode:
    """``code_run`` 的回复代码块路径必须与 ``script`` 参数同等判定."""

    def test_missing_code_is_left_to_tool_validation(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        # 参数为空且没有围栏代码块：这次调用不会执行任何代码，交给 code_run
        # 自己报参数错误（code_run_missing_input），门控不伪造安全结论
        verdict = runtime.evaluate_tool_call("code_run", {"type": "bash"})

        assert verdict.is_allow

    def test_reply_block_dangerous_command_is_blocked(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        verdict = runtime.evaluate_tool_call(
            "code_run",
            {"type": "bash"},
            response=_fenced("```bash\nrm -rf /\n```"),
        )

        assert verdict.is_block

    def test_reply_block_safe_command_is_allowed(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        verdict = runtime.evaluate_tool_call(
            "code_run",
            {"type": "bash"},
            response=_fenced("```bash\nls -la\n```"),
        )

        assert verdict.is_allow

    def test_decoy_argument_does_not_hide_reply_block(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        # code_run 不读 "code" 参数：诱饵参数不能当作「已检查」的凭证，
        # 真正会执行的回复代码块仍要判定
        verdict = runtime.evaluate_tool_call(
            "code_run",
            {"code": "ls"},
            response=_fenced("```bash\nrm -rf /\n```"),
        )

        assert verdict.is_block

    def test_explicit_script_wins_over_reply_block(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        # script 存在时 handler 只执行 script，回复代码块不会被执行
        verdict = runtime.evaluate_tool_call(
            "code_run",
            {"script": "ls -la"},
            response=_fenced("```bash\nrm -rf /\n```"),
        )

        assert verdict.is_allow


class TestScriptPathMatchesExecution:
    """门控必须读 ``code_run`` 真正会执行的那个文件."""

    def test_workspace_relative_script_is_read(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        workspace = Path(runtime.config.workspace_dir)
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "x.py").write_text(
            "rm -rf /\n", encoding="utf-8",
        )

        # memory/x.py 在门控里曾被按文件工具口径重定向到 <memory_dir>/x.py，
        # 与实际执行的 <workspace>/memory/x.py 不是同一个文件
        verdict = runtime.evaluate_tool_call(
            "code_run", {"type": "shell", "script_path": "memory/x.py"},
        )

        assert verdict.is_block
        assert "递归" in verdict.reason

    def test_escape_outside_workspace_is_blocked(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        verdict = runtime.evaluate_tool_call(
            "code_run", {"script_path": "../../etc/passwd"},
        )

        assert verdict.is_block

    def test_unreadable_script_is_blocked(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)

        verdict = runtime.evaluate_tool_call(
            "code_run", {"script_path": "missing.py"},
        )

        assert verdict.is_block
        assert "读取" in verdict.reason

    def test_oversized_script_is_blocked(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        workspace = Path(runtime.config.workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "huge.py").write_text(
            "print(1)\n" + "x = 1\n" * 40_000, encoding="utf-8",
        )

        verdict = runtime.evaluate_tool_call(
            "code_run", {"script_path": "huge.py"},
        )

        assert verdict.is_block
        assert "超过" in verdict.reason


class TestShellBypasses:
    """带引号、转义、嵌套 shell、命令替换、find -exec 的删除都要被判到."""

    @pytest.mark.parametrize(
        "code",
        [
            "\\rm -rf /",
            '"rm" -rf /',
            "r''m -rf /",
            "sh -c 'rm -rf /'",
            'bash -lc "rm -rf /"',
            "eval 'rm -rf /'",
            "echo $(rm -rf /tmp/x)",
            "echo `rm -rf /tmp/x`",
            "find / -exec rm -rf {} +",
            "pushd /tmp && rm -rf build",
            "env -C /tmp rm -rf build",
        ],
    )
    def test_bypass_is_blocked(self, tmp_path: Path, code: str) -> None:
        runtime = _builtin_runtime(tmp_path)

        assert runtime.evaluate_tool_call("code_run", {"script": code}).is_block

    @pytest.mark.parametrize(
        "code",
        [
            "rm -rf build",
            "rm -f a.txt",
            "ls -la",
            "echo $(date)",
            "find . -name '*.pyc' -delete",
            "cd sub && ls",
        ],
    )
    def test_legitimate_command_is_allowed(self, tmp_path: Path, code: str) -> None:
        runtime = _builtin_runtime(tmp_path)

        assert runtime.evaluate_tool_call("code_run", {"script": code}).is_allow


class TestInternalToolsAreNotGated:
    """循环内部信号与未知工具不应弹审批."""

    def _handler(self, runtime: ExtensionRuntime):
        from zero_agent.core.handler import BaseHandler

        class Parent:
            extensions = runtime
            loop = None
            config = None

        handler = BaseHandler(registry=runtime.registry, cwd=".")
        handler.parent = Parent()
        handler.task_contract = TaskContract(
            task_id="t1", user_request="do", mode=TaskMode.OPEN,
        )
        return handler

    @staticmethod
    def _dispatch(handler, tool_name: str, args: dict):
        generator = handler.dispatch(tool_name, args, None)
        try:
            while True:
                next(generator)
        except StopIteration as stop:
            return stop.value

    @pytest.mark.parametrize("tool_name", ["bad_json", "hallucinated_tool"])
    def test_internal_names_do_not_pause(
        self, tmp_path: Path, tool_name: str,
    ) -> None:
        runtime = _builtin_runtime(tmp_path)
        handler = self._handler(runtime)

        outcome = self._dispatch(handler, tool_name, {"msg": "bad"})

        assert outcome.action is not StepAction.WAIT_FOR_USER
        assert outcome.action is StepAction.CONTINUE

    def test_unknown_effect_tool_still_asks(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        runtime.registry.register(ToolDefinition(
            name="mystery",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, response, handler: None,
        ))
        handler = self._handler(runtime)

        outcome = self._dispatch(handler, "mystery", {})

        assert outcome.action is StepAction.WAIT_FOR_USER


class TestOptionalWriteTargets:
    """声明了 optional_write_paths 的工具，缺省写入目标不再被阻断."""

    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("web_screenshot", {}),
            ("web_execute_js", {"script": "return 1"}),
        ],
    )
    def test_browser_tools_allow_default_target(
        self, tmp_path: Path, tool_name: str, args: dict,
    ) -> None:
        runtime = _builtin_runtime(tmp_path)

        assert runtime.evaluate_tool_call(tool_name, dict(args)).is_allow

    def test_write_tool_without_targets_still_blocked(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        extensions_dir = Path(runtime.home) / "extensions"
        extensions_dir.mkdir(parents=True, exist_ok=True)
        (extensions_dir / "writer.py").write_text(
            "def setup(ctx):\n"
            "    @ctx.tool(name='writer', description='d',\n"
            "              parameters={'type': 'object', 'properties': {}},\n"
            "              effects=('write',), write_path_keys=('path',))\n"
            "    def writer(args):\n"
            "        return 'ok'\n",
            encoding="utf-8",
        )
        runtime.load_all()

        verdict = runtime.evaluate_tool_call("writer", {})

        assert verdict.is_block
        assert "写入目标为空" in verdict.reason

    def test_optional_write_still_checks_declared_target(
        self, tmp_path: Path,
    ) -> None:
        runtime = _builtin_runtime(tmp_path)
        protected = Path(runtime.home) / "skills" / "demo" / "SKILL.md"

        verdict = runtime.evaluate_tool_call(
            "web_screenshot", {"save_to_file": str(protected)},
        )

        assert verdict.needs_approval


class TestCaseInsensitiveProtectedPaths:
    """大小写不敏感文件系统上，改大小写不能绕过受保护前缀."""

    @pytest.mark.skipif(
        not path_policy.is_case_insensitive_fs(),
        reason="当前平台文件系统大小写敏感",
    )
    def test_uppercase_variant_still_protected(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        protected_dir = Path(runtime.home) / "skills"
        protected_dir.mkdir(parents=True, exist_ok=True)
        variant = str(Path(runtime.home) / "SKILLS" / "x.md")
        if not os.path.isdir(str(protected_dir.parent / "SKILLS")):
            pytest.skip("文件系统未按预期折叠大小写")

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": variant, "content": "x"},
        )

        assert verdict.needs_approval


class TestApprovalCandidates:
    """门控给的快捷选项必须透传到暂停数据."""

    def test_candidates_reach_payload(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        extensions_dir = Path(runtime.home) / "extensions"
        extensions_dir.mkdir(parents=True, exist_ok=True)
        (extensions_dir / "ask.py").write_text(
            "def setup(ctx):\n"
            "    @ctx.on_tool_call\n"
            "    def guard(call):\n"
            "        if call.tool_name == 'danger':\n"
            "            return ctx.confirm('sure?', candidates=['A', 'B'])\n"
            "    @ctx.tool(name='danger', description='d',\n"
            "              parameters={'type': 'object', 'properties': {}},\n"
            "              effects=('read',))\n"
            "    def danger(args):\n"
            "        return 'x'\n",
            encoding="utf-8",
        )
        runtime.load_all()

        from zero_agent.core.handler import BaseHandler

        class Parent:
            extensions = runtime
            loop = None
            config = None

        handler = BaseHandler(registry=runtime.registry, cwd=".")
        handler.parent = Parent()
        handler.task_contract = TaskContract(
            task_id="t1", user_request="do", mode=TaskMode.OPEN,
        )
        generator = handler.dispatch("danger", {}, None)
        try:
            while True:
                next(generator)
        except StopIteration as stop:
            outcome = stop.value

        assert outcome.action is StepAction.WAIT_FOR_USER
        assert outcome.data["data"]["candidates"] == ["A", "B"]


class TestNotifyPayloadIsolation:
    """通知是只观察契约：回调改负载不能污染真实对象."""

    def test_callback_mutation_does_not_leak(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        messages = [{"role": "user", "content": "hi"}]
        args = {"path": "a.txt"}
        handler = object()

        def mutate(payload):
            payload["messages"].append({"role": "user", "content": "injected"})
            payload["args"]["path"] = "hacked"
            payload["handler"] = None

        runtime.subscribe_notify("llm_before", "probe", mutate)
        runtime.notify("llm_before", {
            "messages": messages,
            "args": args,
            "handler": handler,
        })

        assert messages == [{"role": "user", "content": "hi"}]
        assert args == {"path": "a.txt"}

    def test_handler_reference_is_shared(self, tmp_path: Path) -> None:
        runtime = _builtin_runtime(tmp_path)
        seen = []
        handler = object()

        runtime.subscribe_notify("tool_before", "probe", seen.append)
        runtime.notify("tool_before", {"handler": handler})

        assert seen[0]["handler"] is handler


class TestMcpRegistrationCleanup:
    """注册循环中途失败时必须回收已经启动的子进程."""

    def test_client_closed_when_registration_raises(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        closed = []

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                return None

            def list_tools(self):
                return [{"name": "one"}, {"name": "two"}]

            def close(self):
                closed.append(True)

            def call_tool(self, name, arguments):
                return "ok"

        monkeypatch.setattr(mcp_client, "StdioMcpClient", FakeClient)

        class FailingCtx:
            def tool(self, **kwargs):
                raise RuntimeError("registry exploded")

        with pytest.raises(RuntimeError):
            mcp_client._register_server(
                FailingCtx(), "server", {"command": "fake"}, set(),
            )

        assert closed == [True]


class TestConfigValueNormalisation:
    """YAML 里的字符串/布尔误写法不能被静默当成 True."""

    def test_string_false_disables_extensions(self) -> None:
        config = AgentConfig._from_dict({
            "extensions": {"enabled": "false"},
        })

        assert config.extensions_enabled is False

    def test_single_protected_write_string_is_not_split(self) -> None:
        config = AgentConfig._from_dict({
            "extensions": {"protected_writes": "~/secrets"},
        })

        assert config.extensions_protected_writes == ["~/secrets"]

    def test_yaml_off_becomes_boolean_false_but_stays_off(self) -> None:
        # PyYAML 按 YAML 1.1 把 off 解析成 False
        config = AgentConfig._from_dict({"extensions": {"sandbox": False}})

        assert config.sandbox_mode == "off"

    def test_boolean_true_sandbox_falls_back_to_off(self) -> None:
        config = AgentConfig._from_dict({"extensions": {"sandbox": True}})

        assert config.sandbox_mode == "off"


class TestPathPolicyScopes:
    """``/foo`` 不能匹配 ``/foobar``，软链要按真实目标判定."""

    def test_sibling_prefix_is_not_inside(self, tmp_path: Path) -> None:
        root = tmp_path / "vault"
        sibling = tmp_path / "vault-evil"
        root.mkdir()
        sibling.mkdir()

        assert path_policy.inside(str(sibling / "x"), str(root)) is False

    def test_symlink_resolves_to_target(self, tmp_path: Path) -> None:
        protected = tmp_path / "protected"
        protected.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(protected, link)
        except OSError:
            pytest.skip("当前环境不支持符号链接")

        assert path_policy.under(
            str(link / "x.md"), (str(protected),), None,
        ) == str(protected)


class TestLinuxSandboxBindsTempDir:
    """Linux 沙箱必须把临时目录绑成可写，否则脚本落盘后命令必然失败."""

    def test_wrap_binds_temp_dirs(self, tmp_path: Path, monkeypatch) -> None:
        from zero_agent.extensions import sandbox

        monkeypatch.setattr(sandbox, "_linux_available", lambda: True)
        monkeypatch.setattr(sys, "platform", "linux")

        argv, wrapped, reason = sandbox.wrap_command(
            ["/bin/true"], str(tmp_path), "linux",
        )

        assert wrapped is True
        assert reason == ""
        assert "--bind" in argv
        assert argv.count("--bind") >= 1
        assert str(tmp_path.resolve()) in argv
