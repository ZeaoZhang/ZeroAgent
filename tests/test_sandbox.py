"""进程沙箱测试：模式解析、平台降级与实际隔离效果."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from zero_agent.extensions import sandbox


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
        assert "降级" in reason

    def test_unsupported_platform_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "windows")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)

        cmd, applied, reason = sandbox.wrap_command(["echo"], "/tmp", "auto")

        assert cmd == ["echo"]
        assert applied is False
        assert "不支持" in reason or "降级" in reason

    def test_missing_bwrap_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "linux")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)

        cmd, applied, reason = sandbox.wrap_command(["echo"], "/tmp", "linux")

        assert cmd == ["echo"]
        assert applied is False
        assert "降级" in reason

    def test_macos_wrapping_keeps_original_argv(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")

        cmd, applied, reason = sandbox.wrap_command(
            ["bash", "-c", "echo hi"], "/tmp/work", "auto"
        )

        assert applied is True
        assert reason == ""
        assert cmd[0] == "sandbox-exec"
        # 原始命令必须原样保留在末尾
        assert cmd[-3:] == ["bash", "-c", "echo hi"]

    def test_linux_wrapping_keeps_original_argv(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "linux")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")

        cmd, applied, _reason = sandbox.wrap_command(["echo", "hi"], "/tmp/work", "linux")

        assert applied is True
        assert cmd[0] == "bwrap"
        assert cmd[-2:] == ["echo", "hi"]


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

        result = subprocess.run(
            cmd, cwd=str(work), capture_output=True, text=True, timeout=30
        )

        assert result.returncode == 0
        assert (work / "inside.txt").is_file()

    def test_outside_write_is_denied(self) -> None:
        work = Path(tempfile.mkdtemp())
        target = "/etc/za_sandbox_probe_should_fail"
        cmd, applied, reason = sandbox.wrap_command(
            ["bash", "-c", f"echo x > {target}"],
            str(work),
            "macos",
        )
        assert applied, reason

        result = subprocess.run(
            cmd, cwd=str(work), capture_output=True, text=True, timeout=30
        )

        assert result.returncode != 0
        assert not Path(target).exists()
