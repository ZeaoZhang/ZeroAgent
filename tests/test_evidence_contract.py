"""Contract tests for the registry custom-tool evidence declaration.

These tests lock the evidence and control semantics:

- The default anchor prompt produced after a tool dispatch MUST expose the
  latest evidence ``ref`` (so the model can cite it in complete_task).

- A registry tool that declares an eligible ``evidence_kind`` and succeeds
  MUST produce completion-eligible evidence and be completable via
  ``complete_task(evidence_refs=[1])``.

- An undeclared or non-eligible registry tool MUST NOT produce evidence that
  can satisfy completion.

- Genuine custom ``next_prompt``, ``WAIT_FOR_USER``, and memory/control
  semantics are preserved.
"""

from typing import Any, Generator

import pytest

from zero_agent.core.completion import evaluate_completion
from zero_agent.core.handler import BaseHandler
from zero_agent.core.types import (
    EvidenceLedger,
    EvidenceRecord,
    StepAction,
    StepOutcome,
    TaskContract,
    TaskMode,
)
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolDefinition, ToolHandler


def _exhaust(gen: Generator[Any, None, Any]) -> Any:
    """Consume a generator and return its final value."""
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value


def _register(
    handler: BaseHandler,
    name: str,
    handler_fn: ToolHandler,
    **tool_kwargs: Any,
) -> None:
    """Register a custom registry tool on the handler's registry."""
    handler.registry.register(ToolDefinition(
        name=name,
        description="custom tool",
        parameters={"type": "object", "properties": {}},
        handler=handler_fn,
        **tool_kwargs,
    ))


def _executing_contract() -> TaskContract:
    return TaskContract(
        task_id="task-1",
        user_request="do the work",
        mode=TaskMode.EXECUTING,
    )


# ---- default anchor carries the latest ref ----

def test_first_call_anchor_includes_latest_ref(mock_handler: BaseHandler) -> None:
    """首位调用后默认锚点必须包含最新 ref=1 及其工具名."""
    def successful_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "ok\n"
        return {"status": "success", "result": 42}

    _register(mock_handler, "run_custom", successful_tool)
    outcome = _exhaust(mock_handler.dispatch("run_custom", {}, MockResponse()))

    assert outcome.next_prompt is not None
    assert "ref=1" in outcome.next_prompt
    assert "tool=run_custom" in outcome.next_prompt
    assert "recent_evidence: none" not in outcome.next_prompt


def test_second_dispatch_anchor_includes_latest_ref(mock_handler: BaseHandler) -> None:
    """连续调用后默认锚点必须包含最新 ref=2（以及 ref=1）. Semantics:
    anchor exposes the most recent record so the model can cite it."""
    def successful_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "ok\n"
        return {"status": "success", "result": 1}

    _register(mock_handler, "custom_a", successful_tool)
    _register(mock_handler, "custom_b", successful_tool)
    _exhaust(mock_handler.dispatch("custom_a", {}, MockResponse()))
    outcome = _exhaust(mock_handler.dispatch("custom_b", {}, MockResponse()))

    assert outcome.next_prompt is not None
    assert "ref=1" in outcome.next_prompt
    assert "ref=2" in outcome.next_prompt
    assert "tool=custom_b" in outcome.next_prompt

def test_declared_execute_plain_result_is_successful_evidence(
    mock_handler: BaseHandler,
) -> None:
    def successful_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "running\n"
        return {"result": 42}

    _register(
        mock_handler,
        "run_custom",
        successful_tool,
        evidence_kind="execute",
    )
    _exhaust(mock_handler.dispatch("run_custom", {}, MockResponse()))

    record = mock_handler.evidence_ledger.records[0]
    assert record.kind == "execute"
    assert record.status == "success"

    completion = _exhaust(mock_handler.dispatch(
        "complete_task",
        {"answer": "done", "evidence_refs": [1]},
        MockResponse(),
    ))

    assert completion.action is StepAction.REQUEST_COMPLETION
    assert mock_handler.completion_certificate is not None


