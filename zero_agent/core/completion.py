"""Deterministic completion certificates for ZeroAgent tasks."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from zero_agent.core.interruption import classify_interruption
from zero_agent.core.types import (
    CompletionCertificate,
    EvidenceLedger,
    EvidenceRecord,
    TaskContract,
    TaskMode,
)

_RELEVANT_EVIDENCE_KINDS = {"read", "write", "execute", "web", "verify"}
_PLAN_VERIFY_KINDS = {"read", "execute", "web", "verify"}
_ACCEPT_PARTIAL_LITERALS = {
    "accept_partial",
    "接受 partial",
    "接受 PARTIAL 并完成",
}

_ACTION_PROMISE_RE = re.compile(
    r"("
    r"\b(?:i\s*(?:will|'ll|am going to)|i need to|let me|next i will|i can now)\b"
    r"|(?:接下来|下一步|现在|稍后|继续|先).{0,16}(?:查看|读取|检查|运行|执行|打开|写入|修改|搜索|验证|调用)"
    r"|(?:我(?:会|将|要|需要|可以)).{0,16}(?:查看|读取|检查|运行|执行|打开|写入|修改|搜索|验证|调用)"
    r")",
    re.IGNORECASE,
)
_PROTOCOL_TEXT_RE = re.compile(
    r"<\s*(?:tool_use|tool_call|function_call|file_content)\b",
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(r"<\s*summary\b[^>]*>[\s\S]*?<\s*/\s*summary\s*>", re.IGNORECASE)
_THINKING_RE = re.compile(r"<\s*thinking\b[^>]*>[\s\S]*?<\s*/\s*thinking\s*>", re.IGNORECASE)
_VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(PASS|FAIL|PARTIAL)\s*$", re.IGNORECASE | re.MULTILINE)


def evaluate_completion(
    contract: TaskContract,
    ledger: EvidenceLedger,
    response: Any,
    *,
    plan_remaining: Optional[int],
    plan_verify_status: str,
) -> tuple[Optional[CompletionCertificate], Optional[str]]:
    """Return ``(certificate, continuation_prompt)`` for one no-tool response."""

    mode = _task_mode(contract.mode)
    final_text = _visible_text(response)
    blocker = _completion_blocker(response, final_text)

    if mode is TaskMode.CHAT:
        if blocker:
            return None, blocker
        if not final_text:
            return None, "[System] The chat answer is empty. Provide a visible final answer."
        return _certificate(
            contract,
            ledger,
            reason="chat_final_answer",
            verify_status="not_required",
            plan_remaining=plan_remaining,
            final_text=final_text,
        ), None

    if mode is TaskMode.PLAN:
        status = (plan_verify_status or "missing").strip().lower()
        if plan_remaining == 0 and status in {"pass", "partial_accepted"}:
            return _certificate(
                contract,
                ledger,
                reason="plan_verified",
                verify_status="partial_accepted" if status == "partial_accepted" else "pass",
                plan_remaining=plan_remaining,
                final_text=final_text,
            ), None
        if status == "partial":
            return None, _partial_acceptance_prompt()
        return None, _plan_continuation_prompt(plan_remaining, status)

    if blocker:
        return None, blocker
    if not final_text:
        return None, "[System] Execution tasks need a visible final response after tool evidence."

    relevant = [record for record in _records(ledger) if _record_kind(record) in _RELEVANT_EVIDENCE_KINDS]
    if relevant and _record_status(relevant[-1]) == "error":
        return None, "[System] The latest relevant tool evidence is an error. Fix or explain the blocker with evidence."

    successful = [record for record in relevant if _record_status(record) == "success"]
    if not successful:
        return None, (
            "[System] Execution tasks cannot complete from text alone. "
            "Use an appropriate tool and collect successful read/write/execute/web/verify evidence."
        )

    verify_status = "pass" if any(_record_kind(record) == "verify" for record in successful) else "not_required"
    return _certificate(
        contract,
        ledger,
        reason="execution_evidence_satisfied",
        verify_status=verify_status,
        plan_remaining=plan_remaining,
        final_text=final_text,
    ), None


def load_plan_verify_status(contract: TaskContract) -> str:
    """Validate plan verifier files and return pass/partial/fail/missing."""

    if not contract.plan_path:
        return "missing"
    plan_path = Path(contract.plan_path)
    plan_dir = plan_path.parent
    result_path = plan_dir / "result.md"
    evidence_path = plan_dir / "evidence.json"
    verify_context_path = plan_dir / "verify_context.json"

    if not result_path.is_file() or not evidence_path.is_file() or not verify_context_path.is_file():
        return "missing"

    verdict = _read_verdict(result_path)
    if verdict is None:
        return "missing"
    if verdict == "fail":
        return "fail"

    records = _read_evidence_records(evidence_path, contract.task_id)
    if not any(
        _record_status(record) == "success" and _record_kind(record) in _PLAN_VERIFY_KINDS
        for record in records
    ):
        return "missing"
    return verdict


def accepts_partial_reply(reply: str) -> bool:
    """Return True only for the fixed partial-acceptance literals."""

    return (reply or "").strip() in _ACCEPT_PARTIAL_LITERALS


def write_evidence_json(path: str | Path, task_id: str, ledger: EvidenceLedger) -> None:
    """Atomically write task-mode evidence for verifier validation."""

    target = Path(path)
    payload = {
        "task_id": task_id,
        "records": [asdict(record) if isinstance(record, EvidenceRecord) else dict(record) for record in _records(ledger)],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _certificate(
    contract: TaskContract,
    ledger: EvidenceLedger,
    *,
    reason: str,
    verify_status: str,
    plan_remaining: Optional[int],
    final_text: str,
) -> CompletionCertificate:
    return CompletionCertificate(
        task_id=contract.task_id,
        status="completed",
        reason=reason,
        evidence_count=len(_records(ledger)),
        verify_status=verify_status,  # type: ignore[arg-type]
        plan_remaining=plan_remaining,
        final_text=final_text,
    )


def _completion_blocker(response: Any, final_text: str) -> Optional[str]:
    if classify_interruption(response):
        return "[System] The model response was interrupted or truncated. Retry before completing."
    stop_reason = str(getattr(response, "stop_reason", "") or "")
    if stop_reason.startswith("unknown:") or stop_reason in {"error", "content_filter", "cancelled", "interrupted", "stream_interrupted"}:
        return f"[System] The provider stop_reason `{stop_reason}` is not a trusted completion signal. Retry or collect evidence."
    if _PROTOCOL_TEXT_RE.search(final_text):
        return "[System] The response contains tool protocol text instead of a final deliverable. Regenerate or use tools correctly."
    if _ACTION_PROMISE_RE.search(final_text):
        return "[System] The response promises future tool/action work. Perform the work or report a real blocker; do not complete yet."
    return None


def _visible_text(response: Any) -> str:
    text = str(getattr(response, "content", "") or "")
    text = _THINKING_RE.sub("", text)
    text = _SUMMARY_RE.sub("", text)
    return text.strip()


def _task_mode(mode: Any) -> TaskMode:
    if isinstance(mode, TaskMode):
        return mode
    try:
        return TaskMode(str(mode))
    except ValueError:
        return TaskMode.EXECUTION


def _records(ledger: EvidenceLedger) -> list[Any]:
    records = getattr(ledger, "records", [])
    return list(records) if isinstance(records, list) else []


def _record_status(record: Any) -> str:
    return str(_record_get(record, "status") or "unknown")


def _record_kind(record: Any) -> str:
    return str(_record_get(record, "kind") or "system")


def _record_get(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _read_verdict(path: Path) -> Optional[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _VERDICT_RE.findall(content)
    if not matches:
        return None
    return matches[-1].lower()


def _read_evidence_records(path: Path, task_id: str) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("task_id") != task_id:
        return []
    records = payload.get("records")
    return records if isinstance(records, list) else []


def _partial_acceptance_prompt() -> str:
    return (
        "[System] Plan verification returned VERDICT: PARTIAL. Ask the user before completing. "
        "Use ask_user with exactly these candidates: `接受 PARTIAL 并完成` and `继续修复`. "
        "Only the literal replies `accept_partial`, `接受 partial`, or `接受 PARTIAL 并完成` may mark partial_accepted."
    )


def _plan_continuation_prompt(plan_remaining: Optional[int], status: str) -> str:
    remaining = "unknown" if plan_remaining is None else str(plan_remaining)
    return (
        "[System] Plan completion is not certified. "
        f"Unchecked items: {remaining}; verifier status: {status or 'missing'}. "
        "Update the plan, run verification, and collect valid evidence before completing."
    )
