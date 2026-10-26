"""SQLite manifest and immutable Markdown revisions for local context data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.types import (
    CollectionPrecondition,
    ContextObject,
    ContextAccessStat,
    DerivedSummary,
    EvidenceReceipt,
    PurgeResult,
    RuntimeContext,
    collection_fingerprint,
    context_access_scope_key,
)

SCHEMA_VERSION = 5
_SAFE_AUDIT_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LocalContextStore:
    """Local single-host ContextStore with immutable content and CAS pointers."""

    def __init__(self, root: str | Path) -> None:
        requested_root = Path(root).expanduser()
        if requested_root.is_symlink():
            raise ContextError("INTEGRITY_ERROR", "context_root cannot be a symlink")
        self.root = requested_root.resolve()
        self.manifest_path = self.root / "manifest.sqlite"
        self.objects_path = self.root / "objects"
        self.packages_path = self.root / "packages"
        self.derived_path = self.root / "derived"
        self.exports_path = self.root / "exports"
        self._initialize()

    def get(self, uri: str, revision: int | None = None) -> ContextObject | None:
        with self._connection() as connection:
            if revision is None:
                row = connection.execute(
                    """SELECT o.*, r.revision AS revision, r.content_sha256, r.relative_path,
                              r.metadata_json AS revision_metadata_json, r.committed_at
                       FROM objects o JOIN revisions r
                         ON r.uri=o.uri AND r.revision=o.current_revision
                       WHERE o.uri=?""",
                    (uri,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT o.*, r.revision AS revision, r.content_sha256, r.relative_path,
                              r.metadata_json AS revision_metadata_json, r.committed_at
                       FROM objects o JOIN revisions r ON r.uri=o.uri
                       WHERE o.uri=? AND r.revision=?""",
                    (uri, revision),
                ).fetchone()
        return None if row is None else self._object_from_row(row)

    def list_objects(
        self,
        *,
        uri_filter: Callable[[str], bool] | None = None,
    ) -> Sequence[ContextObject]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT o.*, r.revision AS revision, r.content_sha256, r.relative_path,
                          r.metadata_json AS revision_metadata_json, r.committed_at
                   FROM objects o JOIN revisions r
                     ON r.uri=o.uri AND r.revision=o.current_revision
                   ORDER BY o.uri"""
            ).fetchall()
        return tuple(
            self._object_from_row(row)
            for row in rows
            if (uri_filter is None or uri_filter(str(row["uri"])))
            and (uri_filter is None or str(row["status"]) != "sensitive_quarantined")
        )

    def record_access(
        self,
        runtime: RuntimeContext,
        objects: Sequence[ContextObject],
        *,
        operation: str,
    ) -> None:
        if operation not in {"read", "list", "search"}:
            raise ValueError("unsupported context access operation")
        if not objects:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            connection.executemany(
                """INSERT INTO context_access_stats (
                     account_id, user_id, scope_key, uri, revision, operation, count, last_access_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(account_id,user_id,scope_key,uri,revision,operation)
                   DO UPDATE SET count=count+1, last_access_at=excluded.last_access_at""",
                (
                    (
                        runtime.account_id,
                        runtime.user_id,
                        context_access_scope_key(obj, runtime),
                        obj.uri,
                        obj.revision,
                        operation,
                        now,
                    )
                    for obj in objects
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_access_stats(self, runtime: RuntimeContext) -> Sequence[ContextAccessStat]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT uri, revision, scope_key, operation, count, last_access_at
                   FROM context_access_stats WHERE account_id=? AND user_id=?
                   ORDER BY uri, revision, operation""",
                (runtime.account_id, runtime.user_id),
            ).fetchall()
        return tuple(
            ContextAccessStat(
                uri=str(row["uri"]), revision=int(row["revision"]),
                scope_key=str(row["scope_key"]), operation=str(row["operation"]),
                count=int(row["count"]), last_access_at=str(row["last_access_at"]),
            )
            for row in rows
        )

    def resolve_committed_revision_path(self, path: str | Path) -> tuple[str, int] | None:
        """Resolve an exact committed body path through manifest metadata only."""
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
            objects_root = self.objects_path.resolve(strict=True)
            relative = resolved.relative_to(objects_root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT uri, revision FROM revisions WHERE relative_path=?",
                (f"objects/{relative}",),
            ).fetchone()
        if row is None:
            return None
        # Verify bytes before exposing the logical reference to a caller.
        self.get(row["uri"], int(row["revision"]))
        return row["uri"], int(row["revision"])

    def create_evidence_receipt(self, receipt: EvidenceReceipt) -> EvidenceReceipt:
        """Persist an immutable minimal source receipt and its creation event."""

        _validate_receipt(receipt)
        payload_hash = _receipt_payload_hash(receipt)
        result_json = json.dumps(
            receipt.result_fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM evidence_receipts WHERE receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                stored = _evidence_receipt_from_row(connection, existing)
                if _receipt_payload_hash(stored) != payload_hash:
                    raise ContextConflictError("Receipt identity was reused with different content.")
                connection.rollback()
                return stored
            connection.execute(
                """INSERT INTO evidence_receipts (
                     receipt_id, account_id, user_id, session_id, task_id, project_id, event_sequence,
                     source_type, source_id, tool_name, tool_contract_version,
                     outcome_status, result_json, excerpt, source_revision,
                     source_content_sha256, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.receipt_id,
                    receipt.account_id,
                    receipt.user_id,
                    receipt.session_id,
                    receipt.task_id,
                    receipt.project_id,
                    receipt.event_sequence,
                    receipt.source_type,
                    receipt.source_id,
                    receipt.tool_name,
                    receipt.tool_contract_version,
                    receipt.outcome_status,
                    result_json,
                    receipt.excerpt,
                    receipt.source_revision,
                    receipt.source_content_sha256,
                    receipt.created_at,
                ),
            )
            connection.execute(
                """INSERT INTO receipt_events (
                     event_id, receipt_id, event_sequence, principal_id,
                     event_type, reason_code, created_at
                   ) VALUES (?, ?, 1, ?, 'recorded', '', ?)""",
                (str(uuid.uuid4()), receipt.receipt_id, receipt.user_id, _utc_now()),
            )
            connection.commit()
            return receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_evidence_receipt(self, receipt_id: str) -> EvidenceReceipt | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                return None
            return _evidence_receipt_from_row(connection, row)

    def next_evidence_event_sequence(self, user_id: str, task_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT MAX(event_sequence) FROM evidence_receipts WHERE user_id=? AND task_id=?",
                (user_id, task_id),
            ).fetchone()
        return (int(row[0]) + 1) if row is not None and row[0] is not None else 1
    def list_evidence_receipts(
        self,
        *,
        account_id: str,
        user_id: str,
        session_id: str,
        task_id: str,
        source_type: str | None = None,
    ) -> Sequence[EvidenceReceipt]:
        query = (
            "SELECT * FROM evidence_receipts WHERE account_id=? AND user_id=? "
            "AND session_id=? AND task_id=?"
        )
        values: tuple[object, ...] = (account_id, user_id, session_id, task_id)
        if source_type is not None:
            query += " AND source_type=?"
            values += (source_type,)
        query += " ORDER BY event_sequence, receipt_id"
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
            return tuple(_evidence_receipt_from_row(connection, row) for row in rows)


    def revoke_evidence_receipt(
        self,
        receipt_id: str,
        *,
        principal_id: str,
        reason_code: str,
    ) -> None:
        if not principal_id or not _SAFE_AUDIT_CODE.fullmatch(reason_code):
            raise ValueError("receipt revocation requires a principal and safe reason code")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM evidence_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise ContextError("NOT_FOUND", "The evidence receipt was not found.")
            if row["user_id"] != principal_id:
                raise ContextError("PERMISSION_DENIED", "The evidence receipt belongs to another principal.")
            latest = connection.execute(
                """SELECT event_sequence, event_type FROM receipt_events
                   WHERE receipt_id=? ORDER BY event_sequence DESC LIMIT 1""",
                (receipt_id,),
            ).fetchone()
            if latest is not None and latest["event_type"] in {"revoked", "redacted"}:
                raise ContextConflictError("The evidence receipt is already revoked.")
            event_sequence = 1 if latest is None else int(latest["event_sequence"]) + 1
            connection.execute(
                """INSERT INTO receipt_events (
                     event_id, receipt_id, event_sequence, principal_id,
                     event_type, reason_code, created_at
                   ) VALUES (?, ?, ?, ?, 'revoked', ?, ?)""",
                (
                    str(uuid.uuid4()),
                    receipt_id,
                    event_sequence,
                    principal_id,
                    reason_code,
                    _utc_now(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def purge_object(
        self,
        uri: str,
        *,
        expected_revision: int | None,
        principal_id: str,
        idempotency_key: str,
        idempotency_payload_hash: str,
        additional_source_refs: Sequence[str] = (),
    ) -> PurgeResult:
        """Delete manifest state, then finish physical cleanup under a write lock."""
        if (
            not uri.startswith("za://") or not principal_id or not idempotency_key
            or not re.fullmatch(r"[0-9a-f]{64}", idempotency_payload_hash)
        ):
            raise ContextError("INVALID_SOURCE", "Purge request identity is incomplete.")
        uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        retained_sessions: set[str] = set()
        retained_backups: tuple[str, ...] = ()
        retained_exports: tuple[str, ...] = ()
        source_complete = True
        source_reason = ""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM purge_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["payload_sha256"] != idempotency_payload_hash or prior["uri_sha256"] != uri_hash:
                    raise ContextConflictError("The purge idempotency key has another payload.")
                retained_sessions.update(json.loads(prior["residual_sessions_json"]))
                retained_backups = tuple(json.loads(prior["residual_backups_json"]))
                retained_exports = tuple(json.loads(prior["residual_exports_json"]))
                source_complete = bool(prior["source_cleanup_complete"])
                source_reason = str(prior["source_cleanup_reason"] or "")
                purge_id = str(prior["purge_id"])
                if bool(prior["cleanup_complete"]):
                    connection.rollback()
                    return PurgeResult(
                        True,
                        tuple(sorted(retained_sessions)),
                        retained_backups,
                        retained_exports,
                        source_complete,
                        source_reason,
                    )
            else:
                current = connection.execute("SELECT * FROM objects WHERE uri=?", (uri,)).fetchone()
                if current is None:
                    raise ContextError("NOT_FOUND", "The memory object was not found.")
                if expected_revision is None or expected_revision < 1:
                    raise ContextError("INVALID_SOURCE", "Initial purge requires an exact expected revision.")
                if int(current["current_revision"]) != expected_revision:
                    raise ContextConflictError("The memory object's expected revision is stale.")
                if uri.startswith("za://users/") and current["owner_id"] != principal_id:
                    raise ContextError("PERMISSION_DENIED", "The memory object belongs to another user.")
                retained_backups = tuple(self._find_backups_containing_uri(uri, connection=connection))
                retained_exports = tuple(self._find_exports_containing_uri(uri, connection=connection))
                refs: set[str] = set(additional_source_refs)
                refs.update(json.loads(current["source_refs_json"]))
                history = connection.execute("SELECT metadata_json FROM revisions WHERE uri=?", (uri,)).fetchall()
                for row in history:
                    raw = str(row["metadata_json"] or "{}")
                    try:
                        metadata = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        metadata = {}
                    if not metadata:
                        source_complete = False
                        source_reason = "legacy_history_metadata_unavailable"
                    elif isinstance(metadata.get("source_refs"), list):
                        refs.update(str(item) for item in metadata["source_refs"] if isinstance(item, str))
                if refs:
                    marks = ",".join("?" for _ in refs)
                    rows = connection.execute(f"SELECT source_type,session_id FROM evidence_receipts WHERE receipt_id IN ({marks})", tuple(refs)).fetchall()
                    retained_sessions.update(row["session_id"] for row in rows if row["source_type"] == "user_message")
                surviving_refs: set[str] = set()
                for row in connection.execute("SELECT source_refs_json FROM objects WHERE uri<>?", (uri,)):
                    try:
                        surviving_refs.update(json.loads(row[0]))
                    except (TypeError, json.JSONDecodeError):
                        pass
                for row in connection.execute("SELECT metadata_json FROM revisions WHERE uri<>?", (uri,)):
                    try:
                        metadata = json.loads(row[0] or "{}")
                        if isinstance(metadata.get("source_refs"), list):
                            surviving_refs.update(metadata["source_refs"])
                    except (TypeError, json.JSONDecodeError):
                        pass
                if any(ref in surviving_refs for ref in refs):
                    source_complete = False
                    if source_reason != "legacy_history_metadata_unavailable":
                        source_reason = "source_still_referenced"
                connection.execute("DELETE FROM revisions WHERE uri=?", (uri,))
                connection.execute("DELETE FROM objects WHERE uri=?", (uri,))
                connection.execute("DELETE FROM changes WHERE uri=?", (uri,))
                connection.execute("DELETE FROM context_access_stats WHERE uri=?", (uri,))
                for row in connection.execute("SELECT idempotency_key,result_json FROM idempotency").fetchall():
                    try:
                        items = json.loads(row["result_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if any(isinstance(item, list) and len(item) == 2 and item[0] == uri for item in items):
                        connection.execute("DELETE FROM idempotency WHERE idempotency_key=?", (row["idempotency_key"],))
                for receipt_id in refs - surviving_refs:
                    receipt = connection.execute("SELECT receipt_id FROM evidence_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
                    if receipt is None:
                        continue
                    latest = connection.execute("SELECT MAX(event_sequence) FROM receipt_events WHERE receipt_id=?", (receipt_id,)).fetchone()[0]
                    connection.execute("UPDATE evidence_receipts SET excerpt='',result_json='{}',source_content_sha256=? WHERE receipt_id=?", (hashlib.sha256(b"").hexdigest(), receipt_id))
                    connection.execute("INSERT INTO receipt_events (event_id,receipt_id,event_sequence,principal_id,event_type,reason_code,created_at) VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), receipt_id, (int(latest)+1) if latest is not None else 1, principal_id, "redacted", "source_purged", _utc_now()))
                purge_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO purge_events (
                         purge_id,uri_sha256,principal_id,expected_revision,idempotency_key,
                         payload_sha256,created_at,cleanup_complete,residual_sessions_json,
                         residual_backups_json,residual_exports_json,source_cleanup_complete,
                         source_cleanup_reason
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        purge_id, uri_hash, principal_id, expected_revision, idempotency_key,
                        idempotency_payload_hash, _utc_now(), 0,
                        json.dumps(sorted(retained_sessions), separators=(",", ":")),
                        json.dumps(retained_backups, separators=(",", ":")),
                        json.dumps(retained_exports, separators=(",", ":")),
                        int(source_complete), source_reason,
                    ),
                )
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        cleanup_complete = self._finish_purge_cleanup(uri, uri_hash, purge_id, retained_sessions, retained_backups, retained_exports)
        return PurgeResult(
            cleanup_complete, tuple(sorted(retained_sessions)), retained_backups,
            retained_exports, source_complete, source_reason,
        )

    def collect_orphans(self, *, grace_seconds: int = 3600, dry_run: bool = True) -> dict[str, int | bool]:
        """Count/remove unreferenced aged revision files without following symlinks."""
        import stat
        import time

        if grace_seconds < 0:
            raise ContextError("INVALID_SOURCE", "grace_seconds must not be negative")
        cutoff = time.time() - grace_seconds
        connection = self._connect()
        candidate = removed = deferred = unsafe = failed = reclaimed = 0
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise ContextError("BACKEND_UNAVAILABLE", "The context manifest is busy.") from exc
            referenced = {
                str(row["relative_path"])
                for row in connection.execute("SELECT relative_path FROM revisions")
            }
            root = self.objects_path
            try:
                root_mode = root.lstat().st_mode
            except FileNotFoundError:
                root_mode = 0
            except OSError:
                root_mode = 0
                failed += 1
            if root_mode and (stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode)):
                unsafe += 1
            elif root_mode:
                try:
                    prefixes = tuple(root.iterdir())
                except OSError:
                    prefixes = ()
                    failed += 1
                for prefix in prefixes:
                    try:
                        prefix_mode = prefix.lstat().st_mode
                    except OSError:
                        failed += 1
                        continue
                    if (
                        stat.S_ISLNK(prefix_mode) or not stat.S_ISDIR(prefix_mode)
                        or not re.fullmatch(r"[0-9a-f]{2}", prefix.name)
                    ):
                        unsafe += 1
                        continue
                    try:
                        object_dirs = tuple(prefix.iterdir())
                    except OSError:
                        failed += 1
                        continue
                    for object_dir in object_dirs:
                        try:
                            object_mode = object_dir.lstat().st_mode
                        except OSError:
                            failed += 1
                            continue
                        if (
                            stat.S_ISLNK(object_mode) or not stat.S_ISDIR(object_mode)
                            or not re.fullmatch(r"[0-9a-f]{64}", object_dir.name)
                            or object_dir.name[:2] != prefix.name
                        ):
                            unsafe += 1
                            continue
                        revisions = object_dir / "revisions"
                        try:
                            revisions_mode = revisions.lstat().st_mode
                        except FileNotFoundError:
                            continue
                        except OSError:
                            failed += 1
                            continue
                        if stat.S_ISLNK(revisions_mode) or not stat.S_ISDIR(revisions_mode):
                            unsafe += 1
                            continue
                        try:
                            files = tuple(revisions.iterdir())
                        except OSError:
                            failed += 1
                            continue
                        removed_from_directory = False
                        for item in files:
                            try:
                                metadata = item.lstat()
                            except OSError:
                                failed += 1
                                continue
                            if not stat.S_ISREG(metadata.st_mode):
                                unsafe += 1
                                continue
                            if not (
                                item.name.startswith(".revision-")
                                or re.fullmatch(r"[0-9]+-[0-9a-f]{64}\.md", item.name)
                            ):
                                unsafe += 1
                                continue
                            relative = item.relative_to(self.root).as_posix()
                            if metadata.st_mtime >= cutoff or relative in referenced:
                                deferred += 1
                                continue
                            candidate += 1
                            if dry_run:
                                continue
                            try:
                                item.unlink()
                                removed += 1
                                reclaimed += metadata.st_size
                                removed_from_directory = True
                            except OSError:
                                failed += 1
                        if not dry_run:
                            if removed_from_directory:
                                try:
                                    _fsync_dir(revisions)
                                except OSError:
                                    failed += 1
                            try:
                                revisions.rmdir()
                                object_dir.rmdir()
                                _fsync_dir(prefix)
                            except OSError:
                                pass
                    if not dry_run:
                        try:
                            prefix.rmdir()
                        except OSError:
                            pass
                if not dry_run:
                    try:
                        _fsync_dir(root)
                    except OSError:
                        failed += 1
            connection.commit()
        except ContextError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "candidate_files": candidate,
            "removed_files": removed,
            "deferred_files": deferred,
            "unsafe_entries": unsafe,
            "failed_files": failed,
            "reclaimed_bytes": reclaimed,
            "dry_run": dry_run,
        }

    def lookup_purge_status(
        self,
        uri: str,
        *,
        idempotency_key: str,
        idempotency_payload_hash: str,
        principal_id: str | None = None,
    ) -> PurgeResult | None:
        uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM purge_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["uri_sha256"] != uri_hash or row["payload_sha256"] != idempotency_payload_hash:
            raise ContextConflictError("The purge idempotency key has another payload.")
        if principal_id is not None and row["principal_id"] != principal_id:
            raise ContextError("PERMISSION_DENIED", "The purge request belongs to another principal.")
        return PurgeResult(
            bool(row["cleanup_complete"]),
            tuple(json.loads(row["residual_sessions_json"])),
            tuple(json.loads(row["residual_backups_json"])),
            tuple(json.loads(row["residual_exports_json"])),
            bool(row["source_cleanup_complete"]),
            str(row["source_cleanup_reason"] or ""),
        )

    def _find_backups_containing_uri(self, uri: str, *, connection: sqlite3.Connection | None = None) -> list[str]:
        owns_connection = connection is None
        if owns_connection:
            connection = self._connect()
        try:
            rows = connection.execute("SELECT backup_id, path, state, staging_path FROM backup_catalog ORDER BY created_at").fetchall()
            retained: list[str] = []
            for row in rows:
                identifier = f"backup:{row['backup_id']}"
                if row["state"] == "pending":
                    retained.append(identifier)
                    continue
                path = Path(row["path"])
                if not path.is_dir() or path.is_symlink():
                    retained.append(identifier)
                    continue
                manifest = path / "manifest.sqlite"
                try:
                    reader = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
                    try:
                        exists = reader.execute("SELECT 1 FROM objects WHERE uri=? LIMIT 1", (uri,)).fetchone()
                    finally:
                        reader.close()
                except sqlite3.DatabaseError:
                    retained.append(identifier)
                    continue
                if exists is not None:
                    retained.append(identifier)
            return retained
        finally:
            if owns_connection:
                connection.close()

    def _find_exports_containing_uri(self, uri: str, *, connection: sqlite3.Connection | None = None) -> list[str]:
        owns_connection = connection is None
        if owns_connection:
            connection = self._connect()
        try:
            rows = connection.execute("SELECT export_id, path, state, staging_path FROM export_catalog ORDER BY created_at").fetchall()
            retained: list[str] = []
            for row in rows:
                identifier = f"export:{row['export_id']}"
                if row["state"] == "pending":
                    retained.append(identifier)
                    continue
                path = Path(row["path"])
                if not path.is_file() or path.is_symlink():
                    retained.append(identifier)
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    retained.append(identifier)
                    continue
                if any(item.get("uri") == uri for item in payload.get("objects", []) if isinstance(item, dict)):
                    retained.append(identifier)
            return retained
        finally:
            if owns_connection:
                connection.close()


    def _finish_purge_cleanup(
        self,
        uri: str,
        uri_hash: str,
        purge_id: str,
        retained_sessions: set[str],
        retained_backups: tuple[str, ...],
        retained_exports: tuple[str, ...],
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT uri_sha256 FROM purge_events WHERE purge_id=?", (purge_id,)).fetchone()
            if row is None or row["uri_sha256"] != uri_hash:
                connection.rollback(); return False
            # The manifest must remain absent before physical cleanup.
            if connection.execute("SELECT 1 FROM objects WHERE uri=?", (uri,)).fetchone() is not None:
                connection.rollback(); return False
            for directory in (self.objects_path / uri_hash[:2] / uri_hash, self.derived_path / uri_hash):
                if directory.is_symlink():
                    raise OSError("unsafe purge directory")
                if directory.exists():
                    _unlink_tree_no_symlinks(directory)
            try: (self.objects_path / uri_hash[:2]).rmdir()
            except OSError: pass
            _fsync_dir(self.objects_path); _fsync_dir(self.derived_path)
            connection.execute("UPDATE purge_events SET cleanup_complete=1,residual_sessions_json=?,residual_backups_json=?,residual_exports_json=? WHERE purge_id=?", (json.dumps(sorted(retained_sessions), separators=(",", ":")), json.dumps(retained_backups, separators=(",", ":")), json.dumps(retained_exports, separators=(",", ":")), purge_id))
            connection.commit(); return True
        except Exception:
            connection.rollback(); return False
        finally:
            connection.close()

    def commit(
        self,
        obj: ContextObject,
        *,
        expected_revision: int | None,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
    ) -> ContextObject:
        return self.commit_many(
            ((obj, expected_revision),),
            idempotency_key=idempotency_key,
            idempotency_payload_hash=idempotency_payload_hash,
        )[0]

    def commit_many(
        self,
        operations: Iterable[tuple[ContextObject, int | None]],
        *,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
        preconditions: Sequence[CollectionPrecondition] = (),
    ) -> tuple[ContextObject, ...]:
        """Publish a set of objects together, or leave every pointer unchanged."""

        entries = tuple(operations)
        if not entries:
            raise ValueError("commit_many requires at least one operation")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty when provided")
        if len({obj.uri for obj, _ in entries}) != len(entries):
            raise ValueError("commit_many cannot contain duplicate URIs")
        if len({item.scope for item in preconditions}) != len(preconditions):
            raise ValueError("commit_many cannot repeat collection scopes")
        for obj, expected in entries:
            self._validate_object(obj)
            if expected is not None and expected < 1:
                raise ValueError("expected_revision must be positive or None")

        payload_hash = idempotency_payload_hash or _payload_hash(entries)
        if not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
            raise ValueError("idempotency_payload_hash must be a SHA-256 digest")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_result(
                idempotency_key,
                payload_hash,
                connection=connection,
            )
            if replay is not None:
                connection.rollback()
                return replay
            self._assert_expected_revisions(connection, entries)
            self._assert_collection_preconditions(connection, preconditions)
            for purge in connection.execute(
                "SELECT uri_sha256 FROM purge_events WHERE cleanup_complete=0"
            ):
                pending_uri_hash = str(purge["uri_sha256"])
                if any(hashlib.sha256(obj.uri.encode("utf-8")).hexdigest() == pending_uri_hash for obj, _ in entries):
                    raise ContextConflictError("Object cleanup is pending; the URI cannot be recreated yet.")
            now = _utc_now()
            committed: list[ContextObject] = []
            for obj, expected in entries:
                revision = 1 if expected is None else expected + 1
                content_hash = obj.content_sha256
                relative_path = _revision_relative_path(obj.uri, revision, content_hash)
                self._publish_revision(relative_path, obj.content)
                previous = self._select_current(connection, obj.uri)
                created_at = previous["created_at"] if previous is not None else obj.created_at
                committed_obj = replace(
                    obj,
                    revision=revision,
                    created_at=created_at,
                    updated_at=now,
                )
                connection.execute(
                    """INSERT INTO objects (
                         uri, object_id, kind, owner_id, project_id, current_revision,
                         title, tags_json, status, source_refs_json, schema_version,
                         preference_key, scope_key, value_json, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(uri) DO UPDATE SET
                         object_id=excluded.object_id,
                         kind=excluded.kind,
                         owner_id=excluded.owner_id,
                         project_id=excluded.project_id,
                         current_revision=excluded.current_revision,
                         title=excluded.title,
                         tags_json=excluded.tags_json,
                         status=excluded.status,
                         source_refs_json=excluded.source_refs_json,
                         schema_version=excluded.schema_version,
                         preference_key=excluded.preference_key,
                         scope_key=excluded.scope_key,
                         value_json=excluded.value_json,
                         updated_at=excluded.updated_at""",
                    (
                        committed_obj.uri,
                        committed_obj.object_id,
                        committed_obj.kind,
                        committed_obj.owner_id,
                        committed_obj.project_id,
                        revision,
                        committed_obj.title,
                        json.dumps(committed_obj.tags, ensure_ascii=False),
                        committed_obj.status,
                        json.dumps(committed_obj.source_refs, ensure_ascii=False),
                        committed_obj.schema_version,
                        committed_obj.preference_key,
                        committed_obj.scope_key,
                        json.dumps(committed_obj.value, ensure_ascii=False),
                        created_at,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO revisions (
                         uri, revision, content_sha256, relative_path, committed_at, metadata_json
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (obj.uri, revision, content_hash, relative_path, now, _metadata_json(committed_obj)),
                )
                connection.execute(
                    "UPDATE derived_summaries SET stale=1 WHERE uri=? AND input_revision<>?",
                    (obj.uri, revision),
                )
                connection.execute(
                    """INSERT INTO changes (
                         change_id, uri, before_revision, after_revision, committed_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), obj.uri, expected, revision, now),
                )
                committed.append(committed_obj)
            if idempotency_key is not None:
                connection.execute(
                    "INSERT INTO idempotency (idempotency_key, payload_sha256, result_json) VALUES (?, ?, ?)",
                    (
                        idempotency_key,
                        payload_hash,
                        json.dumps(
                            [(obj.uri, obj.revision) for obj in committed],
                            separators=(",", ":"),
                        ),
                    ),
                )
            self._commit_transaction(connection)
            _fsync_dir(self.root)
            return tuple(committed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def lookup_idempotency(
        self,
        idempotency_key: str,
        payload_sha256: str,
    ) -> tuple[ContextObject, ...] | None:
        if not idempotency_key or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
            raise ValueError("idempotency lookup requires a key and SHA-256 digest")
        return self._idempotent_result(idempotency_key, payload_sha256)

    def save_derived_summary(
        self,
        uri: str,
        *,
        view: str,
        input_revision: int,
        generator_version: str,
        content: str,
    ) -> DerivedSummary:
        if view not in {"abstract", "overview"} or not generator_version or not content.strip():
            raise ContextError("INVALID_SOURCE", "The derived summary contract is invalid.")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT current_revision FROM objects WHERE uri=?",
                (uri,),
            ).fetchone()
            if current is None or int(current["current_revision"]) != input_revision:
                raise ContextConflictError("The source revision changed before summary publication.")
            existing = connection.execute(
                """SELECT content_sha256, content FROM derived_summaries
                   WHERE uri=? AND view=? AND input_revision=? AND generator_version=?""",
                (uri, view, input_revision, generator_version),
            ).fetchone()
            if existing is not None:
                if existing["content_sha256"] != content_hash:
                    raise ContextConflictError("This summary revision already has different content.")
                connection.rollback()
                return self.get_derived_summary(
                    uri,
                    view=view,
                    input_revision=input_revision,
                    generator_version=generator_version,
                )
            created_at = _utc_now()
            connection.execute(
                """INSERT INTO derived_summaries (
                     uri, view, input_revision, generator_version, content,
                     content_sha256, stale, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (uri, view, input_revision, generator_version, content, content_hash, created_at),
            )
            connection.commit()
            return DerivedSummary(
                uri, view, input_revision, generator_version, content,
                content_hash, False, created_at,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_derived_summary(
        self,
        uri: str,
        *,
        view: str,
        input_revision: int | None = None,
        generator_version: str | None = None,
    ) -> DerivedSummary | None:
        with self._connection() as connection:
            if input_revision is None:
                row = connection.execute(
                    """SELECT d.* FROM derived_summaries d JOIN objects o ON o.uri=d.uri
                       WHERE d.uri=? AND d.view=? AND d.input_revision=o.current_revision
                         AND d.stale=0
                       ORDER BY d.created_at DESC LIMIT 1""",
                    (uri, view),
                ).fetchone()
            elif generator_version is None:
                row = connection.execute(
                    """SELECT * FROM derived_summaries WHERE uri=? AND view=? AND input_revision=?
                       ORDER BY created_at DESC LIMIT 1""",
                    (uri, view, input_revision),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM derived_summaries
                       WHERE uri=? AND view=? AND input_revision=? AND generator_version=?""",
                    (uri, view, input_revision, generator_version),
                ).fetchone()
        if row is None:
            return None
        actual_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        if actual_hash != row["content_sha256"]:
            raise ContextError("INTEGRITY_ERROR", "The derived summary hash does not match.")
        return DerivedSummary(
            uri=row["uri"],
            view=row["view"],
            input_revision=int(row["input_revision"]),
            generator_version=row["generator_version"],
            content=row["content"],
            content_sha256=row["content_sha256"],
            stale=bool(row["stale"]),
            created_at=row["created_at"],
        )

    def _publish_cataloged_copy(
        self,
        *,
        kind: str,
        destination: Path,
        build: Callable[[Path, sqlite3.Connection], None],
    ) -> Path:
        if kind not in {"backup", "export"}:
            raise ValueError("unsupported catalog copy kind")
        if destination.is_symlink():
            raise ValueError(f"{kind} destination cannot be a symlink")
        target = destination.expanduser().resolve()
        if kind == "backup":
            if _is_within(target, self.root):
                raise ValueError("backup destination must be outside the context root")
            if target.exists():
                raise FileExistsError(target)
        elif _is_within(target, self.root) and not _is_within(target, self.exports_path):
            raise ValueError("exports inside context_root must be placed under exports/")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.staging"
        catalog = "backup_catalog" if kind == "backup" else "export_catalog"
        identity_column = "backup_id" if kind == "backup" else "export_id"
        published = False
        connection = self._connect()
        catalog_id: str | None = None
        prior = None
        artifact = staging
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                f"SELECT {identity_column}, state FROM {catalog} WHERE path=?", (str(target),)
            ).fetchone()
            if kind == "export" and prior is None and target.exists():
                raise FileExistsError(target)
            if kind == "backup" and prior is not None:
                raise FileExistsError(target)
            if kind == "export" and prior is not None and prior["state"] == "pending":
                pending = connection.execute(
                    "SELECT staging_path FROM export_catalog WHERE export_id=?",
                    (prior[identity_column],),
                ).fetchone()
                prior = _recover_pending_export(
                    self, connection, target, str(prior[identity_column]),
                    None if pending is None else pending["staging_path"],
                )
                connection.commit()
            catalog_id = str(prior[identity_column]) if prior is not None else str(uuid.uuid4())
            if prior is None:
                connection.execute(
                    f"INSERT INTO {catalog} ({identity_column},path,created_at,state,staging_path) VALUES (?,?,?,?,?)",
                    (catalog_id, str(target), _utc_now(), "pending", str(staging)),
                )
            else:
                connection.execute(
                    f"UPDATE {catalog} SET state='pending',staging_path=?,created_at=? WHERE {identity_column}=?",
                    (str(staging), _utc_now(), catalog_id),
                )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            if kind == "backup":
                staging.mkdir(mode=0o700)
                artifact = staging
            else:
                descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                artifact = staging
            build(artifact, connection)
            if kind == "backup":
                _verify_backup(artifact)
            else:
                _secure_file(artifact)
            os.replace(artifact, target)
            published = True
            _fsync_dir(target.parent)
            connection.execute(
                f"UPDATE {catalog} SET state='committed',staging_path=NULL WHERE {identity_column}=?",
                (catalog_id,),
            )
            connection.commit()
            return target
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            if published and kind == "export":
                # Keep the new target and pending catalog for deterministic recovery.
                raise
            cleanup_ok = True
            try:
                if staging.exists():
                    if staging.is_dir():
                        shutil.rmtree(staging)
                    else:
                        staging.unlink()
                if published and prior is None and target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                cleanup_ok = not staging.exists() and not (published and target.exists())
            except OSError:
                cleanup_ok = False
            if cleanup_ok:
                cleanup = self._connect()
                try:
                    cleanup.execute("BEGIN IMMEDIATE")
                    if prior is not None:
                        cleanup.execute(
                            f"UPDATE {catalog} SET state='committed',staging_path=NULL WHERE {identity_column}=?",
                            (catalog_id,),
                        )
                    else:
                        cleanup.execute(
                            f"DELETE FROM {catalog} WHERE {identity_column}=? AND state='pending'",
                            (catalog_id,),
                        )
                    cleanup.commit()
                except Exception:
                    cleanup.rollback()
                finally:
                    cleanup.close()
            raise
        finally:
            connection.close()

    def backup_to(self, destination: str | Path) -> Path:
        """Create a self-contained, point-in-time backup directory."""
        target = Path(destination).expanduser()

        def build(staging: Path, connection: sqlite3.Connection) -> None:
            if connection.execute("SELECT 1 FROM purge_events WHERE cleanup_complete=0 LIMIT 1").fetchone() is not None:
                raise ContextConflictError("Object cleanup is pending; complete purge before creating a backup.")
            backup_db = staging / "manifest.sqlite"
            source = sqlite3.connect(f"file:{self.manifest_path}?mode=ro", uri=True)
            destination_db = sqlite3.connect(backup_db)
            try:
                source.backup(destination_db)
            finally:
                destination_db.close()
                source.close()
            _secure_file(backup_db)
            for name in ("objects", "packages", "derived", "exports"):
                source_dir = self.root / name
                if source_dir.is_dir():
                    shutil.copytree(source_dir, staging / name, symlinks=True)
            _secure_tree(staging)

        return self._publish_cataloged_copy(kind="backup", destination=target, build=build)

    @classmethod
    def restore_backup(cls, source: str | Path, destination: str | Path) -> "LocalContextStore":
        """Restore a verified backup into a new directory."""
        requested_backup = Path(source).expanduser()
        requested_target = Path(destination).expanduser()
        if requested_backup.is_symlink() or requested_target.is_symlink():
            raise ValueError("backup paths cannot be symlinks")
        backup = requested_backup.resolve()
        target = requested_target.resolve()
        if _is_within(target, backup) or _is_within(backup, target):
            raise ValueError("backup source and restore destination must be separate")
        _verify_backup(backup)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(backup, target, symlinks=True)
            _secure_tree(target)
            _verify_backup(target)
            return cls(target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def export_json(
        self, destination: str | Path, *, uris: set[str] | None = None,
        expected_revisions: dict[str, int] | None = None, runtime: RuntimeContext | None = None,
    ) -> Path:
        """Export selected current revisions as portable UTF-8 JSON."""
        target = Path(destination).expanduser()
        selected = set(uris) if uris is not None else None
        if runtime is not None and expected_revisions is None:
            raise ContextError("REVISION_CONFLICT", "Expected revisions are required for authorized export.")
        if expected_revisions is not None and selected is not None and set(expected_revisions) != selected:
            raise ContextError("REVISION_CONFLICT", "Export URI and revision scopes differ.")

        def build(staging: Path, connection: sqlite3.Connection) -> None:
            from zero_agent.context.admin import ContextAdminService
            rows = connection.execute(
                """SELECT o.*, r.revision AS revision, r.content_sha256, r.relative_path,
                          r.metadata_json AS revision_metadata_json, r.committed_at
                   FROM objects o JOIN revisions r ON r.uri=o.uri AND r.revision=o.current_revision
                   ORDER BY o.uri"""
            ).fetchall()
            by_uri = {str(row["uri"]): row for row in rows}
            chosen = set(by_uri) if selected is None else selected
            if expected_revisions is not None and set(expected_revisions) != chosen:
                raise ContextError("REVISION_CONFLICT", "The export selection changed.")
            admin = ContextAdminService(self)
            objects: list[dict[str, object]] = []
            for uri in sorted(chosen):
                row = by_uri.get(uri)
                if row is None or (expected_revisions is not None and int(row["revision"]) != expected_revisions[uri]):
                    raise ContextConflictError("The export object's revision is stale.")
                if connection.execute("SELECT 1 FROM purge_events WHERE uri_sha256=? AND cleanup_complete=0 LIMIT 1", (hashlib.sha256(uri.encode("utf-8")).hexdigest(),)).fetchone() is not None:
                    raise ContextConflictError("Object cleanup is pending; complete purge before exporting.")
                if row["status"] == "sensitive_quarantined":
                    raise ContextError("PERMISSION_DENIED", "Sensitive quarantined objects cannot be exported.")
                if runtime is not None:
                    if uri.startswith("za://users/") and row["owner_id"] != runtime.user_id:
                        raise ContextError("PERMISSION_DENIED", "The current principal cannot export this object.")
                    if uri.startswith("za://projects/"):
                        project_id = str(row["project_id"] or "")
                        access = admin.project_access(project_id, runtime.user_id, connection=connection)
                        if access is None or runtime.project_id != project_id and f"project:{project_id}:read" not in runtime.capabilities:
                            raise ContextError("PERMISSION_DENIED", "The current principal has no project read grant.")
                    elif uri.startswith("za://sessions/"):
                        parts = uri.split("/")
                        session_id = parts[3] if len(parts) > 3 else ""
                        if session_id != runtime.session_id and f"session:{session_id}:read" not in runtime.capabilities:
                            raise ContextError("PERMISSION_DENIED", "The current principal cannot export this session.")
                    elif uri.startswith("za://skills/users/") and row["owner_id"] != runtime.user_id:
                        raise ContextError("PERMISSION_DENIED", "The current principal cannot export this skill object.")
                    elif not (uri.startswith("za://users/") or uri.startswith("za://projects/") or uri.startswith("za://sessions/") or uri.startswith("za://skills/")):
                        raise ContextError("PERMISSION_DENIED", "The current runtime cannot export this object.")
                obj = self._object_from_row(row)
                objects.append({"object_id": obj.object_id, "uri": obj.uri, "kind": obj.kind, "owner_id": obj.owner_id, "project_id": obj.project_id, "revision": obj.revision, "content_sha256": obj.content_sha256, "title": obj.title, "tags": list(obj.tags), "status": obj.status, "source_refs": list(obj.source_refs), "schema_version": obj.schema_version, "content": obj.content, "preference_key": obj.preference_key, "scope_key": obj.scope_key, "value": obj.value})
            payload = {"format": "zero-agent-context-export/v1", "objects": objects}
            fd = os.open(staging, os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staging, 0o600)

        return self._publish_cataloged_copy(kind="export", destination=target, build=build)

    def _initialize(self) -> None:
        root_was_present = self.root.exists()
        if self.manifest_path.is_symlink():
            raise ContextError("INTEGRITY_ERROR", "manifest.sqlite cannot be a symlink")
        if self.manifest_path.exists() and not self.manifest_path.is_file():
            raise ContextError("INTEGRITY_ERROR", "manifest.sqlite must be a regular file")
        if root_was_present and not self.manifest_path.exists() and _has_data_files(self.root):
            raise ContextError(
                "INTEGRITY_ERROR",
                "The context manifest is missing; object files cannot establish committed state.",
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _secure_dir(self.root)
        for directory in (self.objects_path, self.packages_path, self.derived_path, self.exports_path):
            directory.mkdir(mode=0o700, exist_ok=True)
            _secure_dir(directory)
        with self._connection(initialize=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, 2, 3, 4, SCHEMA_VERSION}:
                raise ContextError("INTEGRITY_ERROR", f"Unsupported context schema version: {version}")
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            base_tables = {"objects", "revisions", "changes", "idempotency"}
            v2_tables = base_tables | {
                "evidence_receipts", "receipt_events", "purge_events", "backup_catalog", "export_catalog",
            }
            v3_tables = v2_tables | {"derived_summaries"}
            v4_tables = v3_tables | {
                "projects", "project_grants", "identity_links", "identity_link_tokens",
                "session_bindings", "admin_events",
            }
            v5_tables = v4_tables | {"context_access_stats"}
            expected_by_version = {1: base_tables, 2: v2_tables, 3: v3_tables, 4: v4_tables, 5: v5_tables}
            if version == 0 and existing_tables:
                raise ContextError("INTEGRITY_ERROR", "An unversioned context manifest is not empty.")
            if version in expected_by_version and existing_tables != expected_by_version[version]:
                raise ContextError("INTEGRITY_ERROR", f"The v{version} context manifest schema is incomplete or unexpected.")
            if version == 5:
                self._require_schema_columns(connection, {
                    "revisions": {"metadata_json"},
                    "evidence_receipts": {"project_id"},
                    "purge_events": {"source_cleanup_complete", "source_cleanup_reason"},
                    "backup_catalog": {"state", "staging_path"},
                    "export_catalog": {"state", "staging_path"},
                    "context_access_stats": {
                        "account_id", "user_id", "scope_key", "uri", "revision",
                        "operation", "count", "last_access_at",
                    },
                })
            self._execute_schema(connection,
                """
                CREATE TABLE IF NOT EXISTS objects (
                    uri TEXT PRIMARY KEY,
                    object_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    project_id TEXT,
                    current_revision INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    preference_key TEXT,
                    scope_key TEXT,
                    value_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    uri TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (uri, revision),
                    FOREIGN KEY (uri) REFERENCES objects(uri)
                );
                CREATE TABLE IF NOT EXISTS changes (
                    change_id TEXT PRIMARY KEY,
                    uri TEXT NOT NULL,
                    before_revision INTEGER,
                    after_revision INTEGER NOT NULL,
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    project_id TEXT,
                    event_sequence INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_contract_version TEXT NOT NULL,
                    outcome_status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    source_content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_receipts_principal
                    ON evidence_receipts(user_id, session_id, task_id, event_sequence);
                CREATE TABLE IF NOT EXISTS receipt_events (
                    event_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    principal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (receipt_id) REFERENCES evidence_receipts(receipt_id),
                    UNIQUE (receipt_id, event_sequence)
                );
                CREATE TABLE IF NOT EXISTS purge_events (
                    purge_id TEXT PRIMARY KEY,
                    uri_sha256 TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cleanup_complete INTEGER NOT NULL DEFAULT 0,
                    residual_sessions_json TEXT NOT NULL DEFAULT '[]',
                    residual_backups_json TEXT NOT NULL DEFAULT '[]',
                    residual_exports_json TEXT NOT NULL DEFAULT '[]',
                    source_cleanup_complete INTEGER NOT NULL DEFAULT 1,
                    source_cleanup_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS backup_catalog (
                    backup_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'committed' CHECK(state IN ('pending','committed')),
                    staging_path TEXT
                );
                CREATE TABLE IF NOT EXISTS export_catalog (
                    export_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'committed' CHECK(state IN ('pending','committed')),
                    staging_path TEXT
                );
                CREATE TABLE IF NOT EXISTS derived_summaries (
                    uri TEXT NOT NULL,
                    view TEXT NOT NULL,
                    input_revision INTEGER NOT NULL,
                    generator_version TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (uri, view, input_revision, generator_version),
                    FOREIGN KEY (uri) REFERENCES objects(uri) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    bound_path TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_bound_path
                    ON projects(bound_path);
                CREATE TABLE IF NOT EXISTS project_grants (
                    project_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('reader', 'writer')),
                    revision INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, principal_id),
                    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS identity_links (
                    platform TEXT NOT NULL,
                    bot_account_id TEXT NOT NULL,
                    verified_sender_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    default_principal_id TEXT NOT NULL,
                    link_kind TEXT NOT NULL CHECK (link_kind IN ('default', 'linked')),
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, bot_account_id, verified_sender_id)
                );
                CREATE TABLE IF NOT EXISTS identity_link_tokens (
                    token_sha256 TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    target_platform TEXT NOT NULL,
                    target_bot_account_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    redeemed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_link_target_principal
                    ON identity_links(platform, bot_account_id, principal_id)
                    WHERE link_kind='linked';
                CREATE TABLE IF NOT EXISTS session_bindings (
                    entry_type TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    host_session_key TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (entry_type, account_key, host_session_key),
                    UNIQUE (entry_type, session_id)
                );
                CREATE TABLE IF NOT EXISTS admin_events (
                    event_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    before_revision INTEGER,
                    after_revision INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_access_stats (
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('read','list','search')),
                    count INTEGER NOT NULL CHECK(count > 0),
                    last_access_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, user_id, scope_key, uri, revision, operation)
                );
                """
            )
            if version == 1:
                for name, declaration in (
                    ("preference_key", "TEXT"),
                    ("scope_key", "TEXT"),
                    ("value_json", "TEXT"),
                ):
                    connection.execute(f"ALTER TABLE objects ADD COLUMN {name} {declaration}")
            if version < 5:
                for table, column, declaration in (
                    ("revisions", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("evidence_receipts", "project_id", "TEXT"),
                    ("purge_events", "source_cleanup_complete", "INTEGER NOT NULL DEFAULT 1"),
                    ("purge_events", "source_cleanup_reason", "TEXT NOT NULL DEFAULT ''"),
                    ("backup_catalog", "state", "TEXT NOT NULL DEFAULT 'committed'"),
                    ("backup_catalog", "staging_path", "TEXT"),
                    ("export_catalog", "state", "TEXT NOT NULL DEFAULT 'committed'"),
                    ("export_catalog", "staging_path", "TEXT"),
                ):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if column not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                for row in connection.execute(
                    """SELECT o.*, r.revision, r.content_sha256, r.relative_path, r.committed_at
                       FROM objects o JOIN revisions r
                         ON r.uri=o.uri AND r.revision=o.current_revision"""
                ).fetchall():
                    connection.execute(
                        "UPDATE revisions SET metadata_json=? WHERE uri=? AND revision=?",
                        (_json_compact(_metadata_for_current_row(row)), row["uri"], row["revision"]),
                    )
                connection.execute("PRAGMA user_version = 5")
            connection.commit()
        _secure_file(self.manifest_path)

    @staticmethod
    def _require_schema_columns(
        connection: sqlite3.Connection,
        required: dict[str, set[str]],
    ) -> None:
        for table, expected in required.items():
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if not expected.issubset(columns):
                raise ContextError(
                    "INTEGRITY_ERROR",
                    f"The v5 context manifest is missing required columns in {table}.",
                )
    @staticmethod
    def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
        statement = ""
        for line in script.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    connection.execute(statement)
                statement = ""
        if statement.strip():
            raise ContextError("INTEGRITY_ERROR", "The context schema script is incomplete.")
    @staticmethod
    def _assert_collection_preconditions(
        connection: sqlite3.Connection,
        preconditions: Sequence[CollectionPrecondition],
    ) -> None:
        for condition in preconditions:
            rows = connection.execute(
                """SELECT uri, current_revision FROM objects
                   WHERE uri=? OR substr(uri, 1, length(?) + 1)=?
                   ORDER BY uri""",
                (condition.scope, condition.scope, condition.scope + "/"),
            ).fetchall()
            fingerprint = collection_fingerprint(
                tuple((str(row["uri"]), int(row["current_revision"])) for row in rows)
            )
            if fingerprint != condition.fingerprint:
                raise ContextConflictError("Collection changed since its preimage was read.")
    def _connect(self, *, initialize: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.manifest_path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA secure_delete = ON")
        if initialize:
            connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    @contextmanager
    def _connection(self, *, initialize: bool = False):
        connection = self._connect(initialize=initialize)
        try:
            yield connection
        finally:
            connection.close()


    @staticmethod
    def _assert_expected_revisions(
        connection: sqlite3.Connection,
        entries: Sequence[tuple[ContextObject, int | None]],
    ) -> None:
        for obj, expected in entries:
            row = connection.execute(
                "SELECT current_revision FROM objects WHERE uri=?",
                (obj.uri,),
            ).fetchone()
            actual = None if row is None else int(row["current_revision"])
            if actual != expected:
                raise ContextConflictError(
                    f"Expected revision {expected!r} for {obj.uri}, found {actual!r}."
                )

    def _idempotent_result(
        self,
        key: str | None,
        payload_hash: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[ContextObject, ...] | None:
        if key is None:
            return None
        owns_connection = connection is None
        if connection is None:
            connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_sha256, result_json FROM idempotency WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if row["payload_sha256"] != payload_hash:
                raise ContextConflictError("The idempotency key was already used with another payload.")
            revision_rows = json.loads(row["result_json"])
            objects: list[ContextObject] = []
            for uri, revision in revision_rows:
                revision_row = connection.execute(
                    """SELECT o.*, r.revision, r.content_sha256, r.relative_path,
                              r.metadata_json AS revision_metadata_json, r.committed_at
                       FROM objects o JOIN revisions r ON r.uri=o.uri
                       WHERE o.uri=? AND r.revision=?""",
                    (uri, int(revision)),
                ).fetchone()
                if revision_row is None:
                    raise ContextError("INTEGRITY_ERROR", "An idempotency result references a missing revision.")
                objects.append(self._object_from_row(revision_row))
            return tuple(objects)
        finally:
            if owns_connection:
                connection.close()

    def _publish_revision(self, relative_path: str, content: str) -> None:
        target = self._path_for_relative(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _secure_dir(target.parent)
        data = content.encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".revision-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            _secure_file(target)
            _fsync_dir(target.parent)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _commit_transaction(connection: sqlite3.Connection) -> None:
        """Commit manifest pointers after every immutable revision is published."""

        connection.commit()

    def _path_for_relative(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ContextError("INTEGRITY_ERROR", "The manifest contains an unsafe revision path.")
        target = self.root / relative
        cursor = self.root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ContextError("INTEGRITY_ERROR", "The manifest contains a symlinked revision path.")
        try:
            target.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ContextError("INTEGRITY_ERROR", "The revision path escaped the context root.") from exc
        return target

    def _object_from_row(self, row: sqlite3.Row) -> ContextObject:
        relative_path = str(row["relative_path"])
        path = self._path_for_relative(relative_path)
        if path.is_symlink() or not path.is_file():
            raise ContextError("INTEGRITY_ERROR", "The context revision file is missing or unsafe.")
        try:
            content_bytes = path.read_bytes()
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContextError("INTEGRITY_ERROR", "The context revision cannot be read as UTF-8.") from exc
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        if content_hash != row["content_sha256"]:
            raise ContextError("INTEGRITY_ERROR", "The context revision hash does not match the manifest.")
        try:
            revision_metadata = json.loads(row["revision_metadata_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ContextError("INTEGRITY_ERROR", "The revision metadata is malformed.") from exc
        if not isinstance(revision_metadata, dict):
            raise ContextError("INTEGRITY_ERROR", "The revision metadata is malformed.")
        if revision_metadata:
            try:
                return ContextObject(
                    object_id=revision_metadata["object_id"],
                    uri=revision_metadata["uri"],
                    kind=revision_metadata["kind"],
                    owner_id=revision_metadata["owner_id"],
                    project_id=revision_metadata["project_id"],
                    revision=int(row["revision"]),
                    content=content,
                    title=revision_metadata["title"],
                    tags=tuple(revision_metadata["tags"]),
                    status=revision_metadata["status"],
                    source_refs=tuple(revision_metadata["source_refs"]),
                    preference_key=revision_metadata["preference_key"],
                    scope_key=revision_metadata["scope_key"],
                    value=revision_metadata["value"],
                    schema_version=int(revision_metadata["schema_version"]),
                    created_at=revision_metadata["created_at"],
                    updated_at=revision_metadata["updated_at"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ContextError("INTEGRITY_ERROR", "The revision metadata is malformed.") from exc
        if int(row["revision"]) == int(row["current_revision"]):
            raise ContextError(
                "INTEGRITY_ERROR",
                "The current revision is missing its immutable metadata snapshot.",
            )
        # Old history has immutable body bytes but no provable historical metadata.
        parts = str(row["uri"]).split("/")
        authority = parts[2] if len(parts) > 2 else ""
        segments = parts[3:]
        owner_id = segments[0] if authority == "users" and segments else "legacy_unverified"
        project_id = segments[0] if authority == "projects" and segments else None
        memory_kind = segments[2] if len(segments) > 2 and segments[1] == "memories" else ""
        kind = {
            "preferences": "user_preference",
            "facts": "environment_fact",
            "decisions": "project_decision",
            "experiences": "experience",
        }.get(memory_kind, "legacy_unverified")
        return ContextObject(
            object_id=segments[-1] if segments else "legacy_unverified",
            uri=str(row["uri"]), kind=kind, owner_id=owner_id, project_id=project_id,
            revision=int(row["revision"]), content=content, title="", tags=(),
            status="legacy_unverified", source_refs=(), preference_key=None,
            scope_key=None, value=None, schema_version=1,
            created_at=str(row["committed_at"]), updated_at=str(row["committed_at"]),
        )

    @staticmethod
    def _select_current(connection: sqlite3.Connection, uri: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT current_revision, created_at FROM objects WHERE uri=?",
            (uri,),
        ).fetchone()

    @staticmethod
    def _validate_object(obj: ContextObject) -> None:
        if not isinstance(obj.uri, str) or not obj.uri.startswith("za://"):
            raise ContextError("INVALID_URI", "Context objects must use a za:// URI.")
        if not isinstance(obj.content, str):
            raise ValueError("context content must be text")
        if not obj.object_id or not obj.owner_id or not obj.kind:
            raise ValueError("object_id, owner_id, and kind are required")
        if obj.status not in {
            "active",
            "candidate_unverified",
            "conflicted",
            "superseded",
            "retracted",
            "legacy_unverified",
            "sensitive_quarantined",
        }:
            raise ValueError("unsupported context object status")
        if obj.preference_key is not None and (not obj.scope_key or obj.value is None):
            raise ValueError("preference_key requires scope_key and value")
        try:
            json.dumps(obj.value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("context value must be JSON serializable") from exc

def _metadata_for_current_row(row: sqlite3.Row) -> dict[str, object]:
    """Return the provable metadata carried by an object's current pointer."""

    return {
        "object_id": row["object_id"],
        "uri": row["uri"],
        "kind": row["kind"],
        "owner_id": row["owner_id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "tags": json.loads(row["tags_json"]),
        "status": row["status"],
        "source_refs": json.loads(row["source_refs_json"]),
        "preference_key": row["preference_key"],
        "scope_key": row["scope_key"],
        "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
        "schema_version": int(row["schema_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _metadata_json(obj: ContextObject) -> str:
    return _json_compact({
        "object_id": obj.object_id,
        "uri": obj.uri,
        "kind": obj.kind,
        "owner_id": obj.owner_id,
        "project_id": obj.project_id,
        "title": obj.title,
        "tags": list(obj.tags),
        "status": obj.status,
        "source_refs": list(obj.source_refs),
        "preference_key": obj.preference_key,
        "scope_key": obj.scope_key,
        "value": obj.value,
        "schema_version": obj.schema_version,
        "created_at": obj.created_at,
        "updated_at": obj.updated_at,
    })


def _json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



def _revision_relative_path(uri: str, revision: int, content_hash: str) -> str:
    uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    return f"objects/{uri_hash[:2]}/{uri_hash}/revisions/{revision}-{content_hash}.md"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _has_data_files(root: Path) -> bool:
    if not root.is_dir():
        return False
    for name in ("objects", "packages", "derived", "exports"):
        candidate = root / name
        if candidate.exists() and any(candidate.iterdir()):
            return True
    return False


def _payload_hash(entries: Sequence[tuple[ContextObject, int | None]]) -> str:
    serialized = [
        {
            "expected_revision": expected,
            "object_id": obj.object_id,
            "uri": obj.uri,
            "kind": obj.kind,
            "owner_id": obj.owner_id,
            "project_id": obj.project_id,
            "content_sha256": obj.content_sha256,
            "title": obj.title,
            "tags": obj.tags,
            "status": obj.status,
            "source_refs": obj.source_refs,
            "preference_key": obj.preference_key,
            "scope_key": obj.scope_key,
            "value": obj.value,
            "schema_version": obj.schema_version,
        }
        for obj, expected in entries
    ]
    return hashlib.sha256(
        json.dumps(serialized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_receipt(receipt: EvidenceReceipt) -> None:
    if receipt.source_type not in {"user_message", "tool_result", "resource_import"}:
        raise ContextError("INVALID_SOURCE", "The evidence receipt source type is unsupported.")
    if receipt.outcome_status not in {"success", "error", "interrupt", "unknown"}:
        raise ContextError("INVALID_SOURCE", "The evidence receipt outcome is unsupported.")
    if (
        not receipt.receipt_id
        or not receipt.account_id
        or not receipt.user_id
        or not receipt.session_id
        or not receipt.task_id
        or not receipt.source_id
        or receipt.event_sequence < 0
    ):
        raise ContextError("INVALID_SOURCE", "The evidence receipt identity is incomplete.")
    if receipt.source_type == "tool_result" and (
        not receipt.tool_name or not receipt.tool_contract_version
    ):
        raise ContextError("INVALID_SOURCE", "Tool evidence requires a named contract version.")
    if len(receipt.excerpt) > 8192:
        raise ContextError("INVALID_SOURCE", "Evidence excerpts exceed the storage limit.")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt.source_content_sha256):
        raise ContextError("INVALID_SOURCE", "Evidence receipts require a SHA-256 source hash.")
    try:
        json.dumps(receipt.result_fields, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContextError("INVALID_SOURCE", "Evidence fields must be JSON values.") from exc


def _receipt_payload_hash(receipt: EvidenceReceipt) -> str:
    payload = {
        "receipt_id": receipt.receipt_id,
        "source_type": receipt.source_type,
        "account_id": receipt.account_id,
        "user_id": receipt.user_id,
        "session_id": receipt.session_id,
        "task_id": receipt.task_id,
        "project_id": receipt.project_id,
        "event_sequence": receipt.event_sequence,
        "source_id": receipt.source_id,
        "tool_name": receipt.tool_name,
        "tool_contract_version": receipt.tool_contract_version,
        "outcome_status": receipt.outcome_status,
        "result_fields": receipt.result_fields,
        "excerpt": receipt.excerpt,
        "source_revision": receipt.source_revision,
        "source_content_sha256": receipt.source_content_sha256,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence_receipt_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> EvidenceReceipt:
    try:
        result_fields = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContextError("INTEGRITY_ERROR", "Evidence receipt fields are malformed.") from exc
    latest = connection.execute(
        """SELECT event_type, reason_code FROM receipt_events WHERE receipt_id=?
           ORDER BY event_sequence DESC LIMIT 1""",
        (row["receipt_id"],),
    ).fetchone()
    return EvidenceReceipt(
        receipt_id=row["receipt_id"],
        source_type=row["source_type"],
        account_id=row["account_id"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        event_sequence=int(row["event_sequence"]),
        source_id=row["source_id"],
        tool_name=row["tool_name"],
        tool_contract_version=row["tool_contract_version"],
        outcome_status=row["outcome_status"],
        result_fields=result_fields,
        excerpt=row["excerpt"],
        source_revision=row["source_revision"],
        source_content_sha256=row["source_content_sha256"],
        created_at=row["created_at"],
        revoked=latest is not None and latest["event_type"] in {"revoked", "redacted"},
        last_event_type="" if latest is None else str(latest["event_type"]),
        last_event_reason_code="" if latest is None else str(latest["reason_code"]),
    )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _secure_dir(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ContextError("INTEGRITY_ERROR", "A context data directory is not a real directory.")
    if os.name != "nt":
        os.chmod(path, 0o700)


def _secure_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ContextError("INTEGRITY_ERROR", "A context data file is not a regular file.")
    if os.name != "nt":
        os.chmod(path, 0o600)



def _recover_pending_export(
    store: LocalContextStore,
    connection: sqlite3.Connection,
    target: Path,
    export_id: str,
    staging_value: object,
) -> sqlite3.Row | None:
    """Validate the published export postimage before completing its catalog row."""
    if not isinstance(staging_value, str) or not staging_value:
        raise ContextError("INTEGRITY_ERROR", "A pending export has no recovery path.")
    staging = Path(staging_value)
    prefix = f".{target.name}."
    suffix = ".staging"
    token = staging.name[len(prefix):-len(suffix)] if staging.name.startswith(prefix) and staging.name.endswith(suffix) else ""
    if staging.parent != target.parent or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ContextError("INTEGRITY_ERROR", "A pending export has an unsafe staging path.")
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ContextError("INTEGRITY_ERROR", "A pending export target is unsafe.")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContextError("INTEGRITY_ERROR", "A pending export target cannot be verified.") from exc
        if not isinstance(payload, dict) or payload.get("format") != "zero-agent-context-export/v1" or not isinstance(payload.get("objects"), list):
            raise ContextError("INTEGRITY_ERROR", "A pending export target has an unsupported format.")
        seen: set[str] = set()
        for exported in payload["objects"]:
            if not isinstance(exported, dict) or not isinstance(exported.get("uri"), str) or exported["uri"] in seen:
                raise ContextError("INTEGRITY_ERROR", "A pending export contains a malformed or duplicate object.")
            seen.add(exported["uri"])
            row = connection.execute(
                """SELECT o.*, r.revision AS revision, r.content_sha256, r.relative_path,
                          r.metadata_json AS revision_metadata_json, r.committed_at
                   FROM objects o JOIN revisions r ON r.uri=o.uri AND r.revision=o.current_revision
                   WHERE o.uri=?""",
                (exported["uri"],),
            ).fetchone()
            if row is None:
                raise ContextError("INTEGRITY_ERROR", "A pending export references a missing object.")
            obj = store._object_from_row(row)
            expected = {
                "object_id": obj.object_id, "uri": obj.uri, "kind": obj.kind,
                "owner_id": obj.owner_id, "project_id": obj.project_id,
                "revision": obj.revision, "content_sha256": obj.content_sha256,
                "title": obj.title, "tags": list(obj.tags), "status": obj.status,
                "source_refs": list(obj.source_refs), "schema_version": obj.schema_version,
                "content": obj.content, "preference_key": obj.preference_key,
                "scope_key": obj.scope_key, "value": obj.value,
            }
            if json.dumps(exported, sort_keys=True, separators=(",", ":"), ensure_ascii=False) != json.dumps(
                expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ):
                raise ContextError("INTEGRITY_ERROR", "A pending export object does not match the current revision.")
        if staging.exists():
            if staging.is_symlink() or not staging.is_file():
                raise ContextError("INTEGRITY_ERROR", "A pending export staging file is unsafe.")
            staging.unlink()
        connection.execute(
            "UPDATE export_catalog SET state='committed',staging_path=NULL WHERE export_id=? AND state='pending'",
            (export_id,),
        )
        return connection.execute(
            "SELECT export_id,state FROM export_catalog WHERE export_id=?",
            (export_id,),
        ).fetchone()
    if staging.exists():
        if staging.is_symlink() or not staging.is_file():
            raise ContextError("INTEGRITY_ERROR", "A pending export staging file is unsafe.")
        staging.unlink()
    connection.execute("DELETE FROM export_catalog WHERE export_id=? AND state='pending'", (export_id,))
    return None

def _unlink_tree_no_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise OSError("unsafe purge directory")
    for child in root.iterdir():
        if child.is_symlink():
            raise OSError("unsafe purge entry")
        if child.is_dir():
            _unlink_tree_no_symlinks(child)
        elif child.is_file():
            child.unlink()
        else:
            raise OSError("unsupported purge entry")
    root.rmdir()

def _secure_tree(root: Path) -> None:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        _secure_dir(current)
        for name in dirnames:
            child = current / name
            if child.is_symlink():
                raise ContextError("INTEGRITY_ERROR", "A backup contains a directory symlink.")
        for name in filenames:
            child = current / name
            if child.is_symlink():
                raise ContextError("INTEGRITY_ERROR", "A backup contains a file symlink.")


def _verify_backup(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ContextError("INTEGRITY_ERROR", "The context backup root is not a regular directory.")
    manifest = root / "manifest.sqlite"
    if manifest.is_symlink() or not manifest.is_file():
        raise ContextError("INTEGRITY_ERROR", "The context backup has no regular manifest.")
    connection = None
    try:
        connection = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {1, 2, 3, 4, 5}:
            raise ContextError("INTEGRITY_ERROR", f"Unsupported backup schema version: {version}")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ContextError("INTEGRITY_ERROR", "The context backup manifest failed SQLite integrity check.")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        base = {"objects", "revisions", "changes", "idempotency"}
        v2 = base | {"evidence_receipts", "receipt_events", "purge_events", "backup_catalog", "export_catalog"}
        v3 = v2 | {"derived_summaries"}
        v4 = v3 | {"projects", "project_grants", "identity_links", "identity_link_tokens", "session_bindings", "admin_events"}
        v5 = v4 | {"context_access_stats"}
        expected = {1: base, 2: v2, 3: v3, 4: v4, 5: v5}[version]
        if tables != expected:
            raise ContextError("INTEGRITY_ERROR", "The context backup schema is incomplete or unexpected.")
        if version == 5:
            required_columns = {
                "revisions": {"metadata_json"},
                "evidence_receipts": {"project_id"},
                "purge_events": {"source_cleanup_complete", "source_cleanup_reason"},
                "backup_catalog": {"state", "staging_path"},
                "export_catalog": {"state", "staging_path"},
                "context_access_stats": {"account_id", "user_id", "scope_key", "uri", "revision", "operation", "count", "last_access_at"},
            }
            for table, required in required_columns.items():
                columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                if not required.issubset(columns):
                    raise ContextError("INTEGRITY_ERROR", f"The v5 context backup is missing columns in {table}.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ContextError("INTEGRITY_ERROR", "The context backup has broken manifest references.")
        missing_current = connection.execute(
            """SELECT COUNT(*) FROM objects o LEFT JOIN revisions r
               ON r.uri=o.uri AND r.revision=o.current_revision WHERE r.uri IS NULL"""
        ).fetchone()[0]
        if missing_current:
            raise ContextError("INTEGRITY_ERROR", "The context backup has an unresolvable current revision.")
        rows = connection.execute("SELECT relative_path, content_sha256 FROM revisions").fetchall()
        connection.close()
        connection = None
        for relative_path, expected_hash in rows:
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ContextError("INTEGRITY_ERROR", "The backup contains an unsafe revision path.")
            path = root / relative
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ContextError("INTEGRITY_ERROR", "The backup contains a symlinked revision.")
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise ContextError("INTEGRITY_ERROR", "The backup revision escaped its root.") from exc
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ContextError("INTEGRITY_ERROR", "The backup revision file is missing or its hash does not match.")
    except sqlite3.DatabaseError as exc:
        raise ContextError("INTEGRITY_ERROR", "The context backup manifest is invalid.") from exc
    finally:
        if connection is not None:
            connection.close()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
