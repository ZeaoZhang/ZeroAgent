"""Tests for the ZeroAgent slash command system.

Covers dispatch, exit detection, help, error handling, command count,
CommandDef attributes, built-in descriptions, and lazy import handlers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from zero_agent.frontends.commands import COMMANDS, CommandDef
from zero_agent.frontends.commands.slash_commands import (
    _build_index,
    _register,
    handle_command,
    has_active_plan,
    is_exit_command,
    resolve_pending_plan,
    resolve_ready_plan,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_mock_agent():
    """Create a minimal mock ZeroAgent for command testing."""
    agent = MagicMock()

    # Simulate a client with all expected attributes.
    client = MagicMock()
    client.history = []
    client.system = ""
    client.name = "mock-model"
    client.temperature = 0.7
    client.max_tokens = 4096
    agent.client = client

    # Simulate _sessions dict.
    agent._sessions = {"default": client}

    # Simulate registry with list_all().
    mock_tool_a = MagicMock()
    mock_tool_a.name = "echo"
    mock_tool_a.description = "Echo back input"
    mock_tool_b = MagicMock()
    mock_tool_b.name = "run_code"
    mock_tool_b.description = "Execute code in a sandbox"
    agent.registry.list_all.return_value = [mock_tool_a, mock_tool_b]

    # Simulate handler.
    agent.handler.working = {}
    agent.handler.history_info = []
    agent.handler._empty_ct = 0

    # Simulate list_backends.
    agent.list_backends.return_value = [
        ("default", "mock-model", True),
        ("gpt4", "gpt-4-turbo", False),
    ]

    # Simulate abort.
    agent.abort.return_value = None

    # Simulate reload_config.
    agent.reload_config.return_value = True

    # Simulate _register_builtin_plugins.
    agent._register_builtin_plugins.return_value = None

    return agent


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_agent():
    """Minimal mock ZeroAgent with all command dependencies wired."""
    return _make_mock_agent()


# ── is_exit_command ────────────────────────────────────────────────────


class TestIsExitCommand:
    """Exit detection for /exit, /quit, /q, and non-exit inputs."""

    def test_slash_exit(self):
        """'/exit' is recognized as an exit command."""
        assert is_exit_command("/exit") is True

    def test_slash_quit(self):
        """'/quit' is recognized as an exit command."""
        assert is_exit_command("/quit") is True

    def test_slash_q(self):
        """'/q' is recognized as an exit command."""
        assert is_exit_command("/q") is True

    def test_slash_exit_with_extra_space(self):
        """'/exit ' with trailing whitespace still matches."""
        assert is_exit_command("/exit  ") is True

    def test_slash_exit_case_insensitive(self):
        """'/EXIT', '/Quit', '/Q' all match (lowered before check)."""
        assert is_exit_command("/EXIT") is True
        assert is_exit_command("/Quit") is True
        assert is_exit_command("/Q") is True

    def test_plain_hello_is_not_exit(self):
        """'hello' without leading slash is not an exit command."""
        assert is_exit_command("hello") is False

    def test_empty_string_is_not_exit(self):
        """Empty input is not an exit command."""
        assert is_exit_command("") is False

    def test_slash_unknown_is_not_exit(self):
        """'/help', '/model' etc. are not exit commands."""
        assert is_exit_command("/help") is False
        assert is_exit_command("/model") is False

    def test_slash_with_trailing_args(self):
        """'/exit now' — action extracted, still exit."""
        assert is_exit_command("/exit now") is True


# ── handle_command — help ─────────────────────────────────────────────


class TestHandleCommandHelp:
    """The /help command returns command listing without error."""

    def test_help_returns_non_empty(self, mock_agent):
        """'/help' returns a non-empty string listing commands."""
        result = handle_command("/help", mock_agent)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "Commands" in result

    def test_help_alias_h(self, mock_agent):
        """'/h' is an alias for /help."""
        result = handle_command("/h", mock_agent)
        assert "Commands" in result

    def test_help_alias_question(self, mock_agent):
        """'/? ' is an alias for /help."""
        result = handle_command("/?", mock_agent)
        assert "Commands" in result


# ── handle_command — tools ────────────────────────────────────────────


class TestHandleCommandTools:
    """The /tools command lists registered tools from the registry."""

    def test_tools_lists_registered_tools(self, mock_agent):
        """'/tools' returns tool names from agent.registry.list_all()."""
        result = handle_command("/tools", mock_agent)
        assert "echo" in result
        assert "run_code" in result

    def test_tools_empty_registry(self, mock_agent):
        """'/tools' returns a message when no tools are registered."""
        mock_agent.registry.list_all.return_value = []
        result = handle_command("/tools", mock_agent)
        assert "没有注册" in result


# ── handle_command — unknown ──────────────────────────────────────────


class TestHandleCommandUnknown:
    """Unrecognized commands return an error message."""

    def test_unknown_command_returns_error(self, mock_agent):
        """'/foobar' returns a message indicating unknown command."""
        result = handle_command("/foobar", mock_agent)
        assert "未知命令" in result
        assert "foobar" in result

    def test_no_slash_returns_error(self, mock_agent):
        """Input without leading '/' is rejected."""
        result = handle_command("just text", mock_agent)
        assert "不是命令" in result

    def test_leading_whitespace_still_works(self, mock_agent):
        """'  /help' with leading spaces — strip() handles it."""
        result = handle_command("  /help", mock_agent)
        assert "Commands" in result


# ── COMMANDS list ──────────────────────────────────────────────────────


class TestCommandsList:
    """The global COMMANDS list has at least 10 registered commands."""

    def test_commands_count_at_least_ten(self):
        """There are at least 10 CommandDef entries registered."""
        assert len(COMMANDS) >= 10, f"Expected >= 10 commands, got {len(COMMANDS)}"

    def test_commands_are_commanddef_instances(self):
        """Every entry in COMMANDS is a CommandDef."""
        for cmd in COMMANDS:
            assert isinstance(cmd, CommandDef), f"{cmd} is not CommandDef"


# ── CommandDef attributes ─────────────────────────────────────────────


class TestCommandDefAttributes:
    """Every CommandDef has name, description, handler, aliases."""

    def test_every_command_has_name(self):
        """Every registered command has a non-empty name."""
        for cmd in COMMANDS:
            assert isinstance(cmd.name, str)
            assert len(cmd.name) > 0, f"Command {cmd} has empty name"

    def test_every_command_has_description(self):
        """Every registered command has a description."""
        for cmd in COMMANDS:
            assert isinstance(cmd.description, str)
            assert len(cmd.description) > 0, \
                f"Command '{cmd.name}' has empty description"

    def test_every_command_has_callable_handler(self):
        """Every registered command has a callable handler."""
        for cmd in COMMANDS:
            assert callable(cmd.handler), \
                f"Command '{cmd.name}' handler is not callable"

    def test_every_command_has_aliases_field(self):
        """Every command has an aliases list (may be empty)."""
        for cmd in COMMANDS:
            assert isinstance(cmd.aliases, list), \
                f"Command '{cmd.name}' aliases is not a list"

    def test_well_known_commands_present(self):
        """Core built-in commands are registered by name."""
        names = {cmd.name for cmd in COMMANDS}
        required = {"help", "exit", "new", "save", "llms", "tools", "model", "workspace"}
        missing = required - names
        assert not missing, f"Missing commands: {missing}"


# ── Built-in descriptions ─────────────────────────────────────────────


class TestBuiltinDescriptions:
    """Every built-in command has a proper, non-empty description."""

    BUILTIN_NAMES = {
        "help", "exit", "new", "save", "llms", "resume", "stop",
        "tools", "session", "continue", "update", "goal",
    }

    def test_all_builtins_have_descriptions(self):
        """Each built-in command has a Chinese description string."""
        cmd_map = {cmd.name: cmd for cmd in COMMANDS}
        for name in self.BUILTIN_NAMES:
            assert name in cmd_map, f"Built-in '{name}' not in COMMANDS"
            desc = cmd_map[name].description
            assert len(desc) > 0, f"Built-in '{name}' has empty description"


# ── Lazy import handler ───────────────────────────────────────────────


class TestLazyImport:
    """Lazy import handlers don't fail on first call when module exists."""

    def test_model_command_returns_string(self, mock_agent):
        """'/model' uses lazy import — should run without import error."""
        mock_agent.get_llm_name.return_value = "mock/mock-model"
        result = handle_command("/model", mock_agent)
        assert isinstance(result, str)
        assert len(result) > 0
        # Should list current model or backends.
        assert "mock" in result.lower()

    def test_workspace_command_handles_no_args(self, mock_agent):
        """'/workspace' with no args returns usage or status."""
        result = handle_command("/workspace", mock_agent)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_lazy_import_unknown_module_raises_on_invoke(self, monkeypatch):
        """A lazy handler for a nonexistent module surfaces the import error."""
        from zero_agent.frontends.commands.slash_commands import _lazy_import

        bad_handler = _lazy_import("nonexistent_module_xyz")
        with pytest.raises(ModuleNotFoundError):
            bad_handler("", MagicMock())


