"""热更新与热重启：插件同步、MCP 重载、检查点与 exec 续跑."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zero_agent.core import checkpoint as checkpoint_store
from zero_agent.core import hot_restart
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.checkpoint import CONTINUATION_PROMPT
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.types import EvidenceLedger, TerminalEvent, TerminalStatus
from zero_agent.extensions import runtime as runtime_module
from zero_agent.extensions.builtin import mcp_client
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.tools.registry import ToolRegistry

PLUGIN_V1 = """
def setup(ctx):
    @ctx.tool(name='a_tool', description='d',
              parameters={'type': 'object', 'properties': {}})
    def a_tool(args):
        return 'v1'

    ctx.prompt_block('a', lambda: 'A')
"""

PLUGIN_V2 = PLUGIN_V1.replace("return 'v1'", "return 'v2'")

FAKE_SERVER = """
import json
import sys

TOOL = {tool!r}
TEXT = {text!r}

def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {{"jsonrpc": "2.0", "id": request_id, "result": {{
            "protocolVersion": "2024-11-05",
            "capabilities": {{}},
            "serverInfo": {{"name": "fake", "version": "0"}},
        }}}}
    if method == "tools/list":
        return {{"jsonrpc": "2.0", "id": request_id, "result": {{"tools": [
            {{"name": TOOL, "description": "d",
              "inputSchema": {{"type": "object", "properties": {{}}}}}},
        ]}}}}
    if method == "tools/call":
        return {{"jsonrpc": "2.0", "id": request_id, "result": {{
            "content": [{{"type": "text", "text": TEXT}}],
        }}}}
    return None

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    response = handle(message)
    if response is not None:
        print(json.dumps(response), flush=True)
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _handler_result(definition) -> object:
    gen = definition.handler({}, None, None)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class TestPluginHotReload:
    def test_add_reload_remove_and_no_duplicate_blocks(self, tmp_path: Path) -> None:
        _write(tmp_path / "extensions" / "a.py", PLUGIN_V1)
        runtime = ExtensionRuntime(registry=ToolRegistry(), home=tmp_path)
        assert runtime.load_all() == 1
        assert len(runtime._prompt_blocks) == 1

        # 新增插件
        _write(
            tmp_path / "extensions" / "b.py",
            "def setup(ctx):\n"
            "    @ctx.tool(name='b_tool', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def b_tool(args):\n"
            "        return 'b'\n"
            "    ctx.prompt_block('b', lambda: 'B')\n",
        )
        report = runtime.sync_plugins()
        assert report["loaded"] == ["b"]
        assert runtime.registry.get("b_tool") is not None
        assert len(runtime._prompt_blocks) == 2

        # 改写插件后行为变化
        _write(tmp_path / "extensions" / "a.py", PLUGIN_V2)
        report = runtime.sync_plugins()
        assert report["reloaded"] == ["a"]
        assert _handler_result(runtime.registry.get("a_tool")) == "v2"

        # 删除插件后工具消失
        (tmp_path / "extensions" / "a.py").unlink()
        report = runtime.sync_plugins()
        assert report["removed"] == ["a"]
        assert runtime.registry.get("a_tool") is None

    def test_repeated_load_all_does_not_duplicate_setup(self, tmp_path: Path) -> None:
        _write(tmp_path / "extensions" / "a.py", PLUGIN_V1)
        runtime = ExtensionRuntime(registry=ToolRegistry(), home=tmp_path)

        runtime.load_all()
        runtime.load_all()

        assert len(runtime._prompt_blocks) == 1

    def test_directory_plugin_reloads_on_edit(self, tmp_path: Path) -> None:
        # 目录插件的入口是 ``__init__.py``：签名必须覆盖整个包目录，
        # 否则任何修改都不会被判定为「已变化」。
        package = tmp_path / "extensions" / "pkg"
        _write(
            package / "__init__.py",
            "VERSION = 'v1'\n"
            "from .helper import unused\n"
            "def setup(ctx):\n"
            "    @ctx.tool(name='pkg_tool', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def pkg_tool(args):\n"
            "        return VERSION\n",
        )
        _write(package / "helper.py", "unused = 1\n")
        runtime = ExtensionRuntime(registry=ToolRegistry(), home=tmp_path)
        assert runtime.sync_plugins()["loaded"] == ["pkg"]
        assert _handler_result(runtime.registry.get("pkg_tool")) == "v1"

        _write(
            package / "__init__.py",
            "VERSION = 'v2'\n"
            "from .helper import unused\n"
            "def setup(ctx):\n"
            "    @ctx.tool(name='pkg_tool', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def pkg_tool(args):\n"
            "        return VERSION\n",
        )
        assert runtime.sync_plugins()["reloaded"] == ["pkg"]
        assert _handler_result(runtime.registry.get("pkg_tool")) == "v2"

        # 只改包内非入口模块也必须触发重载
        _write(package / "helper.py", "unused = 2\n")
        assert runtime.sync_plugins()["reloaded"] == ["pkg"]

    def test_broken_plugin_is_not_retried_until_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from types import SimpleNamespace

        home = tmp_path / "home"
        _write(
            home / "extensions" / "broken.py",
            "def setup(ctx):\n    raise RuntimeError('boom')\n",
        )
        runtime = ExtensionRuntime(
            registry=ToolRegistry(), home=home,
            config=SimpleNamespace(permission_mode="ask"),
        )
        attempts: list[str] = []
        real_load_one = runtime._load_one

        def counting(name, path, is_package):
            attempts.append(name)
            return real_load_one(name, path, is_package)

        monkeypatch.setattr(runtime, "_load_one", counting)

        assert runtime.sync_plugins()["failed"], "首次应尝试并失败"
        assert runtime.sync_plugins()["failed"] == {}
        assert len(attempts) == 1

        _write(home / "extensions" / "broken.py", "def setup(ctx):\n    return None\n")
        assert runtime.sync_plugins()["loaded"] == ["broken"]
        assert len(attempts) == 2