# ---- wrong evidence can never satisfy completion ----

def test_completion_rejects_non_eligible_custom_record(
    mock_handler: BaseHandler,
) -> None:
    """自定义工具记录（kind 未声明，回退 non-eligible）不能用作完成证据."""
    def undeclared_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "ok\n"
        return {"result": 42}

    _register(mock_handler, "run_custom", undeclared_tool)
    # Keep the task in EXECUTING to force evidence-kind validation.
    mock_handler.task_contract = TaskContract(
        task_id="task-1",
        user_request="do the work",
        mode=TaskMode.EXECUTING,
    )
    _exhaust(mock_handler.dispatch("run_custom", {}, MockResponse()))

    record = mock_handler.evidence_ledger.records[0]
    assert record.kind == "system"
    completion = _exhaust(mock_handler.dispatch(
        "complete_task",
        {"answer": "done", "evidence_refs": [1]},
        MockResponse(),
    ))

    assert completion.action is not StepAction.REQUEST_COMPLETION
    assert mock_handler.completion_certificate is None
    assert "read/write/execute/web/verify" in (completion.next_prompt or "")


def test_completion_rejects_error_evidence() -> None:
    """错误状态的可选证据也不能满足完成."""
    ledger = EvidenceLedger(records=[
        EvidenceRecord(1, "code_run", "error", "execute", "tests failed"),
    ])

    cert, prompt = evaluate_completion(
        _executing_contract(),
        ledger,
        MockResponse(content="done"),
        evidence_refs=[1],
        plan_remaining=None,
        plan_verify_status="missing",
    )

    assert cert is None
    assert prompt is not None
    assert "contains an error" in prompt

@pytest.mark.parametrize(
    ("payload_status", "expected_status"),
    (("error", "error"), ("INTERRUPT", "interrupt")),
)
def test_declared_execute_explicit_failure_is_not_completion_eligible(
    mock_handler: BaseHandler,
    payload_status: str,
    expected_status: str,
) -> None:
    """声明 execute 的显式失败状态不能作为完成证据."""
    def failing_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, str]]:
        yield "failed\n"
        return {"status": payload_status}

    _register(
        mock_handler,
        "run_failing",
        failing_tool,
        evidence_kind="execute",
    )
    mock_handler.task_contract = _executing_contract()
    _exhaust(mock_handler.dispatch("run_failing", {}, MockResponse()))

    record = mock_handler.evidence_ledger.records[0]
    assert record.kind == "execute"
    assert record.status == expected_status

    completion = _exhaust(mock_handler.dispatch(
        "complete_task",
        {"answer": "done", "evidence_refs": [1]},
        MockResponse(),
    ))

    assert completion.action is not StepAction.REQUEST_COMPLETION
    assert mock_handler.completion_certificate is None


# ---- safe strategy A: no un-completable EXECUTING ----

def test_undeclared_promoting_custom_tool_must_stay_completable(
    mock_handler: BaseHandler,
) -> None:
    """一个未声明 evidence_kind、promotes_task_state=True 的自定义工具
    运行后，任务必须仍可完成：要么停留在 OPEN（纯回答即可完成），要么其记录
    可被 complete_task 的成功证据引用."""
    def undeclared_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "ok\n"
        return {"result": 42}

    _register(mock_handler, "run_custom", undeclared_tool)  # default promotes_task_state=True, no evidence_kind

    if mock_handler.task_contract.mode is TaskMode.OPEN:
        # OPEN: a plain final answer completes without evidence refs.
        cert, _prompt = evaluate_completion(
            mock_handler.task_contract,
            mock_handler.evidence_ledger,
            MockResponse(content="final answer"),
            plan_remaining=None,
            plan_verify_status="missing",
        )
        assert cert is not None
    else:
        # EXECUTING: its evidence must be completable via explicit refs.
        completion = _exhaust(mock_handler.dispatch(
            "complete_task",
            {"answer": "done", "evidence_refs": [1]},
            MockResponse(),
        ))
        assert completion.action is StepAction.REQUEST_COMPLETION
        assert mock_handler.completion_certificate is not None


