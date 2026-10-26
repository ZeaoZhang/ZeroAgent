"""Context runtime wiring at the ZeroAgent task boundary."""

import pytest

from zero_agent.context.config import ContextConfig
from zero_agent.context.errors import ContextError
from zero_agent.context.types import RuntimeContext
from zero_agent.context.admin import ContextAdminService
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.runtime import bot_runtime_context, local_runtime_context
from zero_agent.core.types import EvidenceLedger, PendingTaskState
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.types import TaskContract, TaskMode
from zero_agent.tools.registry import ToolRegistry


def _config(tmp_path, *, mode="local_v1"):
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
        context=ContextConfig(mode=mode, root=str(tmp_path / "context-data")),
    )


def test_local_mode_registers_context_tools_and_memory_settlement_compatibility(tmp_path) -> None:
    registry = ToolRegistry.with_builtins(_config(tmp_path))

    names = {tool.name for tool in registry.list_all()}
    assert {"context_read", "context_list", "context_search", "context_sources", "memory_update"} <= names
    sources_tool = registry.get("context_sources")
    assert sources_tool is not None
    assert sources_tool.category == "context" and sources_tool.evidence_kind == "read"
    assert sources_tool.promotes_task_state is False and sources_tool.skill_action is None
    memory_tool = registry.get("memory_update")
    assert memory_tool is not None and "context_sources" in memory_tool.description


def test_local_long_term_settlement_compatibility_tool_routes_to_memory_update(tmp_path) -> None:
    config = _config(tmp_path)
    registry = ToolRegistry.with_builtins(config)
    tool = registry.get("start_long_term_update")
    assert tool is not None

    generator = tool.handler({}, None, object())
    progress = []
    try:
        while True:
            progress.append(next(generator))
    except StopIteration as stopped:
        outcome = stopped.value

    assert "memory_update" in "".join(progress)
    assert outcome.data["code"] == "MEMORY_UPDATE_REQUIRES_SERVICE"
    assert not (tmp_path / "legacy-memory").exists()


def test_agent_binds_user_receipts_and_tool_evidence_to_entry_identity(tmp_path) -> None:
    agent = ZeroAgent(config=_config(tmp_path))
    runtime = RuntimeContext("local", "alice", "session-1")
    agent.handler.task_contract = TaskContract(
        task_id="task-1",
        user_request="",
        mode=TaskMode.OPEN,
    )

    agent._initialize_context_runtime(runtime, "请记住，以后回答简洁一些。")
    agent.handler._record_evidence(
        "code_run",
        {"type": "python"},
        {"status": "success", "exit_code": 0},
    )

    record = agent.handler.evidence_ledger.records[-1]
    assert record.data_ref
    receipt = agent.handler.context_service.store.get_evidence_receipt(record.data_ref)
    assert receipt is not None
    assert receipt.tool_name == "code_run"
    assert receipt.result_fields == {"exit_code": 0}
    assert receipt.user_id == "alice"


def test_local_mode_requires_entry_supplied_runtime_identity(tmp_path) -> None:
    agent = ZeroAgent(config=_config(tmp_path))

    with pytest.raises(ContextError) as error:
        agent._initialize_context_runtime(None, "remember this")

    assert error.value.code == "PERMISSION_DENIED"


def test_external_identity_factory_missing_fails_before_task_execution(tmp_path) -> None:
    agent = ZeroAgent(config=_config(tmp_path))
    agent.entry_runtime_context_factory = lambda: None

    with pytest.raises(ContextError) as error:
        next(agent.run("should not start"))

    assert error.value.code == "PERMISSION_DENIED"
    assert agent._is_running_task is False
    assert agent._pending_task_state is None


def test_pending_task_resume_rejects_cross_identity_before_task_execution(tmp_path) -> None:
    agent = ZeroAgent(config=_config(tmp_path))
    original = RuntimeContext("local", "alice", "session-original")
    other = RuntimeContext("local", "bob", "session-other")
    agent._pending_task_state = PendingTaskState(
        contract=TaskContract("waiting", "original", TaskMode.OPEN),
        ledger=EvidenceLedger(),
        plan_verify_status="missing",
        waiting_kind="ask_user",
        runtime_context=original,
    )
    agent.entry_runtime_context_factory = lambda: other

    with pytest.raises(ContextError) as error:
        next(agent.run("answer"))

    assert error.value.code == "PERMISSION_DENIED"
    assert agent._pending_task_state is not None
    assert agent._is_running_task is False

def test_local_runtime_context_persists_session_and_resolves_project_grants(tmp_path) -> None:
    config = _config(tmp_path)
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    admin = ContextAdminService(LocalContextStore(config.context.root))
    project = admin.create_project("local", project_dir)
    admin.grant_project(project.owner_id, project.project_id, "member", "writer", expected_revision=1)

    owner_first = local_runtime_context(
        config,
        entry_type="desktop",
        host_session_key="session-a",
        workspace_path=project_dir,
    )
    owner_repeat = local_runtime_context(
        config,
        entry_type="desktop",
        host_session_key="session-a",
        workspace_path=project_dir,
    )
    member_config = _config(tmp_path)
    member_config.context.local_user_id = "member"
    member = local_runtime_context(
        member_config,
        entry_type="desktop",
        host_session_key="session-b",
        workspace_path=project_dir,
    )

    assert owner_first is not None and owner_repeat is not None and member is not None
    assert owner_first.session_id == owner_repeat.session_id
    assert owner_first.project_id == project.project_id
    assert f"project:{project.project_id}:write" in owner_first.capabilities
    assert member.user_id == "member"
    assert f"project:{project.project_id}:write" in member.capabilities


def test_bot_runtime_context_requires_verified_sender_and_separates_group_writes(tmp_path) -> None:
    config = _config(tmp_path)
    direct = bot_runtime_context(
        config,
        platform="telegram",
        bot_account_id="bot-1",
        verified_sender_id="sender-1",
        host_session_key="chat-1",
        is_private_chat=True,
        is_group_chat=False,
        sender_verified=True,
    )
    group = bot_runtime_context(
        config,
        platform="telegram",
        bot_account_id="bot-1",
        verified_sender_id="sender-1",
        host_session_key="group-1",
        is_private_chat=False,
        is_group_chat=True,
        sender_verified=True,
    )

    assert direct is not None and group is not None
    assert direct.user_id == group.user_id
    assert direct.session_id != group.session_id
    assert "group" in (group.channel_scope or "").split(":")
    with pytest.raises(ContextError) as error:
        bot_runtime_context(
            config,
            platform="telegram",
            bot_account_id="bot-1",
            verified_sender_id="sender-1",
            host_session_key="chat-1",
            is_private_chat=True,
            is_group_chat=False,
            sender_verified=False,
        )
    assert error.value.code == "PERMISSION_DENIED"
