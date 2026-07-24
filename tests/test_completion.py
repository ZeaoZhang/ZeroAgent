"""Tests for deterministic completion certificate evaluation."""

from zero_agent.core.completion import (
    accepts_partial_reply,
    evaluate_completion,
    load_plan_verify_status,
    write_evidence_json,
)
from zero_agent.core.types import EvidenceLedger, EvidenceRecord, TaskContract, TaskMode
from zero_agent.llm.base import MockResponse


def _contract(mode: TaskMode, *, plan_path: str | None = None) -> TaskContract:
    return TaskContract(
        task_id="task-1",
        user_request="do the work",
        mode=mode,
        plan_path=plan_path,
    )


def test_chat_completion_needs_visible_content() -> None:
    cert, prompt = evaluate_completion(
        _contract(TaskMode.CHAT),
        EvidenceLedger(),
        MockResponse(content="Hello, I can help."),
        plan_remaining=None,
        plan_verify_status="missing",
    )

    assert prompt is None
    assert cert is not None
    assert cert.verify_status == "not_required"
    assert cert.reason == "chat_final_answer"


def test_execution_text_only_does_not_complete() -> None:
    cert, prompt = evaluate_completion(
        _contract(TaskMode.EXECUTION),
        EvidenceLedger(),
        MockResponse(content="Done."),
        plan_remaining=None,
        plan_verify_status="missing",
    )

    assert cert is None
    assert prompt is not None
    assert "cannot complete from text alone" in prompt


def test_execution_requires_last_relevant_evidence_not_error() -> None:
    ledger = EvidenceLedger(records=[
        EvidenceRecord(1, "file_read", "success", "read", "read config"),
        EvidenceRecord(2, "code_run", "error", "execute", "tests failed"),
    ])

    cert, prompt = evaluate_completion(
        _contract(TaskMode.EXECUTION),
        ledger,
        MockResponse(content="Fixed."),
        plan_remaining=None,
        plan_verify_status="missing",
    )

    assert cert is None
    assert prompt is not None
    assert "latest relevant tool evidence is an error" in prompt


def test_execution_success_evidence_signs_certificate() -> None:
    ledger = EvidenceLedger(records=[
        EvidenceRecord(1, "file_read", "success", "read", "read config"),
    ])

    cert, prompt = evaluate_completion(
        _contract(TaskMode.EXECUTION),
        ledger,
        MockResponse(content="Config is valid."),
        plan_remaining=None,
        plan_verify_status="missing",
    )

    assert prompt is None
    assert cert is not None
    assert cert.evidence_count == 1
    assert cert.reason == "execution_evidence_satisfied"


def test_plan_pass_requires_matching_successful_evidence(tmp_path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("- [x] done", encoding="utf-8")
    (tmp_path / "verify_context.json").write_text("{}", encoding="utf-8")
    (tmp_path / "result.md").write_text("details\nVERDICT: PASS\n", encoding="utf-8")
    ledger = EvidenceLedger(records=[
        EvidenceRecord(1, "code_run", "success", "execute", "ran check"),
    ])
    write_evidence_json(tmp_path / "evidence.json", "task-1", ledger)
    contract = _contract(TaskMode.PLAN, plan_path=str(plan_path))

    status = load_plan_verify_status(contract)
    cert, prompt = evaluate_completion(
        contract,
        ledger,
        MockResponse(content="Plan verified."),
        plan_remaining=0,
        plan_verify_status=status,
    )

    assert status == "pass"
    assert prompt is None
    assert cert is not None
    assert cert.verify_status == "pass"


def test_plan_partial_requires_user_acceptance() -> None:
    cert, prompt = evaluate_completion(
        _contract(TaskMode.PLAN),
        EvidenceLedger(),
        MockResponse(content="Mostly done."),
        plan_remaining=0,
        plan_verify_status="partial",
    )

    assert cert is None
    assert prompt is not None
    assert "接受 PARTIAL 并完成" in prompt
    assert accepts_partial_reply("accept_partial") is True
    assert accepts_partial_reply("继续修复") is False
