"""Trusted entry-point binding helpers for local and bot sessions."""

from __future__ import annotations

import hashlib
from pathlib import Path

from zero_agent.context.admin import ContextAdminService
from zero_agent.context.errors import ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.types import RuntimeContext
from zero_agent.core.config import AgentConfig


def local_runtime_context(
    config: AgentConfig,
    *,
    entry_type: str,
    host_session_key: str,
    workspace_path: str | Path | None = None,
    channel_scope: str = "local:private",
) -> RuntimeContext:
    """Bind a local app session to its configured principal and optional project."""

    principal = config.context.local_user_id
    admin = ContextAdminService(LocalContextStore(config.context.root))
    session_id = admin.bind_session(entry_type, "local", host_session_key, principal)
    project_access = None
    if workspace_path is not None:
        project_access = admin.resolve_project_for_path(workspace_path, principal)
    project_id = project_access.project.project_id if project_access else None
    capabilities = set(admin.capabilities_for_project(project_id, principal) if project_id else ())
    capabilities.update(config.context.skill_capabilities)
    return RuntimeContext(
        account_id="local",
        user_id=principal,
        session_id=session_id,
        project_id=project_id,
        channel_scope=channel_scope,
        capabilities=frozenset(capabilities),
    )


def bot_runtime_context(
    config: AgentConfig,
    *,
    platform: str,
    bot_account_id: str,
    verified_sender_id: str,
    host_session_key: str,
    is_private_chat: bool,
    is_group_chat: bool,
    sender_verified: bool,
) -> RuntimeContext:
    """Resolve a bot principal only from platform-verified account and sender IDs."""

    if not sender_verified or not verified_sender_id:
        raise ContextError("PERMISSION_DENIED", "The bot entry has no verified sender identity.")
    if is_private_chat and is_group_chat:
        raise ContextError("INVALID_SOURCE", "A bot chat cannot be both private and group scoped.")
    admin = ContextAdminService(LocalContextStore(config.context.root))
    principal = admin.resolve_bot_principal(
        platform,
        bot_account_id,
        verified_sender_id,
        verified=True,
    )
    binding_key = f"{host_session_key}:{_identifier_hash(verified_sender_id)}"
    session_id = admin.bind_session(
        f"bot_{platform}",
        bot_account_id,
        binding_key,
        principal,
    )
    channel_kind = "group" if is_group_chat else "private" if is_private_chat else "session"
    channel_scope = f"{platform}:{channel_kind}:{_identifier_hash(host_session_key)}"
    return RuntimeContext(
        account_id=f"{platform}:{bot_account_id}",
        user_id=principal,
        session_id=session_id,
        channel_scope=channel_scope,
        capabilities=frozenset(config.context.skill_capabilities),
    )


def _identifier_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
