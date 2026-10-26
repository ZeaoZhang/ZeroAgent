"""Shared local project, identity, and session-binding administration."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.local_store import LocalContextStore

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
_SAFE_PLATFORM = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ROLES = {"reader", "writer"}


@dataclass(frozen=True)
class ProjectBinding:
    project_id: str
    owner_id: str
    name: str
    bound_path: str
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ProjectAccess:
    project: ProjectBinding
    role: str


@dataclass(frozen=True)
class IdentityBinding:
    platform: str
    bot_account_id: str
    verified_sender_id: str
    principal_id: str
    default_principal_id: str
    link_kind: str
    revision: int


@dataclass(frozen=True)
class LinkChallenge:
    code: str
    target_platform: str
    target_bot_account_id: str
    expires_at: str


class ContextAdminService:
    """One CAS and audit boundary for CLI, desktop, and bot identity writes."""

    def __init__(
        self,
        store: LocalContextStore,
        *,
        now: Callable[[], datetime] | None = None,
        link_ttl: timedelta = timedelta(minutes=10),
        max_link_attempts: int = 5,
    ) -> None:
        """Create the shared local administration and audit boundary.

        Args:
            store: Local context store used for bindings and audit events.
            now: Optional UTC clock provider for deterministic expiration tests.
            link_ttl: Lifetime of a newly issued one-time identity code.
            max_link_attempts: Wrong-target redemption attempts before expiration.
        """
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.link_ttl = link_ttl
        self.max_link_attempts = max_link_attempts

    def create_project(self, actor_id: str, path: str | Path, *, name: str = "") -> ProjectBinding:
        """Create a uniquely identified project owned by the actor.

        Args:
            actor_id: Trusted local principal creating the project.
            path: Existing project directory to bind.
            name: Optional display name; defaults to the directory name.

        Returns:
            The committed project binding at revision 1.

        Raises:
            ContextConflictError: If the directory is already bound.
            ContextError: If the actor, name, or project directory is invalid.
        """
        actor = _require_key(actor_id, "principal")
        bound_path = _project_path(path)
        project_name = name.strip() or bound_path.name or "Project"
        if len(project_name) > 160:
            raise ContextError("INVALID_SOURCE", "The project name is too long.")
        project_id = "prj_" + secrets.token_hex(16)
        now = _iso(self._now())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT 1 FROM projects WHERE bound_path=?", (str(bound_path),)
            ).fetchone()
            if duplicate is not None:
                raise ContextConflictError("This directory is already bound to a project.")
            connection.execute(
                """INSERT INTO projects (
                     project_id, owner_id, name, bound_path, revision, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (project_id, actor, project_name, str(bound_path), now, now),
            )
            self._audit(
                connection, actor, "project_create", "project", project_id,
                None, 1, {"name": project_name}, now,
            )
            connection.commit()
            return ProjectBinding(project_id, actor, project_name, str(bound_path), 1, now, now)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_project(self, project_id: str) -> ProjectBinding:
        """Read a project binding by its stable project ID.

        Args:
            project_id: Stable project identity.

        Returns:
            The current project binding.

        Raises:
            ContextError: If the project does not exist or its identity is invalid.
        """
        project_id = _require_key(project_id, "project id")
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise ContextError("NOT_FOUND", "The project binding was not found.")
        return _project_from_row(row)

    def rebind_project(
        self,
        actor_id: str,
        project_id: str,
        path: str | Path,
        *,
        expected_revision: int,
    ) -> ProjectBinding:
        """Rebind a project directory using the expected revision.

        Args:
            actor_id: Project owner performing the rebind.
            project_id: Stable project identity.
            path: Existing destination directory.
            expected_revision: Current project revision.

        Returns:
            The updated project binding.
        """
        actor = _require_key(actor_id, "principal")
        project_id = _require_key(project_id, "project id")
        bound_path = _project_path(path)
        now = _iso(self._now())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._project_row(connection, project_id)
            self._require_owner(current, actor)
            self._check_revision(current, expected_revision)
            duplicate = connection.execute(
                "SELECT project_id FROM projects WHERE bound_path=? AND project_id<>?",
                (str(bound_path), project_id),
            ).fetchone()
            if duplicate is not None:
                raise ContextConflictError("This directory is already bound to another project.")
            next_revision = expected_revision + 1
            connection.execute(
                "UPDATE projects SET bound_path=?, revision=?, updated_at=? WHERE project_id=? AND revision=?",
                (str(bound_path), next_revision, now, project_id, expected_revision),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ContextConflictError("The project binding changed during rebind.")
            self._audit(
                connection, actor, "project_rebind", "project", project_id,
                expected_revision, next_revision, {}, now,
            )
            updated = self._project_row(connection, project_id)
            connection.commit()
            return _project_from_row(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def grant_project(
        self,
        actor_id: str,
        project_id: str,
        principal_id: str,
        role: str,
        *,
        expected_revision: int,
    ) -> ProjectBinding:
        """Grant a reader or writer role at the expected project revision.

        Args:
            actor_id: Current project owner.
            project_id: Stable project identity.
            principal_id: Principal receiving the role.
            role: ``reader`` or ``writer``.
            expected_revision: Current project revision for CAS.

        Returns:
            The updated project binding.

        Raises:
            ContextConflictError: If the project revision is stale.
            ContextError: If the actor is not owner or the role is invalid.
        """
        if role not in _ROLES:
            raise ContextError("INVALID_SOURCE", "Project grants must use reader or writer role.")
        return self._mutate_project_membership(
            actor_id, project_id, principal_id, role, expected_revision=expected_revision,
            action="project_grant",
        )

    def revoke_project(
        self,
        actor_id: str,
        project_id: str,
        principal_id: str,
        *,
        expected_revision: int,
    ) -> ProjectBinding:
        """Revoke one member grant at the expected project revision.

        Args:
            actor_id: Current project owner.
            project_id: Stable project identity.
            principal_id: Principal whose grant is removed.
            expected_revision: Current project revision for CAS.

        Returns:
            The updated project binding.

        Raises:
            ContextConflictError: If the project revision is stale.
            ContextError: If the actor is not owner or no grant exists.
        """
        return self._mutate_project_membership(
            actor_id, project_id, principal_id, None, expected_revision=expected_revision,
            action="project_revoke",
        )

    def transfer_project_owner(
        self,
        actor_id: str,
        project_id: str,
        new_owner_id: str,
        *,
        expected_revision: int,
    ) -> ProjectBinding:
        """Transfer ownership at the expected project revision.

        Args:
            actor_id: Current project owner.
            project_id: Stable project identity.
            new_owner_id: Principal receiving ownership.
            expected_revision: Current project revision for CAS.

        Returns:
            The updated project binding.

        Raises:
            ContextConflictError: If the project revision is stale.
            ContextError: If the actor is not owner or the new owner is invalid.
        """
        actor = _require_key(actor_id, "principal")
        new_owner = _require_key(new_owner_id, "principal")
        project_id = _require_key(project_id, "project id")
        now = _iso(self._now())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._project_row(connection, project_id)
            self._require_owner(current, actor)
            self._check_revision(current, expected_revision)
            if new_owner == actor:
                raise ContextError("INVALID_SOURCE", "The new owner must differ from the current owner.")
            next_revision = expected_revision + 1
            connection.execute(
                "UPDATE projects SET owner_id=?, revision=?, updated_at=? WHERE project_id=? AND revision=?",
                (new_owner, next_revision, now, project_id, expected_revision),
            )
            connection.execute(
                "DELETE FROM project_grants WHERE project_id=? AND principal_id=?",
                (project_id, new_owner),
            )
            self._audit(
                connection, actor, "project_transfer_owner", "project", project_id,
                expected_revision, next_revision, {"new_owner_id": new_owner}, now,
            )
            updated = self._project_row(connection, project_id)
            connection.commit()
            return _project_from_row(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def project_access(
        self,
        project_id: str,
        principal_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ProjectAccess | None:
        """Return a principal's project role, or ``None`` when ungranted.

        Args:
            project_id: Stable project identity.
            principal_id: Principal whose grant is checked.
            connection: Optional transaction connection to reuse.

        Returns:
            The project binding and role, or ``None`` when ungranted.
        """
        if connection is None:
            project = self.get_project(project_id)
            if project.owner_id == principal_id:
                return ProjectAccess(project, "owner")
            with self.store._connection() as owned:
                row = owned.execute(
                    "SELECT role FROM project_grants WHERE project_id=? AND principal_id=?",
                    (project.project_id, principal_id),
                ).fetchone()
        else:
            row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                return None
            project = _project_from_row(row)
            if project.owner_id == principal_id:
                return ProjectAccess(project, "owner")
            row = connection.execute(
                "SELECT role FROM project_grants WHERE project_id=? AND principal_id=?",
                (project.project_id, principal_id),
            ).fetchone()
        return None if row is None else ProjectAccess(project, row["role"])

    def capabilities_for_project(self, project_id: str, principal_id: str) -> frozenset[str]:
        """Return the context capabilities implied by a project grant.

        Args:
            project_id: Stable project identity.
            principal_id: Principal whose grant is checked.

        Returns:
            Read/write capability names, or an empty set when ungranted.
        """
        access = self.project_access(project_id, principal_id)
        if access is None:
            return frozenset()
        prefix = f"project:{project_id}:"
        return frozenset({prefix + "read", prefix + "write"} if access.role in {"owner", "writer"} else {prefix + "read"})

    def resolve_project_for_path(self, path: str | Path, principal_id: str) -> ProjectAccess | None:
        """Resolve the deepest bound project containing a workspace path.

        Args:
            path: Current task workspace path.
            principal_id: Trusted current principal.

        Returns:
            The matching project grant, or ``None`` when the path is unbound.

        Raises:
            ContextError: If the principal is not granted access to the deepest match.
        """
        candidate = Path(path).expanduser().resolve(strict=False)
        with self.store._connection() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY length(bound_path) DESC").fetchall()
        for row in rows:
            root = Path(row["bound_path"])
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            access = self.project_access(row["project_id"], principal_id)
            if access is None:
                raise ContextError("PERMISSION_DENIED", "The current principal has no grant for this project binding.")
            return access
        return None

    def bind_session(
        self,
        entry_type: str,
        account_key: str,
        host_session_key: str,
        principal_id: str,
    ) -> str:
        """Bind a host session to one principal and return its persistent ID.

        Args:
            entry_type: Stable local entry point type.
            account_key: Host account or channel-account identity.
            host_session_key: Host-defined durable session key.
            principal_id: Trusted principal assigned to the session.

        Returns:
            The stable generated session ID.

        Raises:
            ContextConflictError: If the host key is already bound to another principal.
        """
        entry = _require_platform(entry_type)
        account = _require_key(account_key, "account key")
        host_session = _require_key(host_session_key, "session key")
        principal = _require_key(principal_id, "principal")
        now = _iso(self._now())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT principal_id, session_id FROM session_bindings
                   WHERE entry_type=? AND account_key=? AND host_session_key=?""",
                (entry, account, host_session),
            ).fetchone()
            if row is not None:
                if row["principal_id"] != principal:
                    raise ContextConflictError("The host session is bound to another principal.")
                connection.commit()
                return row["session_id"]
            session_id = "ses_" + secrets.token_hex(16)
            connection.execute(
                """INSERT INTO session_bindings (
                     entry_type, account_key, host_session_key, principal_id, session_id,
                     created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entry, account, host_session, principal, session_id, now, now),
            )
            self._audit(
                connection, principal, "session_bind", "session", session_id,
                None, 1, {"entry_type": entry}, now,
            )
            connection.commit()
            return session_id
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ContextConflictError("The host session binding conflicts with an existing session.") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_bot_principal(
        self,
        platform: str,
        bot_account_id: str,
        verified_sender_id: str,
        *,
        verified: bool,
    ) -> str:
        """Resolve a principal from platform-verified bot and sender IDs.

        Args:
            platform: External platform identifier.
            bot_account_id: Verified bot/account identifier on that platform.
            verified_sender_id: Platform-verified sender identity.
            verified: Whether the entry point verified the sender ID.

        Returns:
            Existing linked principal or the sender's isolated default principal.

        Raises:
            ContextError: If the sender identity is not verified or malformed.
        """
        platform = _require_platform(platform)
        account = _require_key(bot_account_id, "bot account")
        sender = _require_key(verified_sender_id, "verified sender")
        if verified is not True:
            raise ContextError("PERMISSION_DENIED", "Bot identity requires a platform-verified sender id.")
        now = _iso(self._now())
        default_principal = _default_bot_principal(platform, account, sender)
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._identity_row(connection, platform, account, sender)
            if row is None:
                connection.execute(
                    """INSERT INTO identity_links (
                         platform, bot_account_id, verified_sender_id, principal_id,
                         default_principal_id, link_kind, revision, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 'default', 1, ?, ?)""",
                    (platform, account, sender, default_principal, default_principal, now, now),
                )
                self._audit(
                    connection, default_principal, "bot_identity_create", "identity",
                    f"{platform}/{account}/{hashlib.sha256(sender.encode()).hexdigest()[:12]}",
                    None, 1, {}, now,
                )
                connection.commit()
                return default_principal
            connection.commit()
            return row["principal_id"]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def begin_identity_link(
        self,
        actor_id: str,
        target_platform: str,
        target_bot_account_id: str,
    ) -> LinkChallenge:
        """Create a one-time identity link bound to one target bot account.

        Args:
            actor_id: Local principal initiating the link.
            target_platform: Target platform identifier.
            target_bot_account_id: Exact target bot account.

        Returns:
            Link code and target/expiry metadata; only its hash is persisted.
        """
        actor = _require_key(actor_id, "principal")
        platform = _require_platform(target_platform)
        account = _require_key(target_bot_account_id, "bot account")
        code = secrets.token_urlsafe(24)
        now = self._now()
        expires = now + self.link_ttl
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO identity_link_tokens (
                     token_sha256, principal_id, target_platform, target_bot_account_id,
                     expires_at, attempts, redeemed_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, 0, NULL, ?)""",
                (_sha256(code), actor, platform, account, _iso(expires), _iso(now)),
            )
            self._audit(
                connection, actor, "identity_link_begin", "identity_target",
                f"{platform}/{account}", None, 1, {"expires_at": _iso(expires)}, _iso(now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return LinkChallenge(code, platform, account, _iso(expires))

    def redeem_identity_link(
        self,
        code: str,
        *,
        platform: str,
        bot_account_id: str,
        verified_sender_id: str,
        verified: bool,
        is_private_chat: bool,
    ) -> IdentityBinding:
        """Redeem a one-time link from the verified target bot's private chat.

        Args:
            code: One-time code created for this target bot.
            platform: Current platform identifier.
            bot_account_id: Current verified bot account.
            verified_sender_id: Platform-verified sender identity.
            verified: Whether the sender was verified by the platform adapter.
            is_private_chat: Whether redemption came from a private conversation.

        Returns:
            The committed linked identity.

        Raises:
            ContextError: If the code, target, sender, expiry, or chat scope is invalid.
            ContextConflictError: If the identity is already linked elsewhere.
        """
        platform = _require_platform(platform)
        account = _require_key(bot_account_id, "bot account")
        sender = _require_key(verified_sender_id, "verified sender")
        if not is_private_chat:
            raise ContextError("PERMISSION_DENIED", "Identity links can only be redeemed in a private bot chat.")
        if verified is not True:
            raise ContextError("PERMISSION_DENIED", "Identity linking requires a platform-verified sender id.")
        if not isinstance(code, str) or len(code) < 20 or len(code) > 128:
            raise ContextError("INVALID_SOURCE", "The one-time link code is invalid.")
        now = self._now()
        now_iso = _iso(now)
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            token = connection.execute(
                "SELECT * FROM identity_link_tokens WHERE token_sha256=?",
                (_sha256(code),),
            ).fetchone()
            if token is None or token["redeemed_at"] is not None:
                raise ContextError("INVALID_SOURCE", "The one-time link code is invalid or already used.")
            if token["target_platform"] != platform or token["target_bot_account_id"] != account:
                attempts = int(token["attempts"]) + 1
                connection.execute(
                    "UPDATE identity_link_tokens SET attempts=?, expires_at=? WHERE token_sha256=?",
                    (attempts, now_iso if attempts >= self.max_link_attempts else token["expires_at"], token["token_sha256"]),
                )
                connection.commit()
                raise ContextError("PERMISSION_DENIED", "The one-time link code is bound to another bot account.")
            if datetime.fromisoformat(token["expires_at"]) <= now:
                raise ContextError("INVALID_SOURCE", "The one-time link code has expired.")
            row = self._identity_row(connection, platform, account, sender)
            if row is None:
                default_principal = _default_bot_principal(platform, account, sender)
                next_revision = 1
                connection.execute(
                    """INSERT INTO identity_links (
                         platform, bot_account_id, verified_sender_id, principal_id,
                         default_principal_id, link_kind, revision, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 'linked', 1, ?, ?)""",
                    (
                        platform, account, sender, token["principal_id"], default_principal,
                        now_iso, now_iso,
                    ),
                )
            else:
                if row["link_kind"] == "linked" and row["principal_id"] != token["principal_id"]:
                    raise ContextConflictError("This bot identity is already linked to another principal.")
                default_principal = row["default_principal_id"]
                next_revision = int(row["revision"]) + 1
                connection.execute(
                    """UPDATE identity_links SET principal_id=?, link_kind='linked',
                       revision=?, updated_at=? WHERE platform=? AND bot_account_id=?
                       AND verified_sender_id=? AND revision=?""",
                    (
                        token["principal_id"], next_revision, now_iso,
                        platform, account, sender, row["revision"],
                    ),
                )
            connection.execute(
                "UPDATE identity_link_tokens SET redeemed_at=? WHERE token_sha256=? AND redeemed_at IS NULL",
                (now_iso, token["token_sha256"]),
            )
            identity_id = f"{platform}/{account}/{hashlib.sha256(sender.encode()).hexdigest()[:12]}"
            self._audit(
                connection, token["principal_id"], "identity_link_redeem", "identity",
                identity_id, None if row is None else int(row["revision"]), next_revision,
                {}, now_iso,
            )
            binding = IdentityBinding(
                platform, account, sender, token["principal_id"], default_principal,
                "linked", next_revision,
            )
            connection.commit()
            return binding
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ContextConflictError("This bot principal already has a linked identity for the target account.") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_identities(self, principal_id: str) -> tuple[IdentityBinding, ...]:
        """List bot identities linked to the requested principal."""
        principal = _require_key(principal_id, "principal")
        with self.store._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM identity_links WHERE principal_id=?
                   ORDER BY platform, bot_account_id, verified_sender_id""",
                (principal,),
            ).fetchall()
        return tuple(_identity_from_row(row) for row in rows)

    def revoke_identity_link(
        self,
        actor_id: str,
        *,
        platform: str,
        bot_account_id: str,
        principal_id: str,
        expected_revision: int,
    ) -> IdentityBinding:
        """Revoke a linked bot identity at its exact expected revision.

        Args:
            actor_id: Principal currently owning the linked identity.
            platform: Linked platform.
            bot_account_id: Linked bot account.
            principal_id: Linked principal identity.
            expected_revision: Current identity-link revision for CAS.

        Returns:
            The restored isolated default identity binding.

        Raises:
            ContextConflictError: If the revision is stale or selection is ambiguous.
            ContextError: If the actor is not the linked principal.
        """
        actor = _require_key(actor_id, "principal")
        target_principal = _require_key(principal_id, "principal")
        platform = _require_platform(platform)
        account = _require_key(bot_account_id, "bot account")
        now = _iso(self._now())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM identity_links WHERE platform=? AND bot_account_id=?
                   AND principal_id=? AND link_kind='linked'""",
                (platform, account, target_principal),
            ).fetchall()
            if len(rows) != 1:
                if not rows:
                    raise ContextError("NOT_FOUND", "The linked identity was not found.")
                raise ContextConflictError("The target identifies more than one linked identity.")
            row = rows[0]
            if actor != row["principal_id"]:
                raise ContextError("PERMISSION_DENIED", "Only the linked principal can revoke this identity link.")
            if int(row["revision"]) != expected_revision:
                raise ContextConflictError("The identity link revision is stale.")
            next_revision = expected_revision + 1
            connection.execute(
                """UPDATE identity_links SET principal_id=default_principal_id,
                   link_kind='default', revision=?, updated_at=? WHERE platform=?
                   AND bot_account_id=? AND verified_sender_id=? AND revision=?""",
                (next_revision, now, platform, account, row["verified_sender_id"], expected_revision),
            )
            self._audit(
                connection, actor, "identity_link_revoke", "identity",
                f"{platform}/{account}/{hashlib.sha256(row['verified_sender_id'].encode()).hexdigest()[:12]}",
                expected_revision, next_revision, {}, now,
            )
            updated = self._identity_row(connection, platform, account, row["verified_sender_id"])
            connection.commit()
            return _identity_from_row(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def audit_events(self, resource_id: str | None = None) -> tuple[dict[str, object], ...]:
        """Return administrative audit events, optionally filtered by resource ID.

        Args:
            resource_id: Optional exact resource identity to filter by.

        Returns:
            Audit events ordered by creation time and event ID.
        """
        with self.store._connection() as connection:
            if resource_id is None:
                rows = connection.execute(
                    "SELECT * FROM admin_events ORDER BY created_at, event_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM admin_events WHERE resource_id=? ORDER BY created_at, event_id",
                    (resource_id,),
                ).fetchall()
        return tuple({
            "event_id": row["event_id"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "before_revision": row["before_revision"],
            "after_revision": row["after_revision"],
            "details": json.loads(row["details_json"]),
            "created_at": row["created_at"],
        } for row in rows)

    def _mutate_project_membership(
        self,
        actor_id: str,
        project_id: str,
        principal_id: str,
        role: str | None,
        *,
        expected_revision: int,
        action: str,
    ) -> ProjectBinding:
        actor = _require_key(actor_id, "principal")
        project_id = _require_key(project_id, "project id")
        principal = _require_key(principal_id, "principal")
        now = _iso(self._now())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._project_row(connection, project_id)
            self._require_owner(current, actor)
            self._check_revision(current, expected_revision)
            if principal == current["owner_id"]:
                raise ContextError("INVALID_SOURCE", "The project owner cannot be granted or revoked as a member.")
            existing = connection.execute(
                "SELECT revision FROM project_grants WHERE project_id=? AND principal_id=?",
                (project_id, principal),
            ).fetchone()
            if role is None:
                if existing is None:
                    raise ContextError("NOT_FOUND", "The project grant was not found.")
                connection.execute(
                    "DELETE FROM project_grants WHERE project_id=? AND principal_id=?",
                    (project_id, principal),
                )
            else:
                grant_revision = 1 if existing is None else int(existing["revision"]) + 1
                connection.execute(
                    """INSERT INTO project_grants (
                         project_id, principal_id, role, revision, updated_by, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project_id, principal_id) DO UPDATE SET
                         role=excluded.role, revision=excluded.revision,
                         updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                    (project_id, principal, role, grant_revision, actor, now),
                )
            next_revision = expected_revision + 1
            connection.execute(
                "UPDATE projects SET revision=?, updated_at=? WHERE project_id=? AND revision=?",
                (next_revision, now, project_id, expected_revision),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ContextConflictError("The project membership changed during update.")
            self._audit(
                connection, actor, action, "project", project_id,
                expected_revision, next_revision,
                {"principal_id": principal, "role": role}, now,
            )
            updated = self._project_row(connection, project_id)
            connection.commit()
            return _project_from_row(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _project_row(connection: sqlite3.Connection, project_id: str):
        row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise ContextError("NOT_FOUND", "The project binding was not found.")
        return row

    @staticmethod
    def _identity_row(connection: sqlite3.Connection, platform: str, account: str, sender: str):
        return connection.execute(
            """SELECT * FROM identity_links WHERE platform=? AND bot_account_id=?
               AND verified_sender_id=?""",
            (platform, account, sender),
        ).fetchone()

    @staticmethod
    def _require_owner(row, actor: str) -> None:
        if row["owner_id"] != actor:
            raise ContextError("PERMISSION_DENIED", "Only the project owner may change project administration.")

    @staticmethod
    def _check_revision(row, expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ContextError("INVALID_SOURCE", "A positive expected project revision is required.")
        if int(row["revision"]) != expected_revision:
            raise ContextConflictError("The project revision is stale.")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        before_revision: int | None,
        after_revision: int,
        details: dict[str, object],
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO admin_events (
                 event_id, actor_id, action, resource_type, resource_id,
                 before_revision, after_revision, details_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                secrets.token_hex(16), actor, action, resource_type, resource_id,
                before_revision, after_revision,
                json.dumps(details, sort_keys=True, separators=(",", ":")), created_at,
            ),
        )


def _project_from_row(row) -> ProjectBinding:
    return ProjectBinding(
        row["project_id"], row["owner_id"], row["name"], row["bound_path"],
        int(row["revision"]), row["created_at"], row["updated_at"],
    )


def _identity_from_row(row) -> IdentityBinding:
    return IdentityBinding(
        row["platform"], row["bot_account_id"], row["verified_sender_id"],
        row["principal_id"], row["default_principal_id"], row["link_kind"],
        int(row["revision"]),
    )


def _project_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.exists() or not candidate.is_dir():
        raise ContextError("INVALID_SOURCE", "A project binding must point to an existing regular directory.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ContextError("INVALID_SOURCE", "The project directory cannot be resolved.") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise ContextError("INVALID_SOURCE", "A project binding must point to a real directory.")
    return resolved


def _require_key(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_KEY.fullmatch(value):
        raise ContextError("INVALID_SOURCE", f"The {label} identifier is invalid.")
    return value


def _require_platform(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_PLATFORM.fullmatch(value):
        raise ContextError("INVALID_SOURCE", "The platform identifier is invalid.")
    return value


def _default_bot_principal(platform: str, account: str, sender: str) -> str:
    material = "\0".join((platform, account, sender)).encode("utf-8")
    return "bot_" + hashlib.sha256(material).hexdigest()[:32]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
