"""进程沙箱测试：模式解析、平台降级、实际隔离效果与 B1 回归."""

from __future__ import annotations

import os
import shutil
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
    def test_default_is_auto(self) -> None:
        assert sandbox.resolve_mode(SimpleNamespace()) == "auto"

    def test_explicit_mode(self) -> None:
        assert sandbox.resolve_mode(SimpleNamespace(sandbox_mode="auto")) == "auto"

    def test_invalid_mode_falls_back_to_default(self) -> None:
        assert sandbox.resolve_mode(SimpleNamespace(sandbox_mode="nope")) == "auto"


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

    def test_profile_escapes_backslashes(self) -> None:
        # 含引号的根在 wrap_command 里已被 _normalize_root 拒绝；这里验证
        # profile 文本层面的转义仍然生效（反斜杠是合法路径字符）。
        profile = sandbox._macos_profile('/tmp/with\\backslash')
        assert '\\\\' in profile

    def test_extra_write_roots_enter_the_profile(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(sandbox, "_macos_available", lambda: True)
        extra = tmp_path / "reports"
        extra.mkdir()

        cmd, applied, _reason = sandbox.wrap_command(
            ["echo"], str(tmp_path / "root"), "macos",
            extra_roots=[str(extra)],
        )

        assert applied is True
        assert str(extra.resolve()) in cmd[2]

    def test_extra_write_roots_are_bound_on_linux(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", "linux")
        monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
        monkeypatch.setattr(sandbox, "_linux_available", lambda: True)
        extra = tmp_path / "reports"
        extra.mkdir()

        cmd, applied, _reason = sandbox.wrap_command(
            ["echo"], str(tmp_path / "root"), "linux",
            extra_roots=[str(extra)],
        )

        assert applied is True
        target = str(extra.resolve())
        pairs = [
            (cmd[index + 1], cmd[index + 2])
            for index, item in enumerate(cmd)
            if item == "--bind" and index + 2 < len(cmd)
        ]
        assert (target, target) in pairs


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec 仅 macOS")
@pytest.mark.skipif(
    not sandbox._macos_available(),
    reason="当前环境无法应用 sandbox-exec（例如测试进程本身已在沙箱内，嵌套被拒）",
)
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
        try:
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
        finally:
            shutil.rmtree(outside, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)

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
        try:
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
        finally:
            shutil.rmtree(outside, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)


class TestExplicitSandboxFailClosed:
    """显式 macos/linux 不可用时必须拒绝执行，auto 才允许降级."""

    def _collect(self, generator):
        chunks = []
        while True:
            try:
                chunks.append(next(generator))
            except StopIteration as stop:
                return "".join(chunks), stop.value

    def test_explicit_sandbox_unavailable_refuses_execution(self, monkeypatch) -> None:
        work = tempfile.mkdtemp()
        spawned = []

        monkeypatch.setattr(
            sandbox,
            "wrap_command",
            lambda cmd, root, mode: (cmd, False, "unavailable"),
        )
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *args, **kwargs: spawned.append(args),
        )

        text, result = self._collect(code_run(
            "print('must not run')",
            code_type="python",
            cwd=work,
            sandbox_mode="macos",
            sandbox_root=work,
            timeout=10,
        ))

        assert result["status"] == "error"
        assert result["msg"].startswith("sandbox macos unavailable")
        assert "Sandbox macos unavailable" in text
        assert spawned == []

    def test_explicit_sandbox_refusal_removes_the_temp_script(self, monkeypatch) -> None:
        """Refusing to run must still clean the generated .ai.py file."""
        work = tempfile.mkdtemp()

        monkeypatch.setattr(
            sandbox,
            "wrap_command",
            lambda cmd, root, mode: (cmd, False, "unavailable"),
        )

        _, result = self._collect(code_run(
            "print('never runs')",
            code_type="python",
            cwd=work,
            code_cwd=work,
            sandbox_mode="macos",
            sandbox_root=work,
            timeout=10,
        ))

        assert result["status"] == "error"
        assert [name for name in os.listdir(work) if name.endswith(".ai.py")] == []

    def test_auto_mode_degrades_and_still_runs(self, monkeypatch) -> None:
        work = tempfile.mkdtemp()

        monkeypatch.setattr(
            sandbox,
            "wrap_command",
            lambda cmd, root, mode: (cmd, False, "unavailable"),
        )

        _, result = self._collect(code_run(
            "print('auto-ok')",
            code_type="python",
            cwd=work,
            sandbox_mode="auto",
            sandbox_root=work,
            timeout=30,
        ))

        assert result["status"] == "success"
        assert "auto-ok" in result["stdout"]
