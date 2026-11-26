"""Tests for the user profile file and the memory_profile tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.memory.manager import MemoryManager
from zero_agent.tools.registry import ToolRegistry


def _config(tmp_path) -> AgentConfig:
    return AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="test",
                api_base="https://x",
                model="m",
            )
        },
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
    )


def _manager(tmp_path) -> MemoryManager:
    manager = MemoryManager(
        memory_dir=str(tmp_path / "mem"),
        workspace_dir=str(tmp_path / "ws"),
    )
    manager.init_memory()
    return manager


def _run(tool, args):
    gen = tool.handler(args, None, None)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


def test_init_memory_seeds_user_profile(tmp_path) -> None:
    manager = _manager(tmp_path)

    text = manager.read_user_profile()

    assert "## Preferences" in text
    assert "## Relationships" in text
    assert "## Commitments" in text


def test_init_memory_keeps_existing_profile(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.write_user_profile("# User Profile\n\n## Preferences\n- 喜欢黑咖啡\n")

    manager.init_memory()

    assert "- 喜欢黑咖啡" in manager.read_user_profile()


def test_profile_roundtrip_and_limits(tmp_path) -> None:
    manager = _manager(tmp_path)

    manager.write_user_profile("# User Profile\n\n## Preferences\n- 少糖\n")
    assert manager.read_user_profile() == "# User Profile\n\n## Preferences\n- 少糖\n"

    with pytest.raises(ValueError):
        manager.write_user_profile("bad\x00profile")

    with pytest.raises(ValueError):
        manager.write_user_profile("x" * 40000)


def test_memory_context_injects_user_profile(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.write_user_profile("# User Profile\n\n## Preferences\n- 喜欢黑咖啡\n")

    context = manager.get_global_memory_context()

    assert "[User Profile]" in context
    assert "- 喜欢黑咖啡" in context


def test_memory_context_skips_empty_profile(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.write_user_profile("\n\n")

    assert "[User Profile]" not in manager.get_global_memory_context()


def test_memory_profile_tool_add_remove_list(tmp_path) -> None:
    registry = ToolRegistry.with_builtins(_config(tmp_path))
    tool = registry.get("memory_profile")
    assert tool is not None

    assert _run(tool, {"action": "add", "text": "喜欢黑咖啡"}) == {
        "status": "success",
        "added": True,
    }
    profile = (tmp_path / "mem" / "user_profile.md").read_text(encoding="utf-8")
    assert profile.count("- 喜欢黑咖啡") == 1

    _run(tool, {"action": "add", "text": "喜欢黑咖啡"})
    profile = (tmp_path / "mem" / "user_profile.md").read_text(encoding="utf-8")
    assert profile.count("- 喜欢黑咖啡") == 1

    _run(tool, {"action": "add", "text": "妈妈生日 5/1", "section": "Commitments"})
    listed = _run(tool, {"action": "list"})["profile"]
    assert "- 喜欢黑咖啡" in listed
    assert "## Commitments\n\n- 妈妈生日 5/1" in listed

    assert _run(tool, {"action": "remove", "text": "喜欢黑咖啡"}) == {
        "status": "success",
        "removed": True,
    }
    assert "- 喜欢黑咖啡" not in _run(tool, {"action": "list"})["profile"]

    assert _run(tool, {"action": "remove", "text": "不存在"}) == {
        "status": "error",
        "msg": "line not found",
    }


def test_memory_profile_sections_are_independent(tmp_path) -> None:
    tool = ToolRegistry.with_builtins(_config(tmp_path)).get("memory_profile")
    text = "每周健身三次"

    _run(tool, {"action": "add", "text": text, "section": "Preferences"})
    _run(tool, {"action": "add", "text": text, "section": "Commitments"})

    profile_path = tmp_path / "mem" / "user_profile.md"
    profile = profile_path.read_text(encoding="utf-8")
    assert profile.count(f"- {text}") == 2

    assert _run(
        tool, {"action": "remove", "text": text, "section": "Commitments"},
    ) == {"status": "success", "removed": True}

    profile = profile_path.read_text(encoding="utf-8")
    assert profile.count(f"- {text}") == 1
    preferences = profile.split("## Commitments")[0]
    assert f"- {text}" in preferences

    assert _run(
        tool, {"action": "remove", "text": text, "section": "Commitments"},
    ) == {"status": "error", "msg": "line not found"}


def test_write_user_profile_is_atomic_under_concurrency(tmp_path) -> None:
    import os
    import threading

    manager = _manager(tmp_path)
    payloads = [f"payload-{index}-" + "x" * 8192 for index in range(8)]
    barrier = threading.Barrier(len(payloads))
    errors: list[BaseException] = []

    def write(payload: str) -> None:
        try:
            barrier.wait()
            manager.write_user_profile(payload)
        except BaseException as exc:  # noqa: BLE001 - 收集给主线程断言
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(payload,)) for payload in payloads
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert manager.read_user_profile() in payloads
    leftovers = [name for name in os.listdir(manager.memory_dir) if name.endswith(".tmp")]
    assert leftovers == []


def test_memory_profile_tool_validates_input(tmp_path) -> None:
    tool = ToolRegistry.with_builtins(_config(tmp_path)).get("memory_profile")

    assert _run(tool, {"action": "add"}) == {
        "status": "error",
        "msg": "text is required",
    }
    assert _run(tool, {"action": "add", "text": "x", "section": "Nope"}) == {
        "status": "error",
        "msg": "unknown section: Nope",
    }
    assert _run(tool, {"action": "explode"}) == {
        "status": "error",
        "msg": "unknown action: explode",
    }


def _seed_path(manager: MemoryManager, name: str) -> Path:
    return Path(manager.memory_dir) / "sops" / name


def test_unedited_legacy_seed_is_upgraded(tmp_path, monkeypatch) -> None:
    """Installs seeded by an older release must receive the new SOP text."""
    import hashlib

    from zero_agent.memory import manager as manager_module

    manager = _manager(tmp_path)
    legacy = b"# Plan Mode SOP\n\ncode_run({'inline_eval': True, ...})\n"
    _seed_path(manager, "plan_sop.md").write_bytes(legacy)
    monkeypatch.setattr(
        manager_module,
        "_LEGACY_SEED_SHA256",
        {"plan_sop.md": frozenset({hashlib.sha256(legacy).hexdigest()})},
    )

    manager.init_memory()

    upgraded = _seed_path(manager, "plan_sop.md").read_text(encoding="utf-8")
    assert "inline_eval" not in upgraded
    assert "enter_plan_mode(" in upgraded


def test_user_edited_seed_is_preserved(tmp_path) -> None:
    manager = _manager(tmp_path)
    custom = "# 我的计划 SOP\n\n用户自己写的流程\n"
    _seed_path(manager, "plan_sop.md").write_text(custom, encoding="utf-8")

    manager.init_memory()

    assert _seed_path(manager, "plan_sop.md").read_text(encoding="utf-8") == custom


def test_shipped_seed_hashes_cover_migrated_sops() -> None:
    from zero_agent.memory import manager as manager_module

    assert set(manager_module._LEGACY_SEED_SHA256) == {
        "plan_sop.md",
        "autonomous_operation_sop.md",
        "memory_management_sop.md",
    }
    for hashes in manager_module._LEGACY_SEED_SHA256.values():
        assert hashes
    assert "2d5e3810511b02bf01413c308db756761928adb06b2dc37f45846b27b24c4894" in (
        manager_module._LEGACY_SEED_SHA256["plan_sop.md"]
    )
    assert "fb8a38b1cf5c10a10d937783139ef493a4f252092f08edc0564deac23fc668e0" in (
        manager_module._LEGACY_SEED_SHA256["memory_management_sop.md"]
    )
