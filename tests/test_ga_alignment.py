"""Regression tests for GenericAgent compatibility boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.tools.registry import ToolRegistry


GA_ROOT = Path(__file__).resolve().parents[2] / "GenericAgent"


def _ga_schema(name: str) -> list[dict]:
    return json.loads((GA_ROOT / "assets" / name).read_text(encoding="utf-8"))


def test_builtin_tool_schema_matches_ga_english_exactly(mock_config) -> None:
    assert (
        ToolRegistry.with_builtins(mock_config).generate_openai_schema()
        == _ga_schema("tools_schema.json")
    )


def test_builtin_tool_schema_matches_ga_chinese_exactly() -> None:
    config = AgentConfig(
        language="zh",
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="k",
                api_base="https://x.com",
                model="glm-4",
            ),
        },
        workspace_dir="/tmp/ws",
        memory_dir="/tmp/mem",
    )

    assert (
        ToolRegistry.with_builtins(config).generate_openai_schema()
        == _ga_schema("tools_schema_cn.json")
    )


def test_default_system_prompt_loads_ga_compatible_asset(mock_config, monkeypatch) -> None:
    class FakeClient:
        extra_sys_prompt = ""

    monkeypatch.setattr(
        "zero_agent.core.agent.LLMFactory.create_all_sessions",
        lambda config: {"default": FakeClient()},
    )
    agent = ZeroAgent(config=mock_config)
    prompt = agent._build_system_prompt()

    assert "物理级全能执行者" in prompt or "Physical-Level Omnipotent Executor" in prompt
    assert "<summary>" in prompt
    assert "不可逆" in prompt or "irreversible" in prompt
    assert str(mock_config.workspace_dir) in prompt


def test_memory_resources_do_not_contain_generated_python_cache_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(
        ["git", "ls-files", "zero_agent/memory"],
        cwd=root,
        text=True,
    ).splitlines()
    generated = [
        path
        for path in tracked
        if "__pycache__" in Path(path).parts or Path(path).suffix == ".pyc"
    ]

    assert generated == []
