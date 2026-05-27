"""Tests for reflect/runner.py — ReflectRunner harness."""

import os
import sys
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from zero_agent.reflect.runner import ReflectRunner


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
