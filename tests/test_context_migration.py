from __future__ import annotations

import pytest

from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.migration import ContextMigrationService
from zero_agent.context.service import ContextService
from zero_agent.context.types import RuntimeContext


def _copy_builtin(source_root, source_name="code_review_principles.md"):
    from importlib import resources

    source = resources.files("zero_agent.assets").joinpath(
        f"memory_seed/sops/{source_name}"
    )
    target = source_root / "sops" / source_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


def test_migration_plan_does_not_initialize_target_and_quarantines_sensitive_text(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "global_mem.txt").write_text("Old note about the project.", encoding="utf-8")
    secret_value = "api_key=private-example-value"
    (source / "global_mem_insight.txt").write_text(secret_value, encoding="utf-8")
    _copy_builtin(source)
    project = source / "projects" / "unknown-project" / "project_memory.md"
    project.parent.mkdir(parents=True)
    project.write_text("An unbound project note.", encoding="utf-8")
    archive = source / "L4_raw_sessions" / "2026.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"PK\x03\x04archive")
    target = tmp_path / "context-target"
    service = ContextMigrationService(target)

    plan = service.create_plan(source, principal_id="alice")
    manifest_path = service.write_plan(plan, tmp_path / "migration.json")

    assert not target.exists()
    assert plan["source_file_count"] == 5
    assert plan["importable_count"] == 2
    assert plan["sensitive_quarantine_count"] == 1
    assert plan["manual_review_count"] == 2
    assert "private-example-value" not in manifest_path.read_text(encoding="utf-8")
    by_name = {item["source_relative_path"]: item for item in plan["items"]}
    assert by_name["sops/code_review_principles.md"]["disposition"] == "builtin_reference"
    assert by_name["projects/unknown-project/project_memory.md"]["disposition"] == "manual_review"
    assert by_name["L4_raw_sessions/2026.zip"]["disposition"] == "archive_pending"

    report = service.apply(manifest_path, principal_id="alice")
    replay = service.apply(manifest_path, principal_id="alice")

    assert report["imported_count"] == 2
    assert report["source_modified"] is False
    assert replay["status"] == "replayed"
    assert all((source / path).exists() for path in (
        "global_mem.txt",
        "global_mem_insight.txt",
        "projects/unknown-project/project_memory.md",
        "L4_raw_sessions/2026.zip",
        "sops/code_review_principles.md",
    ))
    store = LocalContextStore(target)
    objects = store.list_objects()
    assert {obj.status for obj in objects} == {"legacy_unverified", "sensitive_quarantined"}
    assert len(objects) == 2
    sensitive = next(obj for obj in objects if obj.status == "sensitive_quarantined")
    runtime = RuntimeContext("local", "alice", "session")
    context = ContextService(store, runtime, receipt_store=store)
    assert context.search("private-example-value")["results"] == []
    with pytest.raises(ContextError) as blocked:
        context.read(sensitive.uri)
    assert blocked.value.code == "PERMISSION_DENIED"
    verify = service.verify(manifest_path, principal_id="alice")
    assert verify["verified_count"] == 2
    assert verify["missing_or_changed"] == []
    assert not verify["complete"]


def test_migration_apply_rejects_changed_preimage_before_target_initialization(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    source_file = source / "global_mem.txt"
    source_file.write_text("Original.", encoding="utf-8")
    target = tmp_path / "context-target"
    service = ContextMigrationService(target)
    plan = service.create_plan(source, principal_id="alice")
    manifest = service.write_plan(plan, tmp_path / "migration.json")
    source_file.write_text("Changed after dry-run.", encoding="utf-8")

    with pytest.raises(ContextConflictError, match="preimage changed"):
        service.apply(manifest, principal_id="alice")

    assert not target.exists()


def test_migration_rejects_target_inside_or_equal_to_source(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "global_mem.txt").write_text("legacy", encoding="utf-8")
    nested_target = source / "context"
    service = ContextMigrationService(nested_target)
    plan = service.create_plan(source, principal_id="alice")
    manifest = service.write_plan(plan, tmp_path / "migration.json")

    with pytest.raises(ContextError, match="independent"):
        service.apply(manifest, principal_id="alice")
    assert not nested_target.exists()
