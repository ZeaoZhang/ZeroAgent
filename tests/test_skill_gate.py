from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from zero_agent.llm.base import MockResponse
from zero_agent.context.errors import ContextError
from zero_agent.core.handler import BaseHandler
from zero_agent.core.types import StepOutcome
from zero_agent.skills.gate import SkillActionGate
from zero_agent.skills.manifest import load_builtin_catalog
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _skill(skill_id: str):
    return next(skill for skill in load_builtin_catalog() if skill.skill_id == skill_id)


def _run(generator):
    while True:
        try:
            next(generator)
        except StopIteration as stopped:
            return stopped.value


def test_triggered_skill_must_be_routed_and_loaded_at_exact_revision() -> None:
    skill = _skill("code_review_principles")
    gate = SkillActionGate([skill], "请做代码审查，并提出修复建议。")

    assert gate.check("code.execute", tool_name="code_run", args={})["code"] == "SKILL_ROUTING_REQUIRED"
    gate.route([skill.skill_id], {skill.skill_id: "代码审查"})
    blocked = gate.check("code.execute", tool_name="code_run", args={})
    assert blocked["code"] == "TRIGGERED_SKILL_NOT_LOADED"
    gate.mark_loaded(skill.uri, "0" * 64)
    assert gate.check("code.execute", tool_name="code_run", args={})["code"] == "TRIGGERED_SKILL_NOT_LOADED"

    gate.mark_loaded(skill.uri, skill.revision)
    assert gate.check("code.execute", tool_name="code_run", args={}) is None

    with pytest.raises(ContextError, match="locked"):
        gate.route([], {})



def test_triggered_skill_cannot_be_omitted_from_route() -> None:
    skill = _skill("code_review_principles")
    gate = SkillActionGate([skill], "请做代码审查，并提出修复建议。")

    with pytest.raises(ContextError) as error:
        gate.route([], {})

    assert error.value.code == "SKILL_ROUTING_REQUIRED"
    assert gate.check("code.execute", tool_name="code_run", args={})["code"] == "SKILL_ROUTING_REQUIRED"


def test_every_matching_skill_must_be_routed() -> None:
    catalog = load_builtin_catalog()
    review = next(skill for skill in catalog if skill.skill_id == "code_review_principles")
    ultraplan = next(skill for skill in catalog if skill.skill_id == "ultraplan_sop")
    gate = SkillActionGate(catalog, "请做代码审查，并启用 UltraPlan。")

    with pytest.raises(ContextError) as error:
        gate.route([review.skill_id], {review.skill_id: "代码审查"})

    assert error.value.code == "SKILL_ROUTING_REQUIRED"
    assert ultraplan.skill_id in str(error.value)


def test_project_change_review_request_requires_code_quality_rule() -> None:
    skill = _skill("code_review_principles")
    gate = SkillActionGate([skill], "检查当前项目的修改是否符合预期呢？")

    with pytest.raises(ContextError) as error:
        gate.route([], {})

    assert error.value.code == "SKILL_ROUTING_REQUIRED"


def test_matching_reference_resource_must_be_routed() -> None:
    catalog = load_builtin_catalog()
    reference = next(skill for skill in catalog if skill.skill_id == "vue3_component_sop")
    gate = SkillActionGate(catalog, "Vue 3 页面需要理解组件状态。")

    with pytest.raises(ContextError) as error:
        gate.route([], {})

    assert error.value.code == "SKILL_ROUTING_REQUIRED"
    assert reference.skill_id in str(error.value)


def test_untriggered_plain_text_completion_and_empty_route_are_allowed() -> None:
    gate = SkillActionGate(load_builtin_catalog(), "把这个短句翻译成英文。")

    assert gate.check("task.complete", tool_name="complete_task", args={}) is None
    gate.route([], {})
    assert gate.check("code.execute", tool_name="code_run", args={}) is None

