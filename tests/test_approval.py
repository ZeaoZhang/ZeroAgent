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
            "    @ctx.tool(name='danger', description='d',\n"
            "              parameters={'type': 'object', 'properties': {}},\n"
            "              evidence_kind='execute')\n"
            "    def danger(args):\n"
            "        return 'executed'\n"
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

        for args in (
            {"code": "rm -rf /"},
            {"script": "rm -rf /"},
        ):
            verdict = runtime.evaluate_tool_call("code_run", args)
            assert verdict.is_block, args

    def test_script_path_content_is_inspected(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        script = tmp_path / "ws" / "danger.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("rm -rf /\n", encoding="utf-8")

        verdict = runtime.evaluate_tool_call(
            "code_run", {"script_path": str(script)}
        )

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
        # 生产路径中 handler 会带上当前任务的 task_id，通行证绑定该任务消费
        assert runtime.evaluate_tool_call("danger", retry, task_id="t1").is_allow
        # 缺任务标识的调用不应消费它（避免跨任务复用）
        assert runtime.evaluate_tool_call("danger", retry).needs_approval

    def test_scope_from_pending_is_granted(self, tmp_path: Path) -> None:
        from zero_agent.core.agent import ZeroAgent

        runtime = _runtime(tmp_path)
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
                    "scope": "danger:/tmp/x",
                    "tool_name": "danger",
                    "reason": "sure?",
                }
            },
        )

        class Stub:
            extensions = runtime
            _is_approval_answer = ZeroAgent._is_approval_answer

        ZeroAgent._approval_continuation(Stub(), pending, "批准")

        # 批量授权按 scope 生效：不同参数、同任务、同 scope 也应放行
        assert runtime.consume_approval(
            approval_fingerprint("danger", {"x": 2}),
            task_id="t1",
            scope="danger:/tmp/x",
        )

    def test_rejected_answer_clears_tokens(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        retry = {"x": 1}
        runtime.grant_approval(approval_fingerprint("danger", retry))

        text = self._agent_with_pending(runtime, "拒绝")

        assert "没有批准" in text
        assert runtime.evaluate_tool_call("danger", retry).needs_approval

    def test_unrecognized_answer_is_treated_as_reject(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)

        text = self._agent_with_pending(runtime, "再说吧")

        assert "没有批准" in text
        assert "再说吧" in text  # 用户答复必须回传给模型

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


class TestHardening:
    """覆盖审阅指出的边界：任务作用域、缺指纹、否定词、内容摘要、路径口径."""

    def test_approval_scoped_to_task(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        args = {"x": 1}
        fingerprint = runtime.evaluate_tool_call(
            "danger", args, task_id="t1"
        ).fingerprint
        runtime.grant_approval(fingerprint, task_id="t1")

        # 同一任务放行
        assert runtime.evaluate_tool_call("danger", args, task_id="t1").is_allow
        # 换任务重新审批
        assert runtime.evaluate_tool_call("danger", args, task_id="t2").needs_approval

    def test_missing_fingerprint_is_not_approved(
        self, tmp_path: Path
    ) -> None:
        from zero_agent.core.agent import ZeroAgent

        runtime = _ask_plugin(tmp_path)
        pending = PendingTaskState(
            contract=TaskContract(
                task_id="t1", user_request="do", mode=TaskMode.OPEN
            ),
            ledger=EvidenceLedger(),
            plan_verify_status="missing",
            waiting_kind="approval",
            waiting_data={"approval": {"tool_name": "danger"}},
        )

        class Stub:
            extensions = runtime
            _is_approval_answer = ZeroAgent._is_approval_answer

        text = ZeroAgent._approval_continuation(Stub(), pending, "批准")

        assert "失效" in text
        assert runtime.evaluate_tool_call("danger", {"x": 1}).needs_approval

    @pytest.mark.parametrize(
        "answer",
        ["不同意", "批准吧", "OK！", "no", "算了"],
    )
    def test_non_exact_answers_are_rejected(
        self, tmp_path: Path, answer: str
    ) -> None:
        from zero_agent.core.agent import ZeroAgent

        runtime = _ask_plugin(tmp_path)
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
                }
            },
        )

        class Stub:
            extensions = runtime
            _is_approval_answer = ZeroAgent._is_approval_answer

        text = ZeroAgent._approval_continuation(Stub(), pending, answer)

        assert "没有批准" in text
        assert runtime.evaluate_tool_call("danger", {"x": 1}).needs_approval

    def test_approval_prompt_shows_content_digest(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        target = str(Path(runtime.home) / "skills" / "demo" / "SKILL.md")

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "hello world"}
        )

        assert verdict.needs_approval
        assert "sha256" in verdict.reason
        assert "11 字符" in verdict.reason

    def test_memory_prefixed_path_uses_file_tool_resolution(
        self, tmp_path: Path
    ) -> None:
        from types import SimpleNamespace

        memory_dir = tmp_path / "home" / "mem"
        config = SimpleNamespace(
            memory_dir=str(memory_dir),
            workspace_dir=str(tmp_path / "ws"),
            extensions_protected_writes=[],
        )
        runtime = ExtensionRuntime(
            registry=_registry(),
            home=tmp_path / "home",
            config=config,
        )
        permission_gate.setup(ExtensionContext("permission_gate", runtime))

        # 首段等于 memory_dir 目录名时，file 工具会重定向到记忆目录
        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": "mem/SKILL.md", "content": "x"}
        )

        assert verdict.needs_approval