class TestMcpHotReload:
    def test_reconfigure_points_to_new_server(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        server_one = tmp_path / "one.py"
        server_one.write_text(
            FAKE_SERVER.format(tool="echo", text="echo1"),
            encoding="utf-8",
        )
        server_two = tmp_path / "two.py"
        server_two.write_text(
            FAKE_SERVER.format(tool="echo2", text="echo2"),
            encoding="utf-8",
        )
        (home / "mcp.json").write_text(json.dumps({
            "mcpServers": {"fake": {"command": "python3", "args": [str(server_one)], "timeout": 10}},
        }), encoding="utf-8")

        runtime = ExtensionRuntime(registry=ToolRegistry(), home=home)
        runtime.reload_builtin("mcp_client", "zero_agent.extensions.builtin.mcp_client")
        try:
            assert runtime.registry.get("mcp__fake__echo") is not None
            live_before = [
                client
                for client in mcp_client._live_clients
                if str(server_one) in (client.args or [])
            ]
            assert live_before

            (home / "mcp.json").write_text(json.dumps({
                "mcpServers": {"fake": {"command": "python3", "args": [str(server_two)], "timeout": 10}},
            }), encoding="utf-8")
            runtime.reload_builtin("mcp_client", "zero_agent.extensions.builtin.mcp_client")

            assert runtime.registry.get("mcp__fake__echo") is None
            assert runtime.registry.get("mcp__fake__echo2") is not None
            assert all(
                client not in mcp_client._live_clients
                for client in live_before
            )
        finally:
            runtime.release()


class TestCheckpoint:
    def _agent(self, tmp_path: Path, session_id: str = "sess-test") -> ZeroAgent:
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
            extensions_home=str(tmp_path / "home"),
            extensions_enabled=False,
        )
        config._checkpoint_session_id = session_id
        config._checkpoint_source = "cli"
        return ZeroAgent(config)

    def test_roundtrip_restores_history_and_pending(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)
        try:
            from zero_agent.core.types import TaskContract, TaskMode
            from zero_agent.core.types import EvidenceRecord

            agent._is_running_task = True
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="do work", mode=TaskMode.OPEN,
            )
            agent.handler.evidence_ledger = EvidenceLedger(records=[EvidenceRecord(
                turn=1, tool_name="file_read", status="success",
                kind="read", summary="read a.txt",
            )])
            agent.handler.working = {"key_info": "remember me"}
            agent.handler.history_info = ["[USER]: do work"]
            agent.handler.last_user_request = "do work"
            agent.client.history = [{"role": "user", "content": "do work"}]
            agent.note_pending_messages([{"role": "user", "content": "pending"}])

            path = agent.save_checkpoint(status="running", restart=True)
            assert path is not None
            data = checkpoint_store.load(path)
            assert data["status"] == "running"
            assert len(data["restart_history"]) == 1

            restored = self._agent(tmp_path)
            try:
                assert restored.restore_checkpoint(data) is True
                assert restored.client.history[-1] == {"role": "user", "content": "pending"}
                assert restored._pending_task_state is not None
                assert restored._pending_task_state.waiting_kind == "restart"
                assert restored._pending_task_state.contract.user_request == "do work"
                assert restored._pending_task_state.ledger.records[0].summary == "read a.txt"
                assert restored.take_restart_prompt() == CONTINUATION_PROMPT
                assert restored.take_restart_prompt() is None
                restored._apply_resume_handler_state()
                assert restored.handler.working == {"key_info": "remember me"}
                assert restored.handler.history_info == ["[USER]: do work"]
            finally:
                restored.close()
        finally:
            agent.close()

    def test_run_uses_continuation_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = self._agent(tmp_path)
        try:
            from zero_agent.core.types import TaskContract, TaskMode

            agent._is_running_task = True
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="original goal", mode=TaskMode.OPEN,
            )
            agent.client.history = [{"role": "user", "content": "original goal"}]
            agent.note_pending_messages([{"role": "user", "content": "pending"}])
            data = checkpoint_store.load(agent.save_checkpoint(status="running", restart=True))

            restored = self._agent(tmp_path)
            try:
                captured: dict[str, object] = {}

                class FakeLoop:
                    def __init__(self, **kwargs):
                        pass

                    def run(self, **kwargs):
                        captured.update(kwargs)
                        yield {"turn": 1}
                        return TerminalEvent(status=TerminalStatus.COMPLETED)

                monkeypatch.setattr("zero_agent.core.agent.AgentLoop", FakeLoop)
                assert restored.restore_checkpoint(data) is True
                assert {"role": "user", "content": "pending"} in restored.client.history

                list(restored.run("ignored"))

                content = str(captured.get("initial_user_content"))
                assert content.startswith(CONTINUATION_PROMPT)
                assert "original goal" in content
                # 终态清理删除 running checkpoint
                assert not checkpoint_store.checkpoint_path(
                    str(tmp_path / "sessions"), data["session_id"],
                ).exists()
            finally:
                restored.close()
        finally:
            agent.close()


