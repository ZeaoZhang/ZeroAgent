from __future__ import annotations

import io
import json

from zero_agent.context.cli import run_context_command
from zero_agent.context.config import ContextConfig
from zero_agent.context.admin import ContextAdminService
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.types import ContextObject
from zero_agent.core.config import AgentConfig
from zero_agent.runners.cli import _build_parser


def _config(root) -> AgentConfig:
    return AgentConfig(
        memory_dir=str(root / "legacy"),
        context=ContextConfig(mode="local_v1", root=str(root / "context"), local_user_id="alice"),
    )


def test_cli_parser_accepts_project_and_identity_admin_commands() -> None:
    parser = _build_parser()

    project = parser.parse_args([
        "context", "project", "create", "--path", "/tmp/project", "--name", "Demo",
    ])
    identity = parser.parse_args([
        "--config", "config.yaml", "context", "identity", "link", "begin",
        "--target", "telegram/bot-a",
    ])
    migration = parser.parse_args([
        "context", "migrate", "--dry-run", "--source", "/tmp/legacy",
        "--manifest", "/tmp/migration.json",
    ])
    resource = parser.parse_args([
        "context", "resource", "import-file", "--project-id", "p-1",
        "--path", "/tmp/readme.md", "--target-uri", "za://projects/p-1/resources/docs/readme",
    ])

    assert project.context_admin_command == "project_create"
    assert identity.context_admin_command == "identity_link_begin"
    assert identity.config == "config.yaml"
    assert migration.context_admin_command == "migration"
    assert migration.dry_run is True
    assert resource.context_admin_command == "resource_import_file"


def test_context_cli_migration_dry_run_does_not_initialize_target(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "global_mem.txt").write_text("Old note.", encoding="utf-8")
    target = tmp_path / "context"
    stdout = io.StringIO()

    result = run_context_command(
        _config(tmp_path),
        "migration",
        type("Args", (), {
            "dry_run": True,
            "source": str(source),
            "manifest": str(tmp_path / "migration.json"),
            "apply_manifest": None,
        })(),
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert result == 0
    assert payload["target_initialized"] is False
    assert not target.exists()


def test_context_cli_creates_project_through_admin_service(tmp_path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    stdout, stderr = io.StringIO(), io.StringIO()

    result = run_context_command(
        _config(tmp_path),
        "project_create",
        type("Args", (), {"path": str(project_dir), "name": "Demo"})(),
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert result == 0
    assert stderr.getvalue() == ""
    assert payload["owner_id"] == "alice"
    assert payload["bound_path"] == str(project_dir.resolve())


def test_context_cli_returns_safe_error_for_stale_project_revision(tmp_path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = _config(tmp_path)
    create_out = io.StringIO()
    run_context_command(
        config,
        "project_create",
        type("Args", (), {"path": str(project_dir), "name": ""})(),
        stdout=create_out,
    )
    project = json.loads(create_out.getvalue())
    stdout, stderr = io.StringIO(), io.StringIO()

    result = run_context_command(
        config,
        "project_grant",
        type("Args", (), {
            "id": project["project_id"],
            "principal": "bob",
            "role": "reader",
            "expected_revision": 9,
        })(),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 2
    assert stdout.getvalue() == ""
    assert "REVISION_CONFLICT" in stderr.getvalue()
    assert "/private/" not in stderr.getvalue()


def test_resource_cli_import_requires_project_write_grant(tmp_path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source = tmp_path / "readme.md"
    source.write_text("A useful project reference.", encoding="utf-8")
    config = _config(tmp_path)
    admin = ContextAdminService(LocalContextStore(config.context.root))
    project = admin.create_project("alice", project_dir)
    stdout = io.StringIO()

    result = run_context_command(
        config,
        "resource_import_file",
        type("Args", (), {
            "project_id": project.project_id,
            "path": str(source),
            "target_uri": f"za://projects/{project.project_id}/resources/docs/readme",
            "title": "Readme",
            "expected_revision": None,
            "idempotency_key": "import-readme",
        })(),
        stdout=stdout,
    )

    assert result == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "active"
    assert LocalContextStore(config.context.root).get(payload["uri"]).content == "A useful project reference."

    admin.grant_project("alice", project.project_id, "bob", "reader", expected_revision=1)
    reader_config = _config(tmp_path)
    reader_config.context.local_user_id = "bob"
    reader_error = io.StringIO()
    result = run_context_command(
        reader_config,
        "resource_import_file",
        type("Args", (), {
            "project_id": project.project_id,
            "path": str(source),
            "target_uri": f"za://projects/{project.project_id}/resources/docs/second",
            "title": "Readme",
            "expected_revision": None,
            "idempotency_key": "reader-import",
        })(),
        stderr=reader_error,
    )
    assert result == 2
    assert "PERMISSION_DENIED" in reader_error.getvalue()


def test_context_export_follows_all_authorized_pages(tmp_path) -> None:
    config = _config(tmp_path)
    store = LocalContextStore(config.context.root)
    for index in range(501):
        uri = f"za://users/alice/memories/legacy/item-{index:04d}"
        store.commit(
            ContextObject(
                object_id=f"item-{index:04d}",
                uri=uri,
                kind="resource",
                owner_id="alice",
                title=f"Item {index}",
                content=f"Item body {index}.",
            ),
            expected_revision=None,
        )
    output = tmp_path / "export.json"
    stdout = io.StringIO()

    result = run_context_command(
        config,
        "context_export",
        type("Args", (), {"output": str(output), "project_id": None})(),
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert payload["object_count"] == 501
    assert len(exported["objects"]) == 501


def test_context_export_does_not_include_or_overwrite_other_principal_objects(tmp_path) -> None:
    config = _config(tmp_path)
    store = LocalContextStore(config.context.root)
    store.commit(
        ContextObject(
            object_id="alice-note", uri="za://users/alice/memories/facts/note",
            kind="environment_fact", owner_id="alice", content="Alice private note",
        ),
        expected_revision=None,
    )
    store.commit(
        ContextObject(
            object_id="bob-note", uri="za://users/bob/memories/facts/note",
            kind="environment_fact", owner_id="bob", content="Bob private note",
        ),
        expected_revision=None,
    )
    output = tmp_path / "alice-export.json"
    stdout = io.StringIO()

    result = run_context_command(
        config, "context_export",
        type("Args", (), {"output": str(output), "project_id": None})(),
        stdout=stdout,
    )

    exported = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert [obj["uri"] for obj in exported["objects"]] == [
        "za://users/alice/memories/facts/note",
    ]


def test_context_export_does_not_overwrite_uncataloged_destination(tmp_path) -> None:
    config = _config(tmp_path)
    output = tmp_path / "existing.json"
    original = b"user-owned bytes\n"
    output.write_bytes(original)
    stderr = io.StringIO()

    result = run_context_command(
        config, "context_export",
        type("Args", (), {"output": str(output), "project_id": None})(),
        stderr=stderr,
    )

    assert result == 2
    assert output.read_bytes() == original
    assert "INVALID_SOURCE" in stderr.getvalue()
