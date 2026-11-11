"""阶段 3 测试：审批门控、一次性通行证与恢复续跑."""

from __future__ import annotations

from pathlib import Path

import pytest

from zero_agent.core.types import (
    EvidenceLedger,
    PendingTaskState,
    StepAction,
    TaskContract,
    TaskMode,
)
from zero_agent.extensions.builtin import permission_gate
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.events import ApprovalRequired, ToolCallVerdict
from zero_agent.extensions.runtime import ExtensionRuntime, approval_fingerprint
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _registry() -> ToolRegistry:
    return ToolRegistry()


def _runtime(home: Path, registry: ToolRegistry | None = None) -> ExtensionRuntime:
    return ExtensionRuntime(registry=registry or _registry(), home=home)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ask_plugin(tmp_path: Path) -> ExtensionRuntime:
    _write(
        tmp_path / "extensions" / "ask.py",
        (
            "def setup(ctx):\n"
            "    @ctx.on_tool_call\n"
            "    def guard(call):\n"
            "        if call.tool_name == 'danger':\n"
            "            return ctx.confirm('确认要执行 danger 吗？')\n"
        ),
    )
    runtime = _runtime(tmp_path)
    runtime.load_all()
    return runtime


class TestFingerprint:
    def test_same_args_same_fingerprint(self) -> None:
        assert approval_fingerprint("t", {"a": 1, "b": 2}) == approval_fingerprint(
            "t", {"b": 2, "a": 1}
        )

    def test_internal_keys_do_not_matter(self) -> None:
        assert approval_fingerprint("t", {"a": 1}) == approval_fingerprint(
            "t", {"a": 1, "_index": 3, "_tool_num": 1}
        )

    def test_changed_content_changes_fingerprint(self) -> None:
        first = approval_fingerprint("file_patch", {"path": "/x", "new": "v1"})
        second = approval_fingerprint("file_patch", {"path": "/x", "new": "v2"})
        assert first != second


