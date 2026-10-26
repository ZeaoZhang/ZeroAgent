from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from zero_agent.context.admin import ContextAdminService
from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.local_store import LocalContextStore


def test_project_admin_is_owner_only_revisioned_and_audited(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    admin = ContextAdminService(store)
    project_dir = tmp_path / "work project"
    project_dir.mkdir()
    project = admin.create_project("alice", project_dir, name="Work")

    granted = admin.grant_project(
        "alice", project.project_id, "bob", "writer", expected_revision=1,
    )

    assert granted.revision == 2
    assert admin.project_access(project.project_id, "bob").role == "writer"
    assert admin.capabilities_for_project(project.project_id, "bob") == {
        f"project:{project.project_id}:read",
        f"project:{project.project_id}:write",
    }
    with pytest.raises(ContextError) as denied:
        admin.revoke_project("bob", project.project_id, "alice", expected_revision=2)
    assert denied.value.code == "PERMISSION_DENIED"
    with pytest.raises(ContextConflictError, match="stale"):
        admin.revoke_project("alice", project.project_id, "bob", expected_revision=1)

    revoked = admin.revoke_project(
        "alice", project.project_id, "bob", expected_revision=2,
    )
    assert revoked.revision == 3
    assert admin.project_access(project.project_id, "bob") is None
    events = admin.audit_events(project.project_id)
    assert [item["action"] for item in events] == [
        "project_create", "project_grant", "project_revoke",
    ]


def test_project_rebind_changes_only_binding_and_rejects_duplicate_path(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    admin = ContextAdminService(store)
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    rebound_path = tmp_path / "rebound"
    first_path.mkdir()
    second_path.mkdir()
    rebound_path.mkdir()
    first = admin.create_project("alice", first_path)
    other = admin.create_project("alice", second_path)

    rebound = admin.rebind_project(
        "alice", first.project_id, rebound_path, expected_revision=1,
    )
    assert rebound.revision == 2
    assert rebound.bound_path == str(rebound_path.resolve())
    with pytest.raises(ContextConflictError, match="another project"):
        admin.rebind_project(
            "alice", first.project_id, second_path, expected_revision=2,
        )
    with pytest.raises(ContextConflictError, match="stale"):
        admin.rebind_project(
            "alice", first.project_id, first_path, expected_revision=1,
        )
    access = admin.resolve_project_for_path(rebound_path, "alice")
    assert access is not None and access.project.project_id == first.project_id
    assert admin.get_project(other.project_id).bound_path == str(second_path.resolve())


def test_project_owner_transfer_cas_and_directory_validation(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    admin = ContextAdminService(store)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = admin.create_project("alice", project_dir)

    moved = admin.transfer_project_owner(
        "alice", project.project_id, "bob", expected_revision=1,
    )
    assert moved.owner_id == "bob"
    with pytest.raises(ContextError) as denied:
        admin.grant_project("alice", project.project_id, "carol", "reader", expected_revision=2)
    assert denied.value.code == "PERMISSION_DENIED"
    with pytest.raises(ContextError) as invalid:
        admin.create_project("alice", tmp_path / "missing")
    assert invalid.value.code == "INVALID_SOURCE"


def test_session_binding_is_stable_and_cannot_change_principal(tmp_path) -> None:
    admin = ContextAdminService(LocalContextStore(tmp_path / "context"))

    session = admin.bind_session("desktop", "account-1", "host-session-1", "alice")
    assert admin.bind_session("desktop", "account-1", "host-session-1", "alice") == session
    with pytest.raises(ContextConflictError):
        admin.bind_session("desktop", "account-1", "host-session-1", "bob")
    assert admin.bind_session("desktop", "account-1", "host-session-2", "alice") != session


def test_bot_identity_is_keyed_by_verified_platform_account_and_sender(tmp_path) -> None:
    admin = ContextAdminService(LocalContextStore(tmp_path / "context"))

    first = admin.resolve_bot_principal(
        "telegram", "bot-a", "sender-1", verified=True,
    )
    repeated = admin.resolve_bot_principal(
        "telegram", "bot-a", "sender-1", verified=True,
    )
    other_account = admin.resolve_bot_principal(
        "telegram", "bot-b", "sender-1", verified=True,
    )
    assert first == repeated
    assert first != other_account
    with pytest.raises(ContextError) as unverified:
        admin.resolve_bot_principal("telegram", "bot-a", "sender-2", verified=False)
    assert unverified.value.code == "PERMISSION_DENIED"


def test_identity_link_is_private_target_bound_one_time_and_reversible(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    admin = ContextAdminService(store)
    fallback = admin.resolve_bot_principal("telegram", "bot-a", "sender-1", verified=True)
    now = datetime.now(timezone.utc)
    challenge_admin = ContextAdminService(
        store,
        now=lambda: now,
        link_ttl=timedelta(minutes=10),
    )
    challenge = challenge_admin.begin_identity_link("alice", "telegram", "bot-a")

    with store._connection() as connection:
        stored = connection.execute("SELECT * FROM identity_link_tokens").fetchone()
    assert challenge.code not in stored["token_sha256"]
    assert stored["token_sha256"] != challenge.code

    with pytest.raises(ContextError) as group:
        challenge_admin.redeem_identity_link(
            challenge.code,
            platform="telegram",
            bot_account_id="bot-a",
            verified_sender_id="sender-1",
            verified=True,
            is_private_chat=False,
        )
    assert group.value.code == "PERMISSION_DENIED"
    with pytest.raises(ContextError) as wrong_target:
        challenge_admin.redeem_identity_link(
            challenge.code,
            platform="discord",
            bot_account_id="bot-a",
            verified_sender_id="sender-1",
            verified=True,
            is_private_chat=True,
        )
    assert wrong_target.value.code == "PERMISSION_DENIED"
    with pytest.raises(ContextError) as wrong_platform:
        challenge_admin.redeem_identity_link(
            challenge.code,
            platform="telegram",
            bot_account_id="bot-b",
            verified_sender_id="sender-1",
            verified=True,
            is_private_chat=True,
        )
    assert wrong_platform.value.code == "PERMISSION_DENIED"

    linked = challenge_admin.redeem_identity_link(
        challenge.code,
        platform="telegram",
        bot_account_id="bot-a",
        verified_sender_id="sender-1",
        verified=True,
        is_private_chat=True,
    )
    assert linked.principal_id == "alice"
    assert linked.default_principal_id == fallback
    assert admin.resolve_bot_principal("telegram", "bot-a", "sender-1", verified=True) == "alice"
    assert admin.list_identities("alice") == (linked,)

    with pytest.raises(ContextError):
        challenge_admin.redeem_identity_link(
            challenge.code,
            platform="telegram",
            bot_account_id="bot-a",
            verified_sender_id="sender-1",
            verified=True,
            is_private_chat=True,
        )
    unlinked = challenge_admin.revoke_identity_link(
        "alice",
        platform="telegram",
        bot_account_id="bot-a",
        principal_id="alice",
        expected_revision=linked.revision,
    )
    assert unlinked.link_kind == "default"
    assert unlinked.principal_id == fallback


def test_identity_link_expiry_and_redeem_attempts_fail_closed(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    now = datetime.now(timezone.utc)
    admin = ContextAdminService(store, now=lambda: now, link_ttl=timedelta(seconds=1))
    challenge = admin.begin_identity_link("alice", "telegram", "bot-a")
    expired_admin = ContextAdminService(store, now=lambda: now + timedelta(seconds=2))

    with pytest.raises(ContextError, match="expired"):
        expired_admin.redeem_identity_link(
            challenge.code,
            platform="telegram",
            bot_account_id="bot-a",
            verified_sender_id="sender-1",
            verified=True,
            is_private_chat=True,
        )


def test_admin_tables_are_migrated_from_schema_three(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    with sqlite3.connect(store.manifest_path) as connection:
        for table in (
            "admin_events",
            "session_bindings",
            "identity_link_tokens",
            "identity_links",
            "project_grants",
            "projects",
            "context_access_stats",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE IF EXISTS context_access_stats")
        connection.execute("PRAGMA user_version = 3")
    reopened = LocalContextStore(tmp_path / "context")
    with reopened._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_bindings'"
        ).fetchone() is not None
