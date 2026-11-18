"""进程沙箱测试：模式解析、平台降级、实际隔离效果与 B1 回归."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from zero_agent.extensions import sandbox
from zero_agent.tools.builtin.code import code_run


def _consume(generator):
    """消费 code_run generator，返回其结果字典."""
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


def _run(argv, cwd=None):
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=30,
    )


class TestResolveMode:
    def test_default_is_off(self) -> None:
        assert sandbox.resolve_mode(SimpleNamespace()) == "off"

    def test_explicit_mode(self) -> None:
        assert sandbox.resolve_mode(SimpleNamespace(sandbox_mode="auto")) == "auto"

    def test_invalid_mode_falls_back_to_off(self) -> None:
        assert sandbox.resolve_mode(SimpleNamespace(sandbox_mode="nope")) == "off"


class TestWrapCommand:
    def test_off_does_not_wrap(self) -> None:
        cmd, applied, reason = sandbox.wrap_command(["echo", "hi"], "/tmp", "off")

        assert cmd == ["echo", "hi"]
        assert applied is False
        assert reason == ""

    def test_unknown_mode_degrades(self) -> None:
        cmd, applied, reason = sandbox.wrap_command(["echo"], "/tmp", "weird")

        assert cmd == ["echo"]
        assert applied is False
        assert "未知沙箱模式" in reason

    def test_unsupported_platform_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "windows")

        cmd, applied, reason = sandbox.wrap_command(["echo"], "/tmp", "auto")

        assert cmd == ["echo"]
        assert applied is False
        assert "不支持沙箱" in reason

    def test_relative_root_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")

        cmd, applied, reason = sandbox.wrap_command(["echo"], "relative/path", "macos")

        assert applied is False
        assert "写入根不可用" in reason

    def test_root_with_quote_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")

        cmd, applied, reason = sandbox.wrap_command(
            ["echo"], '/tmp/we"ird', "macos"
        )

        assert applied is False
        assert "写入根不可用" in reason

    def test_missing_sandbox_exec_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)

        cmd, applied, reason = sandbox.wrap_command(["echo"], "/tmp", "macos")

        assert applied is False
        assert "sandbox-exec 不可用" in reason

    def test_macos_wrapping_keeps_original_argv(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
        monkeypatch.setattr(sandbox, "_macos_available", lambda: True)

        cmd, applied, reason = sandbox.wrap_command(
            ["bash", "-c", "echo hi"], "/tmp/work", "auto"
        )

        assert applied is True
        assert reason == ""
        assert cmd[0] == "sandbox-exec"
        assert cmd[-3:] == ["bash", "-c", "echo hi"]

    def test_linux_wrapping_drops_tmpfs(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "linux")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
        monkeypatch.setattr(sandbox, "_linux_available", lambda: True)

        cmd, applied, _reason = sandbox.wrap_command(["echo", "hi"], "/tmp/work", "linux")

        assert applied is True
        assert cmd[0] == "bwrap"
        assert cmd[-2:] == ["echo", "hi"]
        # --tmpfs /tmp 会遮住 python 模式落在系统临时目录的脚本
        assert "--tmpfs" not in cmd

    def test_profile_escapes_quotes(self) -> None:
        profile = sandbox._macos_profile('/tmp/with"quote')
        assert '\\"' in profile


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec 仅 macOS")
class TestMacosIsolation:
    def test_workspace_write_is_allowed(self) -> None:
        work = Path(tempfile.mkdtemp())
        cmd, applied, reason = sandbox.wrap_command(
            ["bash", "-c", "echo data > inside.txt && echo ok"],
            str(work),
            "macos",
        )
        assert applied, reason

        result = _run(cmd, cwd=str(work))

        assert result.returncode == 0
        assert (work / "inside.txt").is_file()

    def test_writable_but_unlisted_location_is_denied(self) -> None:
        """对照实验：位置本身可写，沙箱仍必须拒绝."""
        home = Path.home()
        if not os.access(home, os.W_OK):
            pytest.skip("home 目录不可写")
        work = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp(dir=str(home)))
        target = outside / "blocked.txt"
        script = f"echo x > {target}"

        # 对照：没有沙箱时可以写成功
        baseline = _run(["bash", "-c", script])
        assert baseline.returncode == 0
        assert target.is_file()

        # 沙箱内同一位置必须失败
        sandbox_target = outside / "blocked-sandboxed.txt"
        cmd, applied, reason = sandbox.wrap_command(
            ["bash", "-c", f"echo x > {sandbox_target}"], str(work), "macos"
        )
        assert applied, reason
        result = _run(cmd, cwd=str(work))

        assert result.returncode != 0
        assert not sandbox_target.exists()

    def test_code_run_works_inside_sandbox(self) -> None:
        work = Path(tempfile.mkdtemp())

        result = _consume(code_run(
            "print('sandboxed-ok')",
            code_type="python",
            cwd=str(work),
            sandbox_mode="macos",
            sandbox_root=str(work),
            timeout=30,
        ))

        assert result.get("status") == "success"
        assert "sandboxed-ok" in result.get("stdout", "")

    def test_model_supplied_cwd_cannot_widen_sandbox(self) -> None:
        """B1 回归：cwd 参数不得把沙箱根变成 /."""
        home = Path.home()
        if not os.access(home, os.W_OK):
            pytest.skip("home 目录不可写")
        work = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp(dir=str(home)))
        target = outside / "cwd-bypass.txt"

        result = _consume(code_run(
            f"echo x > {target}",
            code_type="bash",
            cwd="/",
            sandbox_mode="macos",
            sandbox_root=str(work),
            timeout=30,
        ))

        # 无论命令成功与否，目标文件都不能被写出来
        assert not target.exists()