def test_complete_task_dispatch_requires_loaded_matching_rule() -> None:
    skill = _skill("code_review_principles")
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="complete_task",
        description="Complete a task",
        parameters={"type": "object", "properties": {}},
        handler=lambda _args, _response, _handler: None,
        skill_action="task.complete",
    ))
    handler = BaseHandler(registry=registry)
    handler.skill_action_gate = SkillActionGate([skill], "请做代码审查")
    handler.skill_action_gate.route([skill.skill_id], {skill.skill_id: "代码审查"})

    result = _run(handler.dispatch("complete_task", {}, None))

    assert result.data["code"] == "TRIGGERED_SKILL_NOT_LOADED"


def test_triggered_skill_cannot_be_omitted_from_plain_text_completion() -> None:
    skill = _skill("code_review_principles")
    handler = BaseHandler(registry=ToolRegistry())
    handler.skill_action_gate = SkillActionGate([skill], "请做代码审查，并提出修复建议。")

    result = _run(handler.do_no_tool({}, MockResponse(content="审查完成。")))

    assert result.data["code"] == "SKILL_ROUTING_REQUIRED"


def test_handler_blocks_action_until_triggered_skill_is_loaded() -> None:
    skill = _skill("code_review_principles")
    called = []

    def action(_args, _response, _handler):
        called.append(True)
        if False:
            yield ""
        return {"status": "success", "exit_code": 0}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="code_run",
        description="Run test action",
        parameters={"type": "object", "properties": {}},
        handler=action,
        evidence_kind="execute",
        skill_action="code.execute",
    ))
    handler = BaseHandler(registry=registry)
    handler.skill_action_gate = SkillActionGate([skill], "请做代码审查")

    blocked_before_route = _run(handler.dispatch("code_run", {}, None))
    assert isinstance(blocked_before_route, StepOutcome)
    assert blocked_before_route.data["code"] == "SKILL_ROUTING_REQUIRED"
    assert not called

    handler.skill_action_gate.route([skill.skill_id], {skill.skill_id: "代码审查"})
    blocked_before_load = _run(handler.dispatch("code_run", {}, None))
    assert blocked_before_load.data["code"] == "TRIGGERED_SKILL_NOT_LOADED"
    assert not called

    handler.skill_action_gate.mark_loaded(skill.uri, skill.revision)
    allowed = _run(handler.dispatch("code_run", {}, None))
    assert allowed.data == {"status": "success", "exit_code": 0}
    assert called == [True]

def test_build_validation_rejects_triggered_rule_without_trigger_terms(tmp_path, monkeypatch) -> None:
    from zero_agent.skills import manifest

    assets = _copy_skill_assets(Path(__file__).resolve().parents[1], tmp_path)
    catalog_path = assets / "context" / "builtin_catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog["skills"][0].pop("trigger_terms")
    catalog_path.write_text(
        yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(manifest.resources, "files", lambda _package: assets)

    with pytest.raises(ContextError, match="non-empty routing terms"):
        manifest.load_builtin_catalog()





def _copy_skill_assets(source_root: Path, target_root: Path) -> Path:
    source_assets = source_root / "zero_agent" / "assets"
    target_assets = target_root / "assets"
    shutil.copytree(source_assets / "context", target_assets / "context")
    shutil.copytree(source_assets / "memory_seed" / "sops", target_assets / "memory_seed" / "sops")
    return target_assets


def test_build_validation_rejects_unregistered_sop(tmp_path, monkeypatch) -> None:
    from zero_agent.skills import manifest

    assets = _copy_skill_assets(Path(__file__).resolve().parents[1], tmp_path)
    (assets / "memory_seed" / "sops" / "new_sop.md").write_text("# Unregistered\n", encoding="utf-8")
    monkeypatch.setattr(manifest.resources, "files", lambda _package: assets)

    with pytest.raises(ContextError, match="exactly one catalog entry"):
        manifest.load_builtin_catalog()


def test_build_validation_rejects_unmapped_source_section(tmp_path, monkeypatch) -> None:
    from zero_agent.skills import manifest

    assets = _copy_skill_assets(Path(__file__).resolve().parents[1], tmp_path)
    catalog_path = assets / "context" / "builtin_catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    first = catalog["skills"][0]
    first["requirements"].pop()
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(manifest.resources, "files", lambda _package: assets)

    with pytest.raises(ContextError, match="Every SOP section"):
        manifest.load_builtin_catalog()
