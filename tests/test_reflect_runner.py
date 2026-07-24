"""Tests for runners/reflect_runner.py — ReflectRunner harness."""

import importlib
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zero_agent.core.types import TerminalEvent, TerminalStatus
from zero_agent.runners.reflect_runner import ReflectRunner


# ---- helpers ----

def _write_reflect_module(dirpath: str, name: str, content: str) -> str:
    """Write a temporary reflect module and return its path."""
    filepath = os.path.join(dirpath, f"{name}.py")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


# ---- ReflectRunner tests ----

class TestReflectRunnerInit:
    """ReflectRunner 初始化测试."""

    def test_stores_agent_and_path(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "test_mod",
                "INTERVAL = 10\ndef check():\n    return None\n"
            )
            runner = ReflectRunner(agent, mod_path)
            assert runner.agent is agent
            assert os.path.abspath(mod_path) == runner.module_path

    def test_raises_on_missing_file(self) -> None:
        agent = MagicMock()
        runner = ReflectRunner(agent, "/nonexistent/reflect.py")
        with pytest.raises(FileNotFoundError):
            runner._load_module()


class TestReflectRunnerLoadModule:
    """模块加载和热重载测试."""

    def test_loads_valid_module(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "simple",
                "INTERVAL = 5\nONCE = False\ndef check():\n    return None\n"
            )
            runner = ReflectRunner(agent, mod_path)
            mod = runner._load_module()
            assert mod.INTERVAL == 5
            assert mod.ONCE is False
            assert mod.check() is None

    def test_maybe_reload_detects_changes(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "reload_test",
                "INTERVAL = 1\ndef check():\n    return None\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._module.INTERVAL == 1

            # Modify the module file
            time.sleep(0.1)  # ensure mtime changes
            _write_reflect_module(
                tmpdir, "reload_test",
                "INTERVAL = 99\ndef check():\n    return None\n"
            )
            runner._maybe_reload()
            assert runner._module.INTERVAL == 99

    def test_scheduler_import_creates_runtime_dirs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        tasks_dir = tmp_path / "sche_tasks"
        monkeypatch.setenv("ZA_SCHED_TASKS_DIR", str(tasks_dir))
        monkeypatch.setenv("ZA_SCHED_LOCK_PORT", "0")

        sys.modules.pop("zero_agent.reflect.scheduler", None)
        scheduler = importlib.import_module("zero_agent.reflect.scheduler")

        assert scheduler.TASKS == str(tasks_dir)
        assert tasks_dir.is_dir()
        assert (tasks_dir / "done").is_dir()

    def test_scheduler_l4_uses_package_compressor_signature(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        tasks_dir = tmp_path / "sche_tasks"
        raw_dir = tmp_path / "responses"
        l4_dir = tmp_path / "l4"
        calls = []

        monkeypatch.setenv("ZA_SCHED_TASKS_DIR", str(tasks_dir))
        monkeypatch.setenv("ZA_SCHED_LOCK_PORT", "0")
        monkeypatch.setenv("ZA_MODEL_RESPONSES_DIR", str(raw_dir))
        monkeypatch.setenv("ZA_L4_DIR", str(l4_dir))
        monkeypatch.setattr(
            "zero_agent.memory.compress_session.batch_process",
            lambda src, dst, dry_run=True: calls.append((src, dst, dry_run)) or {
                "processed": 0,
            },
        )

        sys.modules.pop("zero_agent.reflect.scheduler", None)
        scheduler = importlib.import_module("zero_agent.reflect.scheduler")
        scheduler.check()

        assert calls == [(str(raw_dir), str(l4_dir), False)]


class TestReflectRunnerCheck:
    """check() 调用测试."""

    def test_returns_check_result(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "task_mod",
                'INTERVAL = 1\ndef check():\n    return "do something"\n'
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._call_check() == "do something"

    def test_returns_none(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "idle_mod",
                "INTERVAL = 1\ndef check():\n    return None\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._call_check() is None

    def test_returns_exit(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "exit_mod",
                "INTERVAL = 1\ndef check():\n    return '/exit'\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._call_check() == "/exit"

    def test_missing_check_returns_none(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "no_check",
                "INTERVAL = 1\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._call_check() is None


class TestReflectRunnerOnceMode:
    """ONCE 模式测试."""

    def test_should_exit_after_run_true(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "once_mod",
                "INTERVAL = 1\nONCE = True\ndef check():\n    return 'task'\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._should_exit_after_run() is True

    def test_should_exit_after_run_false_default(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "no_once",
                "INTERVAL = 1\ndef check():\n    return 'task'\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            assert runner._should_exit_after_run() is False


class TestReflectRunnerTask:
    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (TerminalStatus.COMPLETED, "completion_certificate"),
            (TerminalStatus.WAITING, "human_intervention"),
            (TerminalStatus.BUDGET_EXHAUSTED, "max_turns"),
            (TerminalStatus.PROTOCOL_ERROR, "invalid_step_outcome"),
        ],
    )
    def test_run_task_retains_terminal(self, capsys, status, reason) -> None:
        agent = MagicMock()

        def run(_task):
            yield "streamed"
            return TerminalEvent(status=status, reason=reason)

        agent.run = run
        runner = ReflectRunner(agent, "/fake/path/reflect.py")

        terminal = runner._run_task("task")

        assert capsys.readouterr().out == "streamed"
        assert terminal.status == status
        assert terminal.reason == reason

    def test_run_task_converts_exception(self) -> None:
        agent = MagicMock()

        def run(_task):
            yield "before"
            raise RuntimeError("broken")

        agent.run = run
        runner = ReflectRunner(agent, "/fake/path/reflect.py")

        terminal = runner._run_task("task")

        assert terminal.status == TerminalStatus.FAILED
        assert terminal.reason == "RuntimeError"
        assert terminal.text == "broken"

    def test_run_task_converts_synchronous_exception(self) -> None:
        agent = MagicMock()
        agent.run.side_effect = RuntimeError("broken before generator")
        runner = ReflectRunner(agent, "/fake/path/reflect.py")

        terminal = runner._run_task("task")

        assert terminal.status == TerminalStatus.FAILED
        assert terminal.reason == "RuntimeError"
        assert terminal.text == "broken before generator"


class TestReflectRunnerLifecycle:
    """ReflectRunner 生命周期测试."""

    def test_stop_sets_running_false(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "forever",
                "INTERVAL = 60\ndef check():\n    return None\n"
            )
            runner = ReflectRunner(agent, mod_path)
            runner._running = True
            runner.stop()
            assert runner._running is False

    def test_init_and_on_done_called(self) -> None:
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_path = _write_reflect_module(
                tmpdir, "callbacks",
                'INTERVAL = 1\n'
                'ONCE = False\n'
                '_init_called = False\n'
                '_done_called = False\n'
                'def init(args):\n'
                '    global _init_called\n'
                '    _init_called = True\n'
                'def check():\n'
                '    return "task"\n'
                'def on_done(result):\n'
                '    global _done_called\n'
                '    _done_called = True\n'
            )
            runner = ReflectRunner(agent, mod_path)
            runner._load_module()
            runner._call_init({"key": "value"})
            assert runner._module._init_called is True
            runner._call_on_done("result")
            assert runner._module._done_called is True


class TestSubAgentLaunchWritingInputTxt:
    """SubAgentManager._launch() writes input.txt and uses --nobg -q command."""

    def test_launch_writes_input_txt_and_nobg_command(self, tmp_path) -> None:
        """_launch() writes input.txt (not input.md) and command includes --nobg."""
        import shutil
        from zero_agent.reflect.subagent import SubAgentManager, SubAgentTask

        manager = SubAgentManager()
        task = SubAgentTask(task_id="t1", prompt="hello", io_dir=str(tmp_path))

        # Force zero-agent not found to test fallback command
        orig_which = shutil.which
        shutil.which = lambda name: None

        captured_cmd = []
        orig_popen = None
        import subprocess as _sp

        class FakeProc:
            def wait(self, timeout=None):
                return 0
            @property
            def returncode(self):
                return 0
            def communicate(self, timeout=None):
                return ("", "")

        def fake_popen(cmd, **kwargs):
            captured_cmd.append(cmd)
            return FakeProc()

        orig_popen_obj = _sp.Popen
        _sp.Popen = fake_popen

        try:
            manager._launch(task)
        finally:
            shutil.which = orig_which
            _sp.Popen = orig_popen_obj

        # input.txt exists (not input.md)
        assert (tmp_path / "input.txt").exists()
        assert not (tmp_path / "input.md").exists()
        assert (tmp_path / "input.txt").read_text() == "hello"

        # Command includes --nobg and -m zero_agent.runners.cli fallback
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert "-m" in cmd
        assert "zero_agent.runners.cli" in cmd
        assert "--task" in cmd
        assert "--nobg" in cmd
        assert "-q" in cmd

    def test_launch_uses_zero_agent_bin_when_found(self, tmp_path) -> None:
        """When zero-agent is found, command starts with the bin path."""
        import shutil
        from zero_agent.reflect.subagent import SubAgentManager, SubAgentTask

        manager = SubAgentManager()
        task = SubAgentTask(task_id="t2", prompt="hi", io_dir=str(tmp_path))

        orig_which = shutil.which
        shutil.which = lambda name: "/usr/local/bin/zero-agent" if name == "zero-agent" else None

        captured_cmd = []
        import subprocess as _sp

        class FakeProc:
            def wait(self, timeout=None):
                return 0
            @property
            def returncode(self):
                return 0
            def communicate(self, timeout=None):
                return ("", "")

        def fake_popen(cmd, **kwargs):
            captured_cmd.append(cmd)
            return FakeProc()

        orig_popen_obj = _sp.Popen
        _sp.Popen = fake_popen

        try:
            manager._launch(task)
        finally:
            shutil.which = orig_which
            _sp.Popen = orig_popen_obj

        cmd = captured_cmd[0]
        assert cmd[0] == "/usr/local/bin/zero-agent"
        assert "--nobg" in cmd
        assert "-q" in cmd
        assert "-m" not in cmd  # No module fallback when bin found

    def test_collect_result_prefers_output_txt(self, tmp_path) -> None:
        """_collect_result reads output.txt first, then output.md."""
        from zero_agent.reflect.subagent import SubAgentManager, SubAgentTask

        # Only output.md present
        task_md = SubAgentTask(task_id="md", prompt="", io_dir=str(tmp_path))
        (tmp_path / "output.md").write_text("from_md", encoding="utf-8")
        assert SubAgentManager._collect_result(task_md) == "from_md"

        (tmp_path / "output.txt").write_text("from_txt", encoding="utf-8")
        assert SubAgentManager._collect_result(task_md) == "from_txt"

class TestReflectLogWriting:
    """ReflectRunner._write_reflect_log writes to workspace_dir/reflect_logs/."""

    def test_write_reflect_log_creates_log_file(self, tmp_path) -> None:
        """_write_reflect_log creates a log file with the expected format."""
        from zero_agent.runners.reflect_runner import ReflectRunner
        from unittest.mock import MagicMock
        from pathlib import Path

        agent = MagicMock()
        agent.config.workspace_dir = str(tmp_path)

        runner = ReflectRunner(agent, "/fake/path/goal_mode.py")
        runner._write_reflect_log("hello world")

        log_dir = tmp_path / "reflect_logs"
        assert log_dir.exists()

        log_files = list(log_dir.glob("goal_mode_*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text(encoding="utf-8")
        assert "hello world" in content

    def test_write_reflect_log_fallback_cwd(self, monkeypatch) -> None:
        """When workspace_dir is unavailable, falls back to cwd/reflect_logs."""
        from zero_agent.runners.reflect_runner import ReflectRunner
        from unittest.mock import MagicMock
        import tempfile
        import datetime

        agent = MagicMock()
        del agent.config.workspace_dir  # simulate missing attribute

        runner = ReflectRunner(agent, "/fake/path/testmod.py")
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.chdir(td)
            runner._write_reflect_log("test result")

            log_dir = Path(td) / "reflect_logs"
            assert log_dir.exists()
