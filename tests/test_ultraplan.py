"""Tests for Phase 4 assets — SOPs, UltraPlan, goal mode, packaging."""

import os

import pytest


def test_ultraplan_sop_contains_keyword():
    """ultraplan_sop.md contains 'UltraPlan' keyword."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "zero_agent", "assets", "memory_seed", "sops", "ultraplan_sop.md",
    )
    content = open(path, encoding="utf-8").read()
    assert "UltraPlan" in content
    # Should reference the ZA import path
    assert "zero_agent.assets.ga_ultraplan" in content


def test_project_mode_sop_contains_keyword():
    """project_mode_sop.md contains 'PROJECT MODE' reference."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "zero_agent", "assets", "memory_seed", "sops", "project_mode_sop.md",
    )
    content = open(path, encoding="utf-8").read()
    assert "Project Mode" in content


def test_computer_use_contains_macos():
    """computer_use.md contains 'macOS' section."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "zero_agent", "assets", "memory_seed", "sops", "computer_use.md",
    )
    content = open(path, encoding="utf-8").read()
    assert "macOS" in content
    assert "zero_agent.utils.macljqctrl" in content


def test_ultraplan_py_imports():
    """ga_ultraplan.py has __all__ with plan, phase, parallel, mapchain."""
    from zero_agent.assets.ga_ultraplan import __all__ as up_all
    assert "plan" in up_all
    assert "phase" in up_all
    assert "parallel" in up_all
    assert "mapchain" in up_all

def test_ultraplan_subagent_propagates_config_and_keeps_logs(monkeypatch, tmp_path) -> None:
    """UltraPlan worker 应继承 ZA_CONFIG_PATH，且不再禁用日志。"""
    import zero_agent.assets.ga_ultraplan as up

    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0
        pid = 12345

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return "", ""

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setenv("ZA_CONFIG_PATH", str(tmp_path / "live.yaml"))
    monkeypatch.setattr(up.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(up, "_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(up, "_TASK_SLUG", "case")
    monkeypatch.setattr(up, "_FUNC_SEQ", 0)

    output = up._subagent("worker", llm_no=2, timeout=17)

    cmd = captured["cmd"]
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == str(tmp_path / "live.yaml")
    assert "--nolog" not in cmd
    assert "--no-user-tools" in cmd
    assert captured["timeout"] == 17
    assert captured["kwargs"]["start_new_session"] is True
    assert output.endswith(".out.txt")


def test_macljqctrl_import_does_not_crash():
    """macljqctrl module imports without crashing."""
    # On macOS with Quartz available, this works normally.
    # On non-macOS platforms, the import may fail or print errors.
    try:
        import zero_agent.utils.macljqctrl  # noqa: F401
    except ImportError:
        # Allowed: platform dependency
        pass


def test_ljqctrl_bg_import_does_not_crash():
    """ljqctrl_bg module imports without crashing."""
    try:
        import zero_agent.utils.ljqctrl_bg  # noqa: F401
    except ImportError:
        pass


def test_goal_mode_continuation_has_phases():
    """Goal mode CONTINUATION_PROMPT contains 创造阶段, 检验阶段, 改进阶段."""
    from zero_agent.reflect.goal_mode import CONTINUATION_PROMPT
    assert "创造阶段" in CONTINUATION_PROMPT
    assert "检验阶段" in CONTINUATION_PROMPT
    assert "改进阶段" in CONTINUATION_PROMPT


def test_project_mode_plugin_register():
    """project_mode plugin register() is callable and returns True."""
    from zero_agent.plugins.project_mode import register
    from zero_agent.core.hooks import HookSystem
    hs = HookSystem()
    result = register(hs)
    assert result is True
