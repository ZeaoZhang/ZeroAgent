from __future__ import annotations

from pathlib import Path

import pytest

from zero_agent.context.config import ContextConfig
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.migration import ContextMigrationService, legacy_uri_for_relative
from zero_agent.context.service import ContextService
from zero_agent.context.types import ContextObject, RuntimeContext
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.types import StepOutcome
from zero_agent.tools.builtin.file import register_file_tools
from zero_agent.tools.registry import ToolRegistry


class _Handler:
    def __init__(self, service: ContextService) -> None:
        self.context_service = service
        self.runtime_context = service.runtime
        self.current_turn = 1
        self._file_read_cache = {}


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="test-key",
                api_base="https://api.openai.com/v1",
                model="test-model",
            ),
        },
        workspace_dir=str(tmp_path / "workspace"),
        memory_dir=str(tmp_path / "legacy-memory"),
        context=ContextConfig(mode="local_v1", root=str(tmp_path / "context-data")),
    )


def _run(tool, args, handler):
    generator = tool.handler(args, None, handler)
    progress = []
    while True:
        try:
            progress.append(next(generator))
        except StopIteration as stop:
            return progress, stop.value


def _runtime(config: AgentConfig) -> tuple[ToolRegistry, _Handler]:
    store = LocalContextStore(config.context.root)
    runtime = RuntimeContext("local", "alice", "test-session")
    service = ContextService(store, runtime, receipt_store=store)
    registry = ToolRegistry()
    register_file_tools(registry, config)
    return registry, _Handler(service)


def test_file_read_legacy_alias_uses_migrated_context_object(tmp_path) -> None:
    config = _config(tmp_path)
    source = Path(config.memory_dir)
    source.mkdir(parents=True)
    (source / "global_mem.txt").write_text("Verified legacy note.", encoding="utf-8")
    plan = ContextMigrationService(config.context.root).create_plan(source, principal_id="alice")
    manifest = ContextMigrationService(config.context.root).write_plan(plan, tmp_path / "migration.json")
    ContextMigrationService(config.context.root).apply(manifest, principal_id="alice")
    registry, handler = _runtime(config)

    progress, result = _run(
        registry.get("file_read"),
        {"path": str(source / "global_mem.txt"), "show_linenos": False},
        handler,
    )

    assert "legacy-memory" not in "".join(progress)
    if isinstance(result, StepOutcome):
        result = result.data
    assert "Verified legacy note." in result
    assert legacy_uri_for_relative("alice", "global_mem.txt") in result
    assert "revision=1" in result


def test_file_read_denies_sensitive_quarantined_legacy_alias(tmp_path) -> None:
    config = _config(tmp_path)
    source = Path(config.memory_dir)
    source.mkdir(parents=True)
    (source / "global_mem.txt").write_text("api_key=private-example-value", encoding="utf-8")
    migration = ContextMigrationService(config.context.root)
    manifest = migration.write_plan(
        migration.create_plan(source, principal_id="alice"),
        tmp_path / "migration.json",
    )
    migration.apply(manifest, principal_id="alice")
    registry, handler = _runtime(config)

    _, result = _run(
        registry.get("file_read"),
        {"path": str(source / "global_mem.txt"), "show_linenos": False},
        handler,
    )

    if isinstance(result, StepOutcome):
        result = result.data
    assert result.startswith("Error:")
    assert "PERMISSION_DENIED" in result
    assert "private-example-value" not in result


@pytest.mark.parametrize("tool_name", ["file_write", "file_patch"])
@pytest.mark.parametrize("managed_target", ["legacy", "context"])
def test_file_writes_to_managed_roots_return_service_error_without_mutation(
    tmp_path, tool_name, managed_target
) -> None:
    config = _config(tmp_path)
    source = Path(config.memory_dir)
    source.mkdir(parents=True)
    legacy_file = source / "global_mem.txt"
    legacy_file.write_text("Original legacy note.", encoding="utf-8")
    registry, handler = _runtime(config)
    context_file = Path(config.context.root) / "objects" / "opaque-content.md"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text("Opaque managed body.", encoding="utf-8")
    target = legacy_file if managed_target == "legacy" else context_file
    args = (
        {"path": str(target), "content": "Replacement"}
        if tool_name == "file_write"
        else {"path": str(target), "old_content": "Original", "new_content": "Replacement"}
    )

    progress, result = _run(registry.get(tool_name), args, handler)

    assert "legacy-memory" not in "".join(progress)
    assert "context-data" not in "".join(progress)
    error = result.data if isinstance(result, StepOutcome) else result
    assert error["status"] == "error"
    assert error["code"] == "MEMORY_WRITE_REQUIRES_SERVICE"
    assert target.read_text(encoding="utf-8") in {"Original legacy note.", "Opaque managed body."}


def test_file_write_through_workspace_symlink_into_managed_memory_is_blocked(tmp_path) -> None:
    config = _config(tmp_path)
    workspace = Path(config.workspace_dir)
    workspace.mkdir(parents=True)
    legacy = Path(config.memory_dir)
    legacy.mkdir(parents=True)
    target = legacy / "global_mem.txt"
    target.write_text("Keep this note.", encoding="utf-8")
    alias = workspace / "legacy-link.txt"
    alias.symlink_to(target)
    registry, handler = _runtime(config)

    _, result = _run(
        registry.get("file_write"),
        {"path": str(alias), "content": "Do not replace"},
        handler,
    )

    error = result.data if isinstance(result, StepOutcome) else result
    assert error["code"] == "MEMORY_WRITE_REQUIRES_SERVICE"
    assert target.read_text(encoding="utf-8") == "Keep this note."


def test_file_write_to_ordinary_workspace_path_still_works(tmp_path) -> None:
    config = _config(tmp_path)
    registry, handler = _runtime(config)
    target = Path(config.workspace_dir) / "notes" / "ordinary.txt"

    _, result = _run(
        registry.get("file_write"),
        {"path": str(target), "content": "ordinary workspace content"},
        handler,
    )

    assert result["status"] == "success"
    assert target.read_text(encoding="utf-8") == "ordinary workspace content"


def test_file_read_of_context_store_physical_object_uses_logical_service(tmp_path) -> None:
    config = _config(tmp_path)
    registry, handler = _runtime(config)
    uri = legacy_uri_for_relative("alice", "migrated.md")
    obj = ContextObject(
        object_id="legacy-object",
        uri=uri,
        kind="resource",
        owner_id="alice",
        title="Migrated",
        content="Committed managed content.",
        status="legacy_unverified",
    )
    handler.context_service.store.commit(obj, expected_revision=None)
    path = next(Path(config.context.root).rglob("1-*.md"))

    progress, result = _run(
        registry.get("file_read"),
        {"path": str(path), "show_linenos": False},
        handler,
    )

    assert "context-data" not in "".join(progress)
    assert "Committed managed content." in result
    assert uri in result
