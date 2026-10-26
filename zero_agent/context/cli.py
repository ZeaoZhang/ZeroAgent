"""Host-side command dispatch for context administration."""

from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

from zero_agent.context.admin import ContextAdminService
from zero_agent.context.errors import ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.migration import ContextMigrationService
from zero_agent.context.resources import ResourceService
from zero_agent.context.service import ContextService
from zero_agent.context.sources import EvidenceReceiptService
from zero_agent.context.types import RuntimeContext
from zero_agent.core.config import AgentConfig


def run_context_command(
    config: AgentConfig,
    command: str,
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one explicit host command through the shared admin service."""

    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        admin: ContextAdminService | None = None

        def get_admin() -> ContextAdminService:
            nonlocal admin
            if admin is None:
                admin = ContextAdminService(LocalContextStore(config.context.root))
            return admin

        actor = config.context.local_user_id
        if command == "project_create":
            result = get_admin().create_project(actor, args.path, name=args.name or "")
            payload = result.__dict__
        elif command == "project_rebind":
            result = get_admin().rebind_project(
                actor, args.id, args.path, expected_revision=args.expected_revision,
            )
            payload = result.__dict__
        elif command == "project_grant":
            result = get_admin().grant_project(
                actor, args.id, args.principal, args.role,
                expected_revision=args.expected_revision,
            )
            payload = result.__dict__
        elif command == "project_revoke":
            result = get_admin().revoke_project(
                actor, args.id, args.principal,
                expected_revision=args.expected_revision,
            )
            payload = result.__dict__
        elif command == "project_transfer_owner":
            result = get_admin().transfer_project_owner(
                actor, args.id, args.to, expected_revision=args.expected_revision,
            )
            payload = result.__dict__
        elif command == "identity_list":
            payload = [item.__dict__ for item in get_admin().list_identities(actor)]
        elif command == "identity_link_begin":
            platform, account = _parse_target(args.target)
            challenge = get_admin().begin_identity_link(actor, platform, account)
            payload = {
                "target": f"{challenge.target_platform}/{challenge.target_bot_account_id}",
                "code": challenge.code,
                "expires_at": challenge.expires_at,
                "redeem_instruction": "Send /link <code> in a private chat with the specified bot before expiry.",
            }
        elif command == "identity_link_revoke":
            platform, account = _parse_target(args.target)
            result = get_admin().revoke_identity_link(
                actor,
                platform=platform,
                bot_account_id=account,
                principal_id=args.principal,
                expected_revision=args.expected_revision,
            )
            payload = result.__dict__
        elif command == "migration":
            migration = ContextMigrationService(config.context.root)
            if args.dry_run:
                if not args.source or not args.manifest:
                    raise ContextError("INVALID_SOURCE", "--dry-run requires both --source and --manifest.")
                plan = migration.create_plan(args.source, principal_id=actor)
                manifest_path = migration.write_plan(plan, args.manifest)
                payload = {
                    "migration_id": plan["migration_id"],
                    "manifest": str(manifest_path),
                    "source_file_count": plan["source_file_count"],
                    "importable_count": plan["importable_count"],
                    "sensitive_quarantine_count": plan["sensitive_quarantine_count"],
                    "manual_review_count": plan["manual_review_count"],
                    "target_initialized": (Path(config.context.root).expanduser() / "manifest.sqlite").is_file(),
                }
            else:
                if not args.apply_manifest:
                    raise ContextError("INVALID_SOURCE", "--apply requires a migration manifest path.")
                payload = migration.apply(args.apply_manifest, principal_id=actor)
        elif command == "migration_apply":
            payload = ContextMigrationService(config.context.root).apply(args.manifest, principal_id=actor)
        elif command == "migration_verify":
            payload = ContextMigrationService(config.context.root).verify(args.manifest, principal_id=actor)
        elif command == "context_gc":
            if args.grace_seconds < 0:
                raise ContextError("INVALID_SOURCE", "--grace-seconds must not be negative")
            manifest = Path(config.context.root).expanduser() / "manifest.sqlite"
            if not manifest.is_file():
                raise ContextError("NOT_FOUND", "The context manifest was not found.")
            store = LocalContextStore(config.context.root)
            payload = store.collect_orphans(grace_seconds=args.grace_seconds, dry_run=not args.apply)
        elif command == "context_export":
            payload = _export_visible_context(config, args.output, project_id=args.project_id)
        elif command == "context_reindex":
            payload = _verify_current_content(config)
        elif command == "resource_import_file":
            service = _resource_service(get_admin(), args.project_id, actor)
            obj = service.import_file(
                args.path,
                target_uri=args.target_uri,
                title=args.title,
                expected_revision=args.expected_revision,
                idempotency_key=args.idempotency_key,
            )
            payload = {
                "uri": obj.uri,
                "revision": obj.revision,
                "content_sha256": obj.content_sha256,
                "status": obj.status,
            }
        elif command == "resource_import_directory":
            service = _resource_service(get_admin(), args.project_id, actor)
            result = service.import_directory(
                args.path,
                target_prefix=args.target_prefix,
                idempotency_key=args.idempotency_key,
            )
            payload = {
                "imported": [
                    {"uri": obj.uri, "revision": obj.revision, "content_sha256": obj.content_sha256}
                    for obj in result.imported
                ],
                "skipped": [{"relative_path": path, "reason": reason} for path, reason in result.skipped],
            }
        else:
            print("error [INVALID_SOURCE]: unsupported context command", file=err)
            return 2
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=out)
        if command == "context_gc" and int(payload.get("failed_files", 0)) > 0:
            return 1
        return 0
    except ContextError as exc:
        print(f"error [{exc.code}]: {exc}", file=err)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error [INVALID_SOURCE]: {exc}", file=err)
        return 2


def _parse_target(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or "/" not in value:
        raise ContextError("INVALID_SOURCE", "The bot target must be platform/bot-account.")
    platform, account = value.split("/", 1)
    if not platform or not account:
        raise ContextError("INVALID_SOURCE", "The bot target must be platform/bot-account.")
    return platform, account


def _export_visible_context(
    config: AgentConfig,
    output: str,
    *,
    project_id: str | None = None,
) -> dict[str, object]:
    store = LocalContextStore(config.context.root)
    runtime = RuntimeContext("local", config.context.local_user_id, "context-export")
    admin = ContextAdminService(store)
    if project_id:
        access = admin.project_access(project_id, config.context.local_user_id)
        if access is None:
            raise ContextError("PERMISSION_DENIED", "The current principal has no grant for this project.")
        runtime = RuntimeContext(
            "local", config.context.local_user_id, "context-export", project_id=project_id,
            capabilities=admin.capabilities_for_project(project_id, config.context.local_user_id),
        )
    receipts = EvidenceReceiptService(store)
    service = ContextService(store, runtime, sources=receipts, receipt_store=store)
    user_id = quote(config.context.local_user_id, safe="-._~")
    scopes = [f"za://users/{user_id}/memories"]
    if project_id:
        scopes.extend((f"za://projects/{project_id}/memories", f"za://projects/{project_id}/resources"))
    revisions: dict[str, int] = {}
    for scope in scopes:
        cursor = None
        while True:
            page = service.list(scope, cursor=cursor, limit=500)
            revisions.update({obj.uri: obj.revision for obj in page["items"]})
            cursor = page["next_cursor"]
            if cursor is None:
                break
    destination = Path(output).expanduser()
    if destination.is_symlink():
        raise ContextError("INVALID_SOURCE", "The export destination cannot be a symlink.")
    exported = store.export_json(destination, uris=set(revisions), expected_revisions=revisions, runtime=runtime)
    digest = hashlib.sha256(exported.read_bytes()).hexdigest()
    return {"output": str(exported), "object_count": len(revisions), "sha256": digest, "source_modified": False}


def _verify_current_content(config: AgentConfig) -> dict[str, object]:
    manifest = Path(config.context.root).expanduser() / "manifest.sqlite"
    if not manifest.is_file():
        raise ContextError("NOT_FOUND", "The context store is not initialized.")
    store = LocalContextStore(config.context.root)
    for obj in store.list_objects():
        if store.get(obj.uri, obj.revision) is None:
            raise ContextError("INTEGRITY_ERROR", "A committed revision is missing.")
    return {
        "verified_objects": len(store.list_objects()),
        "search_backend": config.context.search_backend,
        "persistent_index": False,
    }


def _resource_service(
    admin: ContextAdminService,
    project_id: str,
    principal_id: str,
) -> ResourceService:
    project = admin.get_project(project_id)
    access = admin.project_access(project_id, principal_id)
    if access is None or access.role == "reader":
        raise ContextError("PERMISSION_DENIED", "A project writer grant is required to import resources.")
    runtime = RuntimeContext(
        "local",
        principal_id,
        "context-resource-import",
        project_id=project_id,
        capabilities=admin.capabilities_for_project(project_id, principal_id),
    )
    return ResourceService(
        admin.store,
        EvidenceReceiptService(admin.store),
        runtime,
    )