# ── handle_command — built-in side effects ────────────────────────────


class TestHandleCommandBuiltins:
    """Exercise built-in command handlers through the dispatcher."""

    def test_new_clears_session(self, mock_agent):
        """'/new' clears history and working state."""
        mock_agent.client.history = [{"role": "user", "content": "old"}]
        mock_agent.client.system = "old system"
        mock_agent.handler.working = {"goal": "something"}

        result = handle_command("/new", mock_agent)

        assert mock_agent.client.history == []
        assert mock_agent.client.system == ""
        assert mock_agent.handler.working == {}
        mock_agent.clear_pending_task.assert_called_once_with()
        assert "新会话" in result

    def test_stop_calls_abort(self, mock_agent):
        """'/stop' calls agent.abort() and returns signal message."""
        result = handle_command("/stop", mock_agent)
        mock_agent.abort.assert_called_once()
        assert "中止" in result

    def test_llms_lists_backends(self, mock_agent):
        """'/llms' returns backend listing."""
        result = handle_command("/llms", mock_agent)
        assert "mock-model" in result
        assert "gpt-4-turbo" in result

    def test_goal_set(self, mock_agent):
        """'/goal <desc>' stores the goal in handler.working."""
        result = handle_command("/goal build a bridge", mock_agent)
        assert mock_agent.handler.working["goal"] == "build a bridge"
        assert "已设置" in result

    def test_goal_show_empty(self, mock_agent):
        """'/goal' with no args shows empty-state message."""
        mock_agent.handler.working = {}
        result = handle_command("/goal", mock_agent)
        assert "没有设置" in result or "目标" in result

    def test_session_multipart_key(self, mock_agent):
        """'/session.max_tokens=8192' returns success message."""
        result = handle_command("/session.max_tokens=8192", mock_agent)
        assert "max_tokens" in result
        # Verifying the result string is sufficient; MagicMock attribute
        # traversal interacts differently than real objects for nested attrs.

    def test_exit_prints_and_returns_empty(self, mock_agent, capsys):
        """'/exit' prints 'Bye.' and returns empty string."""
        result = handle_command("/exit", mock_agent)
        captured = capsys.readouterr()
        assert "Bye." in captured.out
        assert result == ""