class TestScopeApproval:
    """批量授权：同任务 + 同 scope 的重复审批合并（治理审批疲劳）."""

    def _gate_runtime(self, tmp_path: Path) -> ExtensionRuntime:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        return runtime

    def _protected_target(self, runtime: ExtensionRuntime) -> str:
        return str(Path(runtime.home) / "skills" / "demo" / "SKILL.md")

    def test_same_path_changed_content_is_merged(self, tmp_path: Path) -> None:
        runtime = self._gate_runtime(tmp_path)
        target = self._protected_target(runtime)

        first = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "demo\n"},
        )
        assert first.needs_approval
        assert first.scope, "受保护写入必须给出批量授权键"

        runtime.grant_approval(first.fingerprint, task_id="t1", scope=first.scope)

        # 内容只差一个换行：模型重发时不该再弹一次审批
        second = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "demo"},
            task_id="t1",
        )
        assert second.is_allow

    def test_other_path_still_asks(self, tmp_path: Path) -> None:
        runtime = self._gate_runtime(tmp_path)
        target = self._protected_target(runtime)

        first = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "a"},
        )
        runtime.grant_approval(first.fingerprint, task_id="t1", scope=first.scope)

        other = str(Path(runtime.home) / "skills" / "other" / "SKILL.md")
        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": other, "content": "a"}, task_id="t1",
        )
        assert verdict.needs_approval

    def test_scope_grant_is_task_bound(self, tmp_path: Path) -> None:
        runtime = self._gate_runtime(tmp_path)
        target = self._protected_target(runtime)

        first = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "a"},
        )
        runtime.grant_approval(first.fingerprint, task_id="t1", scope=first.scope)

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "b"}, task_id="t2",
        )
        assert verdict.needs_approval

    def test_scope_grant_expires(self, tmp_path: Path) -> None:
        runtime = self._gate_runtime(tmp_path)
        target = self._protected_target(runtime)

        first = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "a"},
        )
        runtime.grant_approval(
            first.fingerprint, ttl=0.0, task_id="t1", scope=first.scope,
        )

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "b"}, task_id="t1",
        )
        assert verdict.needs_approval

    def test_clear_approvals_drops_scope(self, tmp_path: Path) -> None:
        runtime = self._gate_runtime(tmp_path)
        target = self._protected_target(runtime)

        first = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "a"},
        )
        runtime.grant_approval(first.fingerprint, task_id="t1", scope=first.scope)
        runtime.clear_approvals()

        verdict = runtime.evaluate_tool_call(
            "file_write", {"path": target, "content": "b"}, task_id="t1",
        )
        assert verdict.needs_approval

    def test_no_scope_keeps_single_approval(self, tmp_path: Path) -> None:
        runtime = _ask_plugin(tmp_path)
        first = runtime.evaluate_tool_call("danger", {"x": 1})
        assert first.needs_approval
        assert first.scope == ""
        runtime.grant_approval(first.fingerprint, task_id="t1")

        # 没有批量授权键：参数一变就必须重新批准
        assert runtime.evaluate_tool_call("danger", {"x": 2}).needs_approval


class TestScopeFlowThroughHandler:
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

    def test_waiting_payload_carries_scope(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        handler = self._handler(runtime)
        target = str(Path(runtime.home) / "skills" / "demo" / "SKILL.md")

        paused = self._dispatch(handler, "file_write", {"path": target, "content": "a"})

        assert paused.action is StepAction.WAIT_FOR_USER
        assert paused.data["approval"]["scope"] == f"file_write:{target}"

    def test_granted_scope_covers_changed_args(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        permission_gate.setup(ExtensionContext("permission_gate", runtime))
        handler = self._handler(runtime)
        target = str(Path(runtime.home) / "skills" / "demo" / "SKILL.md")

        paused = self._dispatch(handler, "file_write", {"path": target, "content": "a"})
        runtime.grant_approval(
            paused.data["approval"]["fingerprint"],
            task_id="t1",
            scope=paused.data["approval"]["scope"],
        )

        outcome = self._dispatch(
            handler, "file_write", {"path": target, "content": "a\n"},
        )
        assert outcome.action is not StepAction.WAIT_FOR_USER
