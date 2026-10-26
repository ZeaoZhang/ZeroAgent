"""Source-hash-bound, non-destructive migration of explicit legacy roots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zero_agent.context.admin import _require_key
from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.types import ContextObject, EvidenceReceipt, RuntimeContext
from zero_agent.skills.manifest import load_builtin_catalog
from zero_agent.utils.files import paths_overlap

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_MAX_FILES = 10_000
_MAX_FILE_BYTES = 50_000_000
_MAX_TOTAL_BYTES = 2_000_000_000


class ContextMigrationService:
    """Build plans without touching the target and apply only the same preimage."""

    def __init__(self, target_root: str | Path, *, account_id: str = "local") -> None:
        self.target_root = Path(target_root).expanduser().resolve()
        self.account_id = account_id

    def create_plan(
        self,
        source_root: str | Path,
        *,
        principal_id: str,
    ) -> dict[str, Any]:
        principal = _require_key(principal_id, "principal")
        root_arg = Path(source_root).expanduser()
        if root_arg.is_symlink() or not root_arg.is_dir():
            raise ContextError("INVALID_SOURCE", "Migration requires an explicit regular source directory.")
        root = root_arg.resolve(strict=True)
        builtins = {
            Path(skill.source_path).name: skill
            for skill in load_builtin_catalog()
        }
        items: list[dict[str, Any]] = []
        total_size = 0
        for current, directories, filenames in os.walk(root, followlinks=False):
            base = Path(current)
            if len(base.relative_to(root).parts) >= 16:
                for name in directories:
                    items.append(_skipped_item((base / name).relative_to(root).as_posix(), "depth_limit"))
                directories[:] = []
            kept_directories = []
            for name in sorted(directories):
                candidate = base / name
                if candidate.is_symlink():
                    items.append(_skipped_item(candidate.relative_to(root).as_posix(), "symlink_directory"))
                else:
                    kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(filenames):
                path = base / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.is_file():
                    items.append(_skipped_item(relative, "not_a_regular_file"))
                    continue
                try:
                    path.resolve(strict=True).relative_to(root)
                except (OSError, RuntimeError, ValueError):
                    items.append(_skipped_item(relative, "path_escape"))
                    continue
                if len(items) >= _MAX_FILES:
                    items.append(_skipped_item(relative, "file_count_limit"))
                    continue
                size = path.stat().st_size
                if size > _MAX_FILE_BYTES:
                    items.append(_skipped_item(relative, "file_size_limit"))
                    continue
                total_size += size
                if total_size > _MAX_TOTAL_BYTES:
                    items.append(_skipped_item(relative, "total_size_limit"))
                    continue
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                classification = _classify(relative, name, data, builtins)
                entry: dict[str, Any] = {
                    "source_relative_path": relative,
                    "source_sha256": digest,
                    "size_bytes": size,
                    **classification,
                }
                if entry["disposition"] == "import_legacy":
                    entry["target_uri"] = _legacy_uri(principal, relative, digest)
                items.append(entry)
        items.sort(key=lambda item: item["source_relative_path"])
        material = {
            "source_root": str(root),
            "target_root": str(self.target_root),
            "principal_id": principal,
            "items": items,
        }
        migration_id = hashlib.sha256(_canonical_json(material)).hexdigest()
        return {
            "schema_version": 1,
            "migration_id": migration_id,
            **material,
            "source_file_count": sum("source_sha256" in item for item in items),
            "importable_count": sum(item.get("disposition") == "import_legacy" for item in items),
            "sensitive_quarantine_count": sum(bool(item.get("sensitive_suspect")) for item in items),
            "manual_review_count": sum(item.get("disposition") in {"manual_review", "archive_pending"} for item in items),
        }

    def write_plan(self, plan: dict[str, Any], destination: str | Path) -> Path:
        target = Path(destination).expanduser()
        if target.exists() or target.is_symlink():
            raise ContextConflictError("The migration manifest destination already exists.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    def apply(self, manifest_path: str | Path, *, principal_id: str) -> dict[str, Any]:
        manifest = _read_manifest(manifest_path)
        principal = _require_key(principal_id, "principal")
        if manifest.get("principal_id") != principal:
            raise ContextError("PERMISSION_DENIED", "The migration manifest belongs to another principal.")
        current_plan = self.create_plan(manifest["source_root"], principal_id=principal)
        if current_plan != manifest:
            raise ContextConflictError("The migration source preimage changed after dry-run.")
        source_root = Path(current_plan["source_root"]).resolve(strict=True)
        if paths_overlap(source_root, self.target_root):
            raise ContextError("INVALID_SOURCE", "The migration target must be independent from its source root.")
        importable = [item for item in current_plan["items"] if item.get("disposition") == "import_legacy"]
        if not importable:
            return _apply_report(current_plan, 0, replayed=False)

        # Construct all content and deterministic receipts before initializing the target.
        operations = []
        receipts = []
        session_id = "migration_" + current_plan["migration_id"][:24]
        task_id = "migration_" + current_plan["migration_id"]
        runtime = RuntimeContext(self.account_id, principal, session_id)
        for index, item in enumerate(importable, start=1):
            path = source_root / item["source_relative_path"]
            try:
                path.resolve(strict=True).relative_to(source_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ContextConflictError("A migration source path escaped its selected root.") from exc
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != item["source_sha256"]:
                raise ContextConflictError("The migration source changed while it was being read.")
            content = data.decode("utf-8", errors="strict")
            receipt_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{current_plan['migration_id']}:{item['source_relative_path']}:{item['source_sha256']}",
            ))
            receipt = EvidenceReceipt(
                receipt_id=receipt_id,
                source_type="resource_import",
                account_id=runtime.account_id,
                user_id=runtime.user_id,
                session_id=runtime.session_id,
                task_id=task_id,
                event_sequence=index,
                source_id=f"sha256:{item['source_sha256']}",
                outcome_status="success",
                result_fields={"size_bytes": item["size_bytes"], "migration_id": current_plan["migration_id"]},
                excerpt="",
                source_revision=item["source_sha256"],
                source_content_sha256=item["source_sha256"],
            )
            receipts.append(receipt)
            status = "sensitive_quarantined" if item.get("sensitive_suspect") else "legacy_unverified"
            obj = ContextObject(
                object_id=item["target_uri"].rsplit("/", 1)[-1],
                uri=item["target_uri"],
                kind="resource",
                owner_id=principal,
                content=content,
                title=Path(item["source_relative_path"]).name,
                status=status,
                source_refs=(receipt_id,),
                tags=("legacy", "migration"),
            )
            operations.append((obj, None))

        store = LocalContextStore(self.target_root)
        for receipt in receipts:
            store.create_evidence_receipt(receipt)
        payload_hash = hashlib.sha256(_canonical_json(current_plan)).hexdigest()
        replay = store.lookup_idempotency(f"migration:{current_plan['migration_id']}", payload_hash)
        if replay is not None:
            return _apply_report(current_plan, len(replay), replayed=True)
        normalized_operations = []
        for obj, _ in operations:
            current = store.get(obj.uri)
            if current is None:
                expected_revision = None
            elif (
                current.owner_id == principal
                and current.status in {"legacy_unverified", "sensitive_quarantined"}
            ):
                expected_revision = current.revision
            else:
                raise ContextConflictError("The migration target URI already belongs to another object.")
            if current is not None and current.status == "sensitive_quarantined":
                obj = replace(obj, status="sensitive_quarantined")
            normalized_operations.append((obj, expected_revision))
        committed = store.commit_many(
            normalized_operations,
            idempotency_key=f"migration:{current_plan['migration_id']}",
            idempotency_payload_hash=payload_hash,
        )
        return _apply_report(current_plan, len(committed), replayed=False)

    def verify(self, manifest_path: str | Path, *, principal_id: str) -> dict[str, Any]:
        manifest = _read_manifest(manifest_path)
        principal = _require_key(principal_id, "principal")
        if manifest.get("principal_id") != principal:
            raise ContextError("PERMISSION_DENIED", "The migration manifest belongs to another principal.")
        current_plan = self.create_plan(manifest["source_root"], principal_id=principal)
        if current_plan != manifest:
            raise ContextConflictError("The migration source no longer matches the verified manifest.")
        manifest_file = self.target_root / "manifest.sqlite"
        if not manifest_file.is_file():
            raise ContextError("NOT_FOUND", "The migration target has not been initialized.")
        store = LocalContextStore(self.target_root)
        missing: list[str] = []
        verified = 0
        for item in current_plan["items"]:
            if item.get("disposition") != "import_legacy":
                continue
            obj = store.get(item["target_uri"])
            expected_status = "sensitive_quarantined" if item.get("sensitive_suspect") else "legacy_unverified"
            if obj is None or obj.content_sha256 != item["source_sha256"] or obj.status != expected_status:
                missing.append(item["source_relative_path"])
            else:
                verified += 1
        return {
            "migration_id": current_plan["migration_id"],
            "verified_count": verified,
            "expected_count": current_plan["importable_count"],
            "missing_or_changed": missing,
            "manual_review": [
                item["source_relative_path"] for item in current_plan["items"]
                if item.get("disposition") in {"manual_review", "archive_pending"}
            ],
            "complete": not missing and not current_plan["manual_review_count"],
        }


def _classify(relative: str, name: str, data: bytes, builtins: dict[str, Any]) -> dict[str, Any]:
    parts = Path(relative).parts
    if any(part.casefold() == "projects" for part in parts) and name == "project_memory.md":
        return {"disposition": "manual_review", "reason": "project_owner_mapping_required", "sensitive_suspect": False}
    if any(part.casefold() == "l4_raw_sessions" for part in parts) or Path(name).suffix.casefold() == ".zip":
        return {"disposition": "archive_pending", "reason": "session_archive_preserved_at_source", "sensitive_suspect": False}
    if "sops" in {part.casefold() for part in parts} and name.endswith(".md"):
        builtin = builtins.get(name)
        if builtin is not None and hashlib.sha256(data).hexdigest() == builtin.source_sha256:
            return {
                "disposition": "builtin_reference",
                "reason": "byte_identical_to_packaged_skill",
                "skill_uri": builtin.uri,
                "sensitive_suspect": False,
            }
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {"disposition": "manual_review", "reason": "non_utf8_source_preserved_at_source", "sensitive_suspect": False}
    sensitive = any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)
    return {
        "disposition": "import_legacy",
        "reason": "sensitive_value_quarantined" if sensitive else "legacy_unverified",
        "sensitive_suspect": sensitive,
        "kind": "resource",
    }


def _legacy_uri(principal_id: str, relative: str, digest: str) -> str:
    return legacy_uri_for_relative(principal_id, relative)


def legacy_uri_for_relative(principal_id: str, relative: str) -> str:
    identity = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
    owner = quote(principal_id, safe="-._~")
    return f"za://users/{owner}/memories/legacy/{identity}"


def _read_manifest(path: str | Path) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ContextError("INVALID_SOURCE", "A regular migration manifest file is required.")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContextError("INVALID_SOURCE", "The migration manifest is unreadable or malformed.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ContextError("INVALID_SOURCE", "The migration manifest version is unsupported.")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")




def _skipped_item(relative: str, reason: str) -> dict[str, Any]:
    return {
        "source_relative_path": relative,
        "disposition": "manual_review",
        "reason": reason,
        "sensitive_suspect": False,
    }


def _apply_report(plan: dict[str, Any], imported: int, *, replayed: bool) -> dict[str, Any]:
    return {
        "migration_id": plan["migration_id"],
        "status": "replayed" if replayed else "applied",
        "imported_count": imported,
        "sensitive_quarantine_count": plan["sensitive_quarantine_count"],
        "manual_review": [
            item["source_relative_path"] for item in plan["items"]
            if item.get("disposition") in {"manual_review", "archive_pending"}
        ],
        "source_modified": False,
    }