# ── _build_index ──────────────────────────────────────────────────────


class TestBuildIndex:
    """The name→CommandDef lookup includes primary names and aliases."""

    def test_index_includes_primary_names(self):
        """Every command's primary name is in the index."""
        import zero_agent.frontends.commands.slash_commands as sc
        sc._name_index = None  # reset
        idx = _build_index()
        for cmd in COMMANDS:
            assert cmd.name in idx, f"'{cmd.name}' missing from index"
            assert idx[cmd.name] is cmd

    def test_index_includes_aliases(self):
        """Every alias maps to the correct CommandDef."""
        import zero_agent.frontends.commands.slash_commands as sc
        sc._name_index = None
        idx = _build_index()
        for cmd in COMMANDS:
            for alias in cmd.aliases:
                assert alias in idx, f"Alias '{alias}' of '{cmd.name}' missing"
                assert idx[alias] is cmd, \
                    f"Alias '{alias}' does not point to '{cmd.name}'"


# ── Non-session handle_command ────────────────────────────────────────


class TestHandleCommandNonSession:
    """Tests for commands that don't require session.xxx syntax."""

    def test_update_no_args_shows_usage(self, mock_agent):
        """'/update' with no args shows usage."""
        result = handle_command("/update", mock_agent)
        assert "用法" in result

    def test_update_config_reloads(self, mock_agent):
        """'/update config' calls reload_config."""
        result = handle_command("/update config", mock_agent)
        mock_agent.reload_config.assert_called_once()
        assert "已重新加载" in result or "未变更" in result

    def test_update_plugins(self, mock_agent):
        """'/update plugins' reloads plugins."""
        result = handle_command("/update plugins", mock_agent)
        mock_agent._register_builtin_plugins.assert_called_once()
        assert "已重新加载" in result


