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
