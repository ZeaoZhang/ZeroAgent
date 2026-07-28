"""Tests for Phase 6 — worldline config, at_complete, plan_state."""

import os
import tempfile

import pytest


def test_enable_worldline_defaults_false():
    """enable_worldline defaults to False in AgentConfig."""
    from zero_agent.core.config import AgentConfig, LLMBackendConfig

    config = AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="sk-test",
                api_base="https://api.openai.com/v1",
                model="gpt-test",
            )
        },
        default_backend="default",
    )
    assert config.enable_worldline is False


def test_enable_worldline_from_yaml(tmp_path):
    """enable_worldline: true in YAML loads correctly."""
    from zero_agent.core.config import AgentConfig

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        f"""
default_backend: primary
enable_worldline: true
llm_backends:
  primary:
    provider: openai
    api_key: sk-test
    api_base: https://api.openai.com/v1
    model: gpt-4o
""".lstrip(),
        encoding="utf-8",
    )
    config = AgentConfig.from_yaml(str(yaml_path))
    assert config.enable_worldline is True


def test_enable_worldline_from_env(monkeypatch):
    """ZA_ENABLE_WORLDLINE defaults false in from_env."""
    monkeypatch.setenv("ZA_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("ZA_LLM_MODEL", "test-model")
    monkeypatch.delenv("ZA_ENABLE_WORLDLINE", raising=False)
    from zero_agent.core.config import AgentConfig
    config = AgentConfig.from_env()
    assert config.enable_worldline is False


def test_plan_state_extract_checks_items():
    """plan_state.extract() parses [ ] / [x] checklists."""
    from zero_agent.frontends.plan_state import extract

    text = "- [ ] Task 1\n- [x] Task 2\n- [ ] Task 3"
    items = extract(text)
    assert len(items) == 3
    assert items[0] == ("Task 1", "open")
    assert items[1] == ("Task 2", "done")
    assert items[2] == ("Task 3", "open")


def test_plan_state_is_complete_when_all_done():
    """is_complete returns True only when no unchecked items remain."""
    from zero_agent.frontends.plan_state import is_complete

    assert is_complete([]) is True
    assert is_complete([("a", "done"), ("b", "done")]) is True
    assert is_complete([("a", "done"), ("b", "open")]) is False


def test_plan_state_reads_only_current_task_contract(tmp_path):
    from types import SimpleNamespace

    from zero_agent.core.types import TaskContract, TaskMode
    from zero_agent.frontends.plan_state import is_active, resolve_path

    plan_path = tmp_path / "plan.md"
    plan_path.write_text("- [ ] Task\n", encoding="utf-8")
    handler = SimpleNamespace(
        task_contract=TaskContract("task", "finish", TaskMode.PLAN, str(plan_path)),
        working={"in_plan_mode": "legacy-plan.md"},
    )
    agent = SimpleNamespace(handler=handler, working={"in_plan_mode": "legacy-agent.md"})

    assert is_active(agent) is True
    assert resolve_path(agent) == str(plan_path)


def test_plan_state_ignores_legacy_paths_and_transcript_mentions(tmp_path):
    from types import SimpleNamespace

    from zero_agent.core.types import TaskContract, TaskMode
    from zero_agent.frontends.plan_state import is_active, resolve_path

    legacy_path = tmp_path / "plan.md"
    legacy_path.write_text("- [ ] Legacy\n", encoding="utf-8")
    handler = SimpleNamespace(
        task_contract=TaskContract("task", "answer", TaskMode.OPEN),
        working={"in_plan_mode": str(legacy_path)},
    )
    agent = SimpleNamespace(handler=handler, working={"in_plan_mode": str(legacy_path)})

    assert is_active(agent) is False
    assert resolve_path(agent) is None


def test_worldline_import():
    """worldline module imports without error regardless of platform."""
    try:
        import zero_agent.frontends.worldline  # noqa: F401
    except ImportError as e:
        if "rich" in str(e):
            pass  # ok, rich is optional
        else:
            raise


def test_worldline_tracking_plugin_register():
    """worldline_tracking plugin register() succeeds."""
    from zero_agent.plugins.worldline_tracking import register
    from zero_agent.core.hooks import HookSystem

    hs = HookSystem()
    result = register(hs)
    assert result is True
