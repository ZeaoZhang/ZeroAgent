"""Regression tests for GenericAgent compatibility boundaries."""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from zero_agent.core.loop import AgentLoop
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.llm.sessions import LiteLLMSession
from zero_agent.memory.manager import MemoryManager
from zero_agent.tools.registry import ToolRegistry


ZA_ROOT = Path(__file__).resolve().parents[1]
GA_ROOT = ZA_ROOT.parent / "GenericAgent"


def _ga_schema(name: str) -> list[dict]:
    return json.loads((GA_ROOT / "assets" / name).read_text(encoding="utf-8"))


def _ga_asset(name: str) -> str:
    return (GA_ROOT / "assets" / name).read_text(encoding="utf-8")


def _za_asset(name: str) -> str:
    return (ZA_ROOT / "zero_agent" / "assets" / name).read_text(encoding="utf-8")


def _ga_reflect_module(name: str):
    path = GA_ROOT / "reflect" / name
    spec = importlib.util.spec_from_file_location(f"ga_reflect_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _za_reflect_module(name: str):
    return importlib.import_module(f"zero_agent.reflect.{Path(name).stem}")


def _ga_slash_module():
    spec = importlib.util.spec_from_file_location(
        "ga_slash_cmds_for_alignment",
        GA_ROOT / "frontends" / "slash_cmds.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_memory_context(
    workspace_dir: str,
    insight: str,
    suffix: str = "",
) -> str:
    return (
        "\n"
        f"cwd = {workspace_dir} (./)\n"
        "\n[Memory] (../memory)\n"
        f"{_ga_asset(f'insight_fixed_structure{suffix}.txt')}\n"
        "../memory/global_mem_insight.txt:\n"
        f"{insight}\n"
    )


def _make_session() -> LiteLLMSession:
    return LiteLLMSession(
        LLMBackendConfig(
            name="default",
            provider="openai",
            api_key="sk-test",
            api_base="https://api.openai.com/v1",
            model="gpt-test",
        )
    )


def _make_ga_tool_client():
    spec = importlib.util.spec_from_file_location(
        "ga_llmcore_for_alignment",
        GA_ROOT / "llmcore.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Backend:
        name = "ga-test"

    return module.ToolClient(Backend())


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


@pytest.mark.parametrize(
    ("name", "suffix"),
    [
        ("sys_prompt.txt", ""),
        ("sys_prompt_en.txt", "_en"),
        ("insight_fixed_structure.txt", ""),
        ("insight_fixed_structure_en.txt", "_en"),
        ("global_mem_insight_template.txt", ""),
        ("global_mem_insight_template_en.txt", "_en"),
    ],
)
def test_prompt_and_memory_assets_match_ga_exactly(name: str, suffix: str) -> None:
    assert _za_asset(name) == _ga_asset(name)


@pytest.mark.parametrize(("language", "suffix"), [("zh", ""), ("en", "_en")])
def test_memory_context_matches_ga_shape_exactly(
    tmp_path,
    language: str,
    suffix: str,
) -> None:
    memory_dir = tmp_path / "memory"
    workspace_dir = tmp_path / "workspace"
    mgr = MemoryManager(
        memory_dir=str(memory_dir),
        workspace_dir=str(workspace_dir),
        language=language,
    )
    mgr.init_memory()

    insight = (memory_dir / "global_mem_insight.txt").read_text(encoding="utf-8")

    assert insight == _ga_asset(f"global_mem_insight_template{suffix}.txt")
    assert mgr.get_global_memory_context() == _expected_memory_context(
        str(workspace_dir),
        insight,
        suffix,
    )


@pytest.mark.parametrize(("language", "suffix"), [("zh", ""), ("en", "_en")])
def test_default_system_prompt_matches_ga_composition_exactly(
    tmp_path,
    monkeypatch,
    language: str,
    suffix: str,
) -> None:
    class FakeClient:
        extra_sys_prompt = ""

    monkeypatch.setattr(
        "zero_agent.core.agent.LLMFactory.create_all_sessions",
        lambda config: {"default": FakeClient()},
    )
    import zero_agent.core.agent as agent_module

    monkeypatch.setattr(agent_module.time, "strftime", lambda fmt: "2099-01-02 Thu")

    config = AgentConfig(
        language=language,
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="k",
                api_base="https://x.com",
                model="test-model",
            ),
        },
        workspace_dir=str(tmp_path / "workspace"),
        memory_dir=str(tmp_path / "memory"),
    )

    agent = ZeroAgent(config=config)
    agent.memory.init_memory()
    prompt = agent._build_system_prompt()
    insight = (
        Path(config.memory_dir) / "global_mem_insight.txt"
    ).read_text(encoding="utf-8")

    assert prompt == (
        _ga_asset(f"sys_prompt{suffix}.txt")
        + "\nToday: 2099-01-02 Thu\n"
        + _expected_memory_context(config.workspace_dir, insight, suffix)
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_tool_protocol_prompt_matches_ga_exactly(monkeypatch, language: str) -> None:
    monkeypatch.delenv("ZA_LANG", raising=False)
    if language == "en":
        monkeypatch.setenv("GA_LANG", "en")
    else:
        monkeypatch.delenv("GA_LANG", raising=False)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    za_session = _make_session()
    ga_client = _make_ga_tool_client()

    assert za_session._prepare_tool_instruction(tools) == ga_client._prepare_tool_instruction(tools)
    assert za_session._prepare_tool_instruction(tools) == ga_client._prepare_tool_instruction(tools)


def test_next_turn_messages_keep_ga_tool_results_shape() -> None:
    tool_results = [{"tool_use_id": "call_1", "content": '{"ok": true}'}]

    assert AgentLoop._build_next_messages("continue", tool_results) == [
        {
            "role": "user",
            "content": "continue",
            "tool_results": tool_results,
        }
    ]


def test_reflect_autonomous_prompt_matches_ga_exactly() -> None:
    ga = _ga_reflect_module("autonomous.py")
    za = _za_reflect_module("autonomous.py")

    assert za.INTERVAL == ga.INTERVAL
    assert za.ONCE == ga.ONCE
    assert za.check() == ga.check()


def test_reflect_goal_mode_prompts_match_ga_exactly() -> None:
    ga = _ga_reflect_module("goal_mode.py")
    za = _za_reflect_module("goal_mode.py")

    assert za.CONTINUATION_PROMPT == ga.CONTINUATION_PROMPT
    assert za.BUDGET_LIMIT_PROMPT == ga.BUDGET_LIMIT_PROMPT


def test_reflect_team_worker_prompt_matches_ga_exactly() -> None:
    ga = _ga_reflect_module("agent_team_worker.py")
    za = _za_reflect_module("agent_team_worker.py")
    for module in (ga, za):
        module.base_url = "https://bbs.example"
        module.board_key = "secret"
        module.name = "worker-a"

    assert za._prompt() == ga._prompt()


def test_reflect_checklist_master_prompt_matches_ga_exactly() -> None:
    ga = _ga_reflect_module("checklist_master.py")
    za = _za_reflect_module("checklist_master.py")
    data = {
        "goal": "ship",
        "bbs": {"url": "https://bbs.example", "key": "secret"},
        "tasks": [{"result": None}],
    }
    new_posts = [{"id": 1, "title": "reply"}]

    assert za._prompt(data, new_posts) == ga._prompt(data, new_posts)


def test_reflect_scheduler_prompt_text_keeps_ga_task_contract() -> None:
    ga_source = (GA_ROOT / "reflect" / "scheduler.py").read_text(encoding="utf-8")
    za_source = (
        ZA_ROOT / "zero_agent" / "reflect" / "scheduler.py"
    ).read_text(encoding="utf-8")
    snippets = [
        "f'[定时任务] {tid}\\n'",
        "f'[报告路径] {rpt}\\n\\n'",
        "f'先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\\n\\n'",
        "f'完成后将执行报告写入 {rpt}。'",
    ]

    for snippet in snippets:
        assert snippet in ga_source
        assert snippet in za_source


@pytest.mark.parametrize("language", ["zh", "en"])
def test_common_slash_prompts_match_ga_exactly(monkeypatch, language: str) -> None:
    if language == "en":
        monkeypatch.setenv("GA_LANG", "en")
    else:
        monkeypatch.setenv("GA_LANG", "zh")

    from zero_agent.frontends import slash_cmds as za

    ga = _ga_slash_module()
    args = "extra scope"
    builders = [
        "build_autorun_prompt",
        "build_morphling_prompt",
        "build_goal_prompt",
        "build_hive_prompt",
        "build_conductor_prompt",
    ]

    for builder in builders:
        assert getattr(za, builder)(args) == getattr(ga, builder)(args)


def test_slash_product_specific_prompts_are_explicit_exceptions() -> None:
    from zero_agent.frontends import slash_cmds as za

    ga = _ga_slash_module()
    ga_commands = {entry[0] for entry in ga.PALETTE_ENTRIES}
    za_commands = {entry[0] for entry in za.PALETTE_ENTRIES}

    assert "/init" not in ga_commands
    assert "/init" in za_commands
    assert za.build_update_prompt("") != ga.build_update_prompt("")
    assert "ZeroAgent" in za.build_update_prompt("")
    assert "GenericAgent" in ga.build_update_prompt("")


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