class TestReloadWorkflow:
    def test_requests_core_restart_after_checkpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.core.types import TaskContract, TaskMode

        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent._is_running_task = True
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="do work", mode=TaskMode.OPEN,
            )
            calls: list[str] = []
            agent.set_hot_restart_hook(lambda reason: calls.append(reason))
            agent._core_signature = "old"
            monkeypatch.setattr(hot_restart, "core_signature", lambda root=None: "changed")
            monkeypatch.setattr(hot_restart, "validate_core", lambda root=None: (True, ""))

            report = agent.reload_workflow()

            assert report["core_restart"] == "requested"
            assert calls == ["core_changed"]
            data = checkpoint_store.load(report["checkpoint_path"])
            assert data["status"] == "running"
            assert len(data["restart_history"]) == 1
        finally:
            agent.close()

    def test_idle_core_change_restarts_without_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = TestCheckpoint()._agent(tmp_path)
        try:
            calls: list[str] = []
            agent.set_hot_restart_hook(lambda reason: calls.append(reason))
            agent._core_signature = "old"
            monkeypatch.setattr(hot_restart, "core_signature", lambda root=None: "changed")
            monkeypatch.setattr(hot_restart, "validate_core", lambda root=None: (True, ""))

            report = agent.reload_workflow()

            assert report["core_restart"] == "requested"
            assert "checkpoint_path" not in report
            assert calls == ["core_changed"]
            assert checkpoint_store.list_checkpoints(agent.config.sessions_dir) == []
        finally:
            agent.close()

    def test_running_task_without_sessions_dir_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent._is_running_task = True
            agent.config.sessions_dir = ""
            calls: list[str] = []
            agent.set_hot_restart_hook(lambda reason: calls.append(reason))
            agent._core_signature = "old"
            monkeypatch.setattr(hot_restart, "core_signature", lambda root=None: "changed")
            monkeypatch.setattr(hot_restart, "validate_core", lambda root=None: (True, ""))

            report = agent.reload_workflow()

            assert report["core_restart"] == "blocked"
            assert report["core_restart_error"] == "checkpoint_failed"
            assert calls == []
        finally:
            agent.close()

    def test_run_entry_does_not_consume_core_change(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.core.types import TaskContract, TaskMode

        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent._is_running_task = True
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="do work", mode=TaskMode.OPEN,
            )
            calls: list[str] = []
            agent.set_hot_restart_hook(lambda reason: calls.append(reason))
            agent._core_signature = "old"
            monkeypatch.setattr(hot_restart, "core_signature", lambda root=None: "changed")

            report = agent.reload_workflow(allow_core_restart=False)

            assert report["core_restart"] == ""
            assert calls == []
            assert agent._core_signature == "old"
        finally:
            agent.close()


    def test_cli_restore_returns_continuation_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.runners import cli
        from zero_agent.core.types import TaskContract, TaskMode

        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent._is_running_task = True
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="do work", mode=TaskMode.OPEN,
            )
            path = agent.save_checkpoint(status="running", restart=True)
            monkeypatch.setenv("ZA_RESUME_CHECKPOINT", str(path))

            restored = TestCheckpoint()._agent(tmp_path)
            try:
                prompt, question = cli._restore_restart_checkpoint(restored)

                assert prompt == CONTINUATION_PROMPT
                assert question is None
                assert os.environ.get("ZA_RESUME_CHECKPOINT") is None
            finally:
                restored.close()
        finally:
            agent.close()

    def test_cli_restore_returns_waiting_question(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.runners import cli
        from zero_agent.core.types import (
            PendingTaskState,
            TaskContract,
            TaskMode,
        )

        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="do work", mode=TaskMode.OPEN,
            )
            agent._pending_task_state = PendingTaskState(
                contract=agent.handler.task_contract,
                ledger=EvidenceLedger(),
                plan_verify_status="missing",
                waiting_kind="ask_user",
                waiting_data={"question": "Which file?"},
            )
            path = agent.save_checkpoint(status="waiting")
            monkeypatch.setenv("ZA_RESUME_CHECKPOINT", str(path))

            restored = TestCheckpoint()._agent(tmp_path)
            try:
                prompt, question = cli._restore_restart_checkpoint(restored)

                assert prompt is None
                assert question == "Which file?"
            finally:
                restored.close()
        finally:
            agent.close()