# ── /plan registry ─────────────────────────────────────────────────────


class TestPlanCommandRegistry:
    """The /plan command is registered with Desktop-consistent metadata."""

    def test_plan_registered_with_task_hint(self):
        cmd_map = {cmd.name: cmd for cmd in COMMANDS}
        assert "plan" in cmd_map
        plan = cmd_map["plan"]
        assert plan.arg_hint == "[task]"
        assert plan.description == "创建并运行一个可验证的计划流程"

    def test_plan_handler_is_non_mutating(self, mock_agent):
        """handle_command('/plan ...') documents usage without running the agent."""
        result = handle_command("/plan build a thing", mock_agent)
        assert isinstance(result, str)
        assert "用法" in result
        mock_agent.run.assert_not_called()


# ── plan helpers ──────────────────────────────────────────────────────


class TestPlanHelpers:
    """has_active_plan / resolve_ready_plan read the live TaskContract."""

    @staticmethod
    def _certificate(
        task_id="t1",
        plan_remaining=0,
        verify_status="pass",
        status="completed",
    ):
        from zero_agent.core.types import CompletionCertificate

        return CompletionCertificate(
            task_id=task_id,
            status=status,  # type: ignore[arg-type]
            reason="plan_verified",
            evidence_count=0,
            verify_status=verify_status,
            plan_remaining=plan_remaining,
        )

    @staticmethod
    def _agent_with_contract(
        mode,
        plan_path=None,
        user_request="",
        certificate=None,
        running=False,
        pending_contract=None,
    ):
        from types import SimpleNamespace
        from zero_agent.core.types import TaskContract, TaskMode

        contract = TaskContract(
            task_id="t1",
            user_request=user_request,
            mode=TaskMode(mode),
            plan_path=plan_path,
        )
        return SimpleNamespace(
            _is_running_task=running,
            _pending_task_state=(
                SimpleNamespace(contract=pending_contract)
                if pending_contract is not None else None
            ),
            handler=SimpleNamespace(
                task_contract=contract,
                completion_certificate=certificate,
            )
        )

    @staticmethod
    def _contract(mode, plan_path=None):
        from zero_agent.core.types import TaskContract, TaskMode

        return TaskContract("t1", "do it", TaskMode(mode), plan_path)

    def test_has_active_plan_running_plan_and_executing(self):
        assert has_active_plan(self._agent_with_contract("plan", running=True)) is True
        assert has_active_plan(self._agent_with_contract("executing", running=True)) is True

    def test_has_active_plan_ready_plan_after_run(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(),
        )
        assert has_active_plan(agent) is True

    def test_has_active_plan_waiting_plan_pending_after_run(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [ ] answer question\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            pending_contract=self._contract("plan", str(plan_file)),
        )
        assert has_active_plan(agent) is True
    def test_has_active_plan_pending_executing_after_waiting_terminal(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "executing",
            plan_path=str(plan_file),
            pending_contract=self._contract("executing", str(plan_file)),
        )
        assert has_active_plan(agent) is True

    def test_has_active_plan_blocks_any_pending_task(self):
        pending_contract = self._contract("open")
        agent = self._agent_with_contract("open", pending_contract=pending_contract)
        assert has_active_plan(agent) is True

    def test_resolve_pending_plan_returns_pending_plan_contract(self, tmp_path):
        from zero_agent.core.types import TaskMode

        plan_file = tmp_path / "plan.md"
        pending_contract = self._contract("executing", str(plan_file))
        agent = self._agent_with_contract("open", pending_contract=pending_contract)

        assert resolve_pending_plan(agent) == ("do it", TaskMode.EXECUTING, str(plan_file))

    def test_resolve_pending_plan_ignores_non_plan_pending(self):
        pending_contract = self._contract("open")
        agent = self._agent_with_contract("open", pending_contract=pending_contract)
        assert resolve_pending_plan(agent) is None


    def test_has_active_plan_terminal_non_ready_plan_allows_recovery(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [ ] never completed\n", encoding="utf-8")
        agent = self._agent_with_contract("plan", plan_path=str(plan_file))
        assert has_active_plan(agent) is False

    def test_has_active_plan_terminal_executing_allows_recovery(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] old step\n", encoding="utf-8")
        agent = self._agent_with_contract("executing", plan_path=str(plan_file))
        assert has_active_plan(agent) is False

    def test_has_active_plan_open(self):
        assert has_active_plan(self._agent_with_contract("open")) is False

    def test_resolve_ready_plan_complete(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n- [x] step two\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(),
        )
        assert resolve_ready_plan(agent) == ("do it", str(plan_file))

    def test_resolve_ready_plan_accepts_partial_accepted(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(verify_status="partial_accepted"),
        )
        assert resolve_ready_plan(agent) == ("do it", str(plan_file))

    def test_resolve_ready_plan_incomplete(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [ ] pending\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            certificate=self._certificate(),
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_not_in_plan_mode(self):
        agent = self._agent_with_contract("open")
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_requires_certificate(self, tmp_path):
        """A hand-edited complete plan.md is not enough without a certificate."""
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan", plan_path=str(plan_file), user_request="do it"
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_nonzero_remaining(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(plan_remaining=1),
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_unverified_certificate(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(verify_status="not_required"),
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_namespace_certificate(self, tmp_path):
        from types import SimpleNamespace

        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=SimpleNamespace(
                task_id="t1",
                status="completed",
                reason="plan_verified",
                evidence_count=0,
                verify_status="pass",
                plan_remaining=0,
            ),
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_dict_certificate(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate={
                "task_id": "t1",
                "status": "completed",
                "reason": "plan_verified",
                "evidence_count": 0,
                "verify_status": "pass",
                "plan_remaining": 0,
            },
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_stale_task_id(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(task_id="old-task"),
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_non_completed_status(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("- [x] step one\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(status="failed"),
        )
        assert resolve_ready_plan(agent) is None

    def test_resolve_ready_plan_rejects_empty_checklist(self, tmp_path):
        """An empty checklist is never ready, even with a valid certificate."""
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n\n## 执行计划\n\n", encoding="utf-8")
        agent = self._agent_with_contract(
            "plan",
            plan_path=str(plan_file),
            user_request="do it",
            certificate=self._certificate(),
        )
        assert resolve_ready_plan(agent) is None
