"""Tests for core/types.py terminal and step control models."""

from zero_agent.core.types import (
    CompletionCertificate,
    EvidenceLedger,
    EvidenceRecord,
    PendingTaskState,
    StepAction,
    StepOutcome,
    TaskContract,
    TaskMode,
    TerminalEvent,
    TerminalStatus,
    TurnResult,
)



class TestTaskEvidenceModels:
    def test_contract_ledger_and_pending_state(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            user_request="write a file",
            mode=TaskMode.EXECUTION,
        )
        record = EvidenceRecord(
            turn=1,
            tool_name="file_write",
            status="success",
            kind="write",
            summary="wrote out.txt",
            data_ref="out.txt",
        )
        ledger = EvidenceLedger(records=[record])
        pending = PendingTaskState(
            contract=contract,
            ledger=ledger,
            plan_verify_status="missing",
            waiting_kind="ask_user",
            waiting_data={"question": "continue?"},
        )

        assert pending.contract.mode is TaskMode.EXECUTION
        assert pending.ledger.records[0].status == "success"
        assert pending.waiting_data == {"question": "continue?"}

class TestStepOutcome:
    def test_defaults_to_continue(self) -> None:
        outcome = StepOutcome(data="test")
        assert outcome.data == "test"
        assert outcome.next_prompt is None
        assert outcome.action is StepAction.CONTINUE
        assert outcome.reason == ""
        assert outcome.terminal_status is None

    def test_explicit_completion_request(self) -> None:
        outcome = StepOutcome(data={}, action=StepAction.REQUEST_COMPLETION)
        assert outcome.action is StepAction.REQUEST_COMPLETION
        assert outcome.next_prompt is None

    def test_explicit_wait(self) -> None:
        outcome = StepOutcome(
            data={"question": "..."},
            action=StepAction.WAIT_FOR_USER,
            reason="human_intervention",
        )
        assert outcome.action is StepAction.WAIT_FOR_USER
        assert outcome.reason == "human_intervention"

    def test_explicit_failure(self) -> None:
        outcome = StepOutcome(
            data={"error": "bad"},
            action=StepAction.FAIL,
            reason="bad_request",
            terminal_status=TerminalStatus.FAILED,
        )
        assert outcome.terminal_status is TerminalStatus.FAILED


class TestTerminalEvent:
    def test_defaults(self) -> None:
        event = TerminalEvent()
        assert event.type == "terminal"
        assert event.status is TerminalStatus.FAILED
        assert event.reason == ""
        assert event.turn == 0

    def test_to_dict_serializes_literals_and_certificate(self) -> None:
        certificate = CompletionCertificate(
            task_id="task-1",
            status="completed",
            reason="verified",
            evidence_count=2,
            verify_status="pass",
            plan_remaining=0,
            final_text="done",
        )
        event = TerminalEvent(
            status=TerminalStatus.COMPLETED,
            reason="completion_certificate",
            text="done",
            data={"ok": True},
            turn=3,
            source="agent",
            certificate=certificate,
        )

        assert event.to_dict() == {
            "type": "terminal",
            "status": "completed",
            "reason": "completion_certificate",
            "text": "done",
            "data": {"ok": True},
            "turn": 3,
            "source": "agent",
            "certificate": {
                "task_id": "task-1",
                "status": "completed",
                "reason": "verified",
                "evidence_count": 2,
                "verify_status": "pass",
                "plan_remaining": 0,
                "final_text": "done",
            },
        }


class TestTurnResult:
    def test_defaults(self) -> None:
        result = TurnResult(turn=1)
        assert result.tool_calls == []
        assert result.tool_results == []
        assert result.terminal is None

    def test_terminal(self) -> None:
        terminal = TerminalEvent(status=TerminalStatus.WAITING)
        result = TurnResult(turn=2, terminal=terminal)
        assert result.terminal is terminal