class TestExtensionTopology:
    def test_home_change_reloads_plugins_and_builtins(self, tmp_path: Path) -> None:
        from zero_agent.core.config import AgentConfig, LLMBackendConfig
        from zero_agent.core.agent import ZeroAgent

        home_one = tmp_path / "home1"
        home_two = tmp_path / "home2"
        _write(
            home_one / "extensions" / "one.py",
            "def setup(ctx):\n"
            "    @ctx.tool(name='one_tool', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def one_tool(args):\n"
            "        return 'one'\n",
        )
        _write(
            home_two / "extensions" / "two.py",
            "def setup(ctx):\n"
            "    @ctx.tool(name='two_tool', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def two_tool(args):\n"
            "        return 'two'\n",
        )
        config = AgentConfig(
            llm_backends={
                "d": LLMBackendConfig(
                    name="d", provider="openai", api_key="k",
                    api_base="https://api.openai.com/v1", model="gpt-4o",
                )
            },
            default_backend="d",
            workspace_dir=str(tmp_path / "ws"),
            memory_dir=str(tmp_path / "mem"),
            sessions_dir=str(tmp_path / "sessions"),
            extensions_home=str(home_one),
        )
        agent = ZeroAgent(config)
        try:
            assert agent.registry.get("one_tool") is not None
            agent.config.extensions_home = str(home_two)

            report = agent.reload_workflow()

            assert report["topology"] is True
            assert agent.extensions.home == home_two
            assert agent.registry.get("one_tool") is None
            assert agent.registry.get("two_tool") is not None
            assert "skills_index" in agent.extensions.installed_builtins
        finally:
            agent.close()

    def test_load_builtin_module_reexecutes_edited_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        pkg = tmp_path / "zafake_reload"
        pkg.mkdir()
        _write(pkg / "__init__.py", "")
        _write(pkg / "m.py", "VALUE = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        try:
            assert ExtensionRuntime.load_builtin_module("zafake_reload.m").VALUE == 1
            # 同长度改写：只有 pyc 失效才能读到 2
            _write(pkg / "m.py", "VALUE = 2\n")
            assert ExtensionRuntime.load_builtin_module("zafake_reload.m").VALUE == 2
            assert sys.modules["zafake_reload.m"].VALUE == 2
        finally:
            sys.modules.pop("zafake_reload.m", None)
            sys.modules.pop("zafake_reload", None)


class TestDesktopResume:
    def _manager(self, tmp_path: Path):
        import threading

        from zero_agent.frontends.desktop_bridge import AgentManager, Session
        from zero_agent.frontends.session_store import SessionStore

        manager = object.__new__(AgentManager)
        manager.lock = threading.RLock()
        manager._hot_restart_lock = threading.RLock()
        manager._hot_restarting = False
        manager.sessions_dir = str(tmp_path / "sessions")
        Path(manager.sessions_dir).mkdir(parents=True, exist_ok=True)
        manager.session_store = SessionStore(
            str(Path(manager.sessions_dir) / "sessions.sqlite3")
        )
        manager.session_store.initialize()
        manager.sessions = {}
        manager.active_session_id = None
        manager._last_persisted_state = {}
        return manager, Session

    def _checkpoint(self, manager, sid: str, status: str, **extra) -> None:
        payload = {
            "version": 1,
            "session_id": sid,
            "source": "desktop",
            "status": status,
            "created_at": 0.0,
            "updated_at": 0.0,
            "restart_history": [],
            "history": [],
            "pending_messages": [],
            "handler": {},
            "pending": {},
            "mode_context": {},
        }
        payload.update(extra)
        checkpoint_store.save(manager.sessions_dir, sid, payload)

    class _FakeZa:
        def __init__(self, pending):
            self._pending_task_state = pending
            self.pending_task_state = pending

        def restore_checkpoint(self, data) -> bool:
            return True

    class _FakeRunner:
        def __init__(self, za):
            self._agent = za

    def test_running_checkpoint_starts_resume_turn(self, tmp_path: Path) -> None:
        from zero_agent.core.types import PendingTaskState, TaskContract, TaskMode

        manager, Session = self._manager(tmp_path)
        sess = Session(id="sess-resume")
        manager.sessions[sess.id] = sess
        pending = PendingTaskState(
            contract=TaskContract(
                task_id="t1", user_request="do", mode=TaskMode.OPEN,
            ),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="restart",
        )
        za = self._FakeZa(pending)
        manager.make_agent = lambda _sess: self._FakeRunner(za)
        calls: list[str] = []
        manager.run_agent_turn = lambda _sess, prompt, **kwargs: calls.append(prompt)
        self._checkpoint(manager, sess.id, "running")

        manager.resume_restarted_sessions()

        import threading
        for thread in threading.enumerate():
            if thread.name.startswith("Resume-"):
                thread.join(2)
        assert sess.status == "running"
        assert calls == [CONTINUATION_PROMPT]
        assert any(message["role"] == "system" for message in sess.messages)

    def test_waiting_question_is_restored_without_running(self, tmp_path: Path) -> None:
        from zero_agent.core.types import PendingTaskState, TaskContract, TaskMode

        manager, Session = self._manager(tmp_path)
        sess = Session(id="sess-waiting")
        manager.sessions[sess.id] = sess
        pending = PendingTaskState(
            contract=TaskContract(
                task_id="t1", user_request="do", mode=TaskMode.OPEN,
            ),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="ask_user",
            waiting_data={"question": "Which file?", "candidates": ["a", "b"]},
        )
        manager.make_agent = lambda _sess: self._FakeRunner(self._FakeZa(pending))
        calls: list[str] = []
        manager.run_agent_turn = lambda _sess, prompt, **kwargs: calls.append(prompt)
        self._checkpoint(manager, sess.id, "waiting")

        manager.resume_restarted_sessions()

        assert sess.status == "waiting"
        assert calls == []
        assert any(
            message.get("content") == "Which file?"
            and message.get("kind") == "input_required"
            for message in sess.messages
        )

    def test_restart_loop_budget_marks_error_and_removes_checkpoint(
        self,
        tmp_path: Path,
    ) -> None:
        import time

        manager, Session = self._manager(tmp_path)
        sess = Session(id="sess-loop")
        manager.sessions[sess.id] = sess
        self._checkpoint(
            manager,
            sess.id,
            "running",
            restart_history=[time.time()] * 5,
        )

        manager.resume_restarted_sessions()

        assert sess.status == "error"
        assert sess.terminal_reason == "hot_restart_loop"
        assert not checkpoint_store.checkpoint_path(
            manager.sessions_dir, sess.id,
        ).exists()


class TestHotRestartValidation:
    def test_core_signature_changes_with_file_content(self, tmp_path: Path) -> None:
        package = tmp_path / "pkg"
        _write(package / "a.py", "VALUE = 1\n")
        first = hot_restart.core_signature(package)

        _write(package / "a.py", "VALUE = 2\n")

        assert hot_restart.core_signature(package) != first

    def test_validate_core_reports_syntax_error(self, tmp_path: Path) -> None:
        package = tmp_path / "pkg"
        _write(package / "bad.py", "def broken(:\n")

        ok, error = hot_restart.validate_core(package)

        assert ok is False
        assert "bad.py" in error

    def test_cli_hot_restart_writes_checkpoint_and_execs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.runners import cli

        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent._is_running_task = True
            from zero_agent.core.types import TaskContract, TaskMode

            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="do work", mode=TaskMode.OPEN,
            )
            calls: list[list[str]] = []
            monkeypatch.setattr(os, "execv", lambda argv0, argv: calls.append(list(argv)))
            monkeypatch.delenv("ZA_RESUME_CHECKPOINT", raising=False)

            with pytest.raises(SystemExit) as exc:
                cli._cli_hot_restart(agent, "test")

            assert exc.value.code == 75
            assert calls and calls[0][0] == os.sys.executable
            resume_path = os.environ.get("ZA_RESUME_CHECKPOINT", "")
            assert resume_path and Path(resume_path).is_file()
            data = checkpoint_store.load(resume_path)
            assert data["status"] == "running"
            assert len(data["restart_history"]) == 1
        finally:
            agent.close()
            os.environ.pop("ZA_RESUME_CHECKPOINT", None)
            os.environ.pop("ZA_HOT_RESTART", None)