# ---- safe strategy B: explicit evidence_kind='execute' completes ----

def test_explicit_execute_evidence_kind_completes(
    mock_handler: BaseHandler,
) -> None:
    """显式声明 evidence_kind='execute' 且成功的自定义工具必须产生可完成证据，
    并能通过 complete_task(evidence_refs=[1]) 完成."""
    def successful_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "running\n"
        return {"status": "success", "result": 42}

    _register(
        mock_handler,
        "run_custom",
        successful_tool,
        evidence_kind="execute",
        promotes_task_state=True,
    )
    _exhaust(mock_handler.dispatch("run_custom", {}, MockResponse()))

    record = mock_handler.evidence_ledger.records[0]
    assert record.kind == "execute"
    assert record.status == "success"

    completion = _exhaust(mock_handler.dispatch(
        "complete_task",
        {"answer": "done", "evidence_refs": [1]},
        MockResponse(),
    ))

    assert completion.action is StepAction.REQUEST_COMPLETION
    assert mock_handler.completion_certificate is not None

def test_non_completion_evidence_kinds_do_not_promote_open_task(
    mock_handler: BaseHandler,
) -> None:
    """memory/user/system contracts cannot strand an OPEN task."""
    def non_completion_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "ok\n"
        return {"status": "success"}

    for kind in ("memory", "user", "system"):
        _register(
            mock_handler,
            f"custom_{kind}",
            non_completion_tool,
            evidence_kind=kind,
            promotes_task_state=True,
        )
        _exhaust(mock_handler.dispatch(f"custom_{kind}", {}, MockResponse()))
        assert mock_handler.task_contract.mode is TaskMode.OPEN


# ---- genuine custom next_prompt & WAIT_FOR_USER preserved ----

def test_custom_next_prompt_preserved(mock_handler: BaseHandler) -> None:
    """注册工具通过 _za_next_prompt 提供的自定义 next_prompt 不能被默认锚点覆盖."""
    def custom_prompt_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, dict[str, Any]]:
        yield "ok\n"
        return {"result": 42, "_za_next_prompt": "keep-this-prompt"}

    _register(mock_handler, "run_custom", custom_prompt_tool)
    outcome = _exhaust(mock_handler.dispatch("run_custom", {}, MockResponse()))

    assert outcome.next_prompt == "keep-this-prompt"


def test_step_outcome_custom_prompt_and_wait_preserved(
    mock_handler: BaseHandler,
) -> None:
    """返回 StepOutcome 的自定义工具保留其 next_prompt 与 WAIT_FOR_USER 控制."""
    def waiting_tool(
        args: dict[str, Any],
        _resp: Any,
        _handler: BaseHandler,
    ) -> Generator[str, None, StepOutcome]:
        yield "ok\n"
        return StepOutcome(
            {"status": "interrupt"},
            next_prompt="custom wait prompt",
            action=StepAction.WAIT_FOR_USER,
        )

    _register(mock_handler, "wait_custom", waiting_tool)
    outcome = _exhaust(mock_handler.dispatch("wait_custom", {}, MockResponse()))

    assert outcome.data == {"status": "interrupt"}
    assert outcome.next_prompt == "custom wait prompt"
    assert outcome.action is StepAction.WAIT_FOR_USER


# ---- memory / control semantics preserved ----

def test_memory_kind_evidence_not_completion_eligible() -> None:
    """memory/control 记录（如 update_working_checkpoint）不能作为完成证据."""
    ledger = EvidenceLedger(records=[
        EvidenceRecord(1, "update_working_checkpoint", "unknown", "memory", "saved ckpt"),
    ])

    cert, prompt = evaluate_completion(
        _executing_contract(),
        ledger,
        MockResponse(content="done"),
        evidence_refs=[1],
        plan_remaining=None,
        plan_verify_status="missing",
    )

    assert cert is None
    assert prompt is not None
    assert "read/write/execute/web/verify" in prompt