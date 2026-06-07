"""Regression tests for GenericAgent compatibility boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from zero_agent.core.agent import ZeroAgent
from zero_agent.tools.registry import ToolRegistry


GA_ROOT = Path(__file__).resolve().parents[2] / "GenericAgent"


def _tools_by_name(tools: list[dict]) -> dict[str, dict]:
    return {tool["function"]["name"]: tool["function"] for tool in tools}


def test_builtin_tool_schema_preserves_ga_core_surface(mock_config) -> None:
    ga_tools = _tools_by_name(
        json.loads((GA_ROOT / "assets" / "tools_schema.json").read_text(encoding="utf-8"))
    )
    za_tools = _tools_by_name(ToolRegistry.with_builtins(mock_config).generate_openai_schema())

    assert set(za_tools) == set(ga_tools)

    for name, ga_tool in ga_tools.items():
        ga_props = set(ga_tool["parameters"].get("properties", {}))
        za_params = za_tools[name]["parameters"]
        za_props = set(za_params.get("properties", {}))

        assert ga_props <= za_props, f"{name} missing GA parameters: {ga_props - za_props}"
        assert "required" not in za_params, f"{name} must keep GA fallback-friendly optional args"


def test_code_run_schema_keeps_ga_script_and_inline_eval_contract(mock_config) -> None:
    schema = _tools_by_name(ToolRegistry.with_builtins(mock_config).generate_openai_schema())
    props = schema["code_run"]["parameters"]["properties"]

    assert "script" in props
    assert props["script"]["type"] == "string"
    assert "inline_eval" in props
    assert props["inline_eval"]["type"] == "boolean"
    assert "python" in props["type"]["enum"]
    assert "powershell" in props["type"]["enum"]


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