class TestCheckpointFidelity:
    def test_history_with_block_content_survives_roundtrip(self, tmp_path: Path) -> None:
        from zero_agent.core.types import TaskContract, TaskMode

        agent = TestCheckpoint()._agent(tmp_path)
        try:
            agent._is_running_task = True
            agent.handler.task_contract = TaskContract(
                task_id="t1", user_request="look", mode=TaskMode.OPEN,
            )
            block = [{"type": "text", "text": "hi"}, {"type": "image_url",
                      "image_url": {"url": "data:image/png;base64,AAA"}}]
            agent.client.history = [{"role": "user", "content": block}]

            path = agent.save_checkpoint(status="running")
            data = checkpoint_store.load(path)

            assert data["history"][0]["content"] == block
            restored = TestCheckpoint()._agent(tmp_path)
            try:
                assert restored.restore_checkpoint(data) is True
                assert restored.client.history[0]["content"] == block
            finally:
                restored.close()
        finally:
            agent.close()

    def test_pending_messages_merge_without_duplicates(self) -> None:
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]

        merged = checkpoint_store.merge_pending_messages(
            list(history),
            [{"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"}],
        )
        assert merged == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]

        # 整段 pending 已落盘时不重复追加
        assert checkpoint_store.merge_pending_messages(list(history), history) == history

    def test_restart_budget_helpers(self) -> None:
        now = 1000.0
        running = {"status": "running", "restart_history": [now - 10] * 4}
        assert not checkpoint_store.restart_budget_exhausted(running, now=now)

        running["restart_history"] = [now - 10] * checkpoint_store.RESTART_BUDGET_LIMIT
        assert checkpoint_store.restart_budget_exhausted(running, now=now)

        # 窗口外的不计入
        stale = {
            "status": "running",
            "restart_history": [now - 10_000] * 20,
        }
        assert not checkpoint_store.restart_budget_exhausted(stale, now=now)

        # 等用户答复的检查点不受预算限制
        waiting = {"status": "waiting", "restart_history": [now] * 20}
        assert not checkpoint_store.restart_budget_exhausted(waiting, now=now)