class TestEvaluate:
    def test_confirm_yields_approve_verdict(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)

        verdict = runtime.evaluate_tool_call("danger", {"x": 1})

        assert isinstance(verdict, ToolCallVerdict)
        assert verdict.needs_approval
        assert verdict.fingerprint
        assert "确认" in verdict.reason

    def test_granted_approval_allows_same_call_once(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        args = {"x": 1}
        fingerprint = runtime.evaluate_tool_call("danger", args).fingerprint

        runtime.grant_approval(fingerprint)
        assert runtime.evaluate_tool_call("danger", args).is_allow
        # 一次性：再次调用仍需审批
        assert runtime.evaluate_tool_call("danger", args).needs_approval

    def test_approval_is_bound_to_args(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        fingerprint = runtime.evaluate_tool_call("danger", {"x": 1}).fingerprint
        runtime.grant_approval(fingerprint)

        assert runtime.evaluate_tool_call("danger", {"x": 2}).needs_approval

    def test_other_tools_unaffected(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        assert runtime.evaluate_tool_call("safe", {}).is_allow

    def test_expired_approval_is_rejected(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        fingerprint = runtime.evaluate_tool_call("danger", {"x": 1}).fingerprint
        runtime.grant_approval(fingerprint, ttl=-1)

        assert runtime.evaluate_tool_call("danger", {"x": 1}).needs_approval


class TestHandlerIntegration:
    def _handler(self, runtime: ExtensionRuntime):
        from zero_agent.core.handler import BaseHandler

        class Parent:
            def __init__(self) -> None:
                self.extensions = runtime
                self.loop = None
                self.config = None

        handler = BaseHandler(registry=runtime.registry, cwd=".")
        handler.parent = Parent()
        handler.task_contract = TaskContract(
            task_id="t1", user_request="do", mode=TaskMode.OPEN
        )
        return handler

    def _dispatch(self, handler, tool_name: str, args: dict):
        generator = handler.dispatch(tool_name, args, None)
        try:
            while True:
                next(generator)
        except StopIteration as stop:
            return stop.value

    def test_approval_pauses_with_wait_for_user(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        handler = self._handler(runtime)

        outcome = self._dispatch(handler, "danger", {"x": 1})

        assert outcome is not None
        assert outcome.action is StepAction.WAIT_FOR_USER
        assert outcome.data["approval"]["fingerprint"]
        assert outcome.data["data"]["candidates"] == ["批准", "拒绝"]
        # 暂停不应推进任务状态
        assert handler.task_contract.mode is TaskMode.OPEN

    def test_approved_call_executes_on_retry(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "extensions" / "ask.py",
            (
                "def setup(ctx):\n"
                "    @ctx.on_tool_call\n"
                "    def guard(call):\n"
                "        if call.tool_name == 'danger':\n"
                "            return ctx.confirm('sure?')\n"
                "    @ctx.tool(name='danger', description='d', parameters={'type': 'object', 'properties': {}})\n"
                "    def danger(args):\n"
                "        return 'executed'\n"
            ),
        )
        registry = _registry()
        runtime = _runtime(tmp_path, registry)
        runtime.load_all()
        handler = self._handler(runtime)

        paused = self._dispatch(handler, "danger", {"x": 1})
        fingerprint = paused.data["approval"]["fingerprint"]
        runtime.grant_approval(fingerprint)

        outcome = self._dispatch(handler, "danger", {"x": 1})
        assert outcome.data == "executed"


class TestPermissionGateApproval:
    def test_protected_write_asks_instead_of_blocking(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        target = str(Path(runtime.home) / "skills" / "demo" / "SKILL.md")

        verdict = runtime.evaluate_tool_call("file_write", {"path": target})

        assert verdict.needs_approval
        assert "是否允许" in verdict.reason

    def test_dangerous_shell_is_still_blocked(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))

        verdict = runtime.evaluate_tool_call("code_run", {"code": "rm -rf /"})

        assert verdict.is_block


class TestRecovery:
    def _agent_with_pending(self, runtime: ExtensionRuntime, answer: str) -> str:
        from zero_agent.core.agent import ZeroAgent

        pending = PendingTaskState(
            contract=TaskContract(
                task_id="t1", user_request="do", mode=TaskMode.OPEN
            ),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="approval",
            waiting_data={
                "approval": {
                    "fingerprint": approval_fingerprint("danger", {"x": 1}),
                    "tool_name": "danger",
                    "reason": "sure?",
                }
            },
        )

        class Stub:
            extensions = runtime
            _is_approval_answer = ZeroAgent._is_approval_answer

        return ZeroAgent._approval_continuation(Stub(), pending, answer)

    def test_approved_answer_grants_token(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        retry = {"x": 1}
        fingerprint = approval_fingerprint("danger", retry)

        text = self._agent_with_pending(runtime, "批准")

        assert "已批准" in text
        assert runtime.evaluate_tool_call("danger", retry).is_allow

    def test_rejected_answer_clears_tokens(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        retry = {"x": 1}
        runtime.grant_approval(approval_fingerprint("danger", retry))

        text = self._agent_with_pending(runtime, "拒绝")

        assert "拒绝" in text
        assert runtime.evaluate_tool_call("danger", retry).needs_approval

    def test_unrecognized_answer_is_treated_as_reject(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)

        text = self._agent_with_pending(runtime, "再说吧")

        assert "拒绝" in text

    def test_approval_waiting_kind_detected(self) -> None:
        from zero_agent.core.agent import ZeroAgent
        from zero_agent.core.types import TerminalEvent, TerminalStatus

        class Stub:
            handler = type(
                "H",
                (),
                {
                    "task_contract": TaskContract(
                        task_id="t", user_request="u", mode=TaskMode.OPEN
                    ),
                    "evidence_ledger": EvidenceLedger(),
                    "plan_verify_status": "missing",
                },
            )()

            def clear_pending_task(self) -> None:
                self._pending_task_state = None

            _pending_task_state = None

        stub = Stub()
        terminal = TerminalEvent(
            status=TerminalStatus.WAITING,
            data={
                "status": "INTERRUPT",
                "data": {"question": "ok?", "candidates": ["批准", "拒绝"]},
                "approval": {"fingerprint": "abc", "tool_name": "danger"},
            },
        )

        ZeroAgent._update_pending_task_state(stub, terminal)

        assert stub._pending_task_state.waiting_kind == "approval"