class TestBuiltinReloadRollback:
    def test_failed_reload_keeps_previous_registrations(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import importlib as importlib_module
        from types import SimpleNamespace

        runtime = ExtensionRuntime(
            registry=ToolRegistry(),
            home=tmp_path,
            config=SimpleNamespace(permission_mode="ask"),
        )
        module_path = "zero_agent.extensions.builtin.permission_gate"
        try:
            assert runtime.reload_builtin("permission_gate", module_path) is True
            assert runtime._tool_call

            class _BrokenModule:
                @staticmethod
                def setup(ctx):
                    raise RuntimeError("boom")

            import importlib as importlib_module

            real_import = importlib_module.import_module
            monkeypatch.setattr(
                importlib_module,
                "import_module",
                lambda path, *a, **kw: (
                    _BrokenModule if path == "broken.builtin" else real_import(path)
                ),
            )

            with pytest.raises(RuntimeError):
                runtime.reload_builtin("permission_gate", "broken.builtin")

            # 回滚：门控仍然在注册表里，且归属仍被视为已安装
            assert runtime._tool_call
            assert "permission_gate" in runtime.installed_builtins
        finally:
            runtime.release()


class TestExtensionTopologyBrowser:
    def test_browser_mode_change_reinstalls_builtin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.core.config import AgentConfig, LLMBackendConfig
        from zero_agent.core.agent import ZeroAgent

        # 不能替换 browser_tools.setup 来观察：重装会重新执行模块代码，
        # 属性替换会被 reload 抹掉。这里观察安装次数与 reinstall 标记。
        installs: list[bool] = []
        real_install = ZeroAgent._install_builtin_extension

        def spy(self, runtime, name, module_path, is_safety, reinstall=False):
            if name == "browser_tools":
                installs.append(reinstall)
            return real_install(
                self, runtime, name, module_path, is_safety, reinstall=reinstall,
            )

        monkeypatch.setattr(ZeroAgent, "_install_builtin_extension", spy)
        config = AgentConfig(
            llm_backends={
                "d": LLMBackendConfig(
                    name="d", provider="openai", api_key="k",
                    api_base="https://api.openai.com/v1", model="gpt-4o",
                )
            },
            default_backend="d",
            workspace_dir=str(tmp_path / "ws"),
            memory_dir=str(tmp_path / "mem"),
            sessions_dir=str(tmp_path / "sessions"),
            extensions_home=str(tmp_path / "home"),
            browser_tools="off",
        )
        agent = ZeroAgent(config)
        try:
            assert "browser_tools" not in agent.extensions.installed_builtins
            assert installs == []

            agent.config.browser_tools = "auto"
            assert agent.reload_workflow()["topology"] is True
            assert "browser_tools" in agent.extensions.installed_builtins
            # 首次安装不重执行模块：sys.modules 里就是当前代码
            assert installs == [False]

            agent.config.browser_tools = "on"
            assert agent.reload_workflow()["topology"] is True
            # 重装必须重执行模块，否则内置扩展的源码改动读不到
            assert installs == [False, True]
        finally:
            agent.close()


class TestHotRestartLifecycle:
    def test_failed_exec_rebuilds_closed_sessions(self, tmp_path: Path) -> None:
        from zero_agent.core.types import PendingTaskState, TaskContract, TaskMode

        manager, Session = TestDesktopResume()._manager(tmp_path)
        sess = Session(id="sess-recover")
        sess.status = "running"
        manager.sessions[sess.id] = sess
        pending = PendingTaskState(
            contract=TaskContract(
                task_id="t1", user_request="do", mode=TaskMode.OPEN,
            ),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="restart",
        )
        rebuilt: list[str] = []
        manager.make_agent = lambda target: (
            rebuilt.append(target.id)
            or TestDesktopResume._FakeRunner(TestDesktopResume._FakeZa(pending))
        )
        TestDesktopResume()._checkpoint(manager, sess.id, "running")

        manager._recover_after_failed_restart([sess.id])

        assert rebuilt == [sess.id]
        assert sess.status == "waiting"
        assert sess.agent is not None
        assert any(
            "热重启失败" in str(message.get("content", ""))
            for message in sess.messages
        )

    def test_exec_restart_hands_token_to_new_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from zero_agent.frontends import desktop_bridge

        manager, _Session = TestDesktopResume()._manager(tmp_path)
        manager.bridge_token = "tok-abc"
        monkeypatch.delenv("ZA_DESKTOP_BRIDGE_TOKEN", raising=False)
        monkeypatch.setattr(
            desktop_bridge.hot_restart,
            "restart_process",
            lambda: (_ for _ in ()).throw(RuntimeError("exec-blocked")),
        )

        with pytest.raises(RuntimeError):
            manager._exec_restart()

        assert os.environ["ZA_DESKTOP_BRIDGE_TOKEN"] == "tok-abc"
        os.environ.pop("ZA_DESKTOP_BRIDGE_TOKEN", None)

    def test_adopt_bridge_token_keeps_env_clean(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from types import SimpleNamespace as NS

        from zero_agent.frontends import desktop_bridge

        monkeypatch.setenv("ZA_DESKTOP_BRIDGE_TOKEN", "tok-123")
        security = desktop_bridge.load_bridge_security("127.0.0.1", 14168)
        monkeypatch.setattr(desktop_bridge, "manager", NS(bridge_token=""))

        desktop_bridge.adopt_bridge_token(security)

        assert desktop_bridge.manager.bridge_token == "tok-123"
        assert "ZA_DESKTOP_BRIDGE_TOKEN" not in os.environ
