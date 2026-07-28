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

_SUMMARY_RE = re.compile(r"<\s*summary\b[^>]*>[\s\S]*?<\s*/\s*summary\s*>", re.IGNORECASE)
_THINKING_RE = re.compile(r"<\s*thinking\b[^>]*>[\s\S]*?<\s*/\s*thinking\s*>", re.IGNORECASE)
_VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(PASS|FAIL|PARTIAL)\s*$", re.IGNORECASE | re.MULTILINE)


def evaluate_completion(
    contract: TaskContract,
    ledger: EvidenceLedger,
    response: Any,
    *,
    final_text: Optional[str] = None,
    evidence_refs: Optional[list[int]] = None,
    plan_remaining: Optional[int],
    plan_verify_status: str,
) -> tuple[Optional[CompletionCertificate], Optional[str]]:
    """Return ``(certificate, continuation_prompt)`` for one no-tool response."""

    mode = _task_mode(contract.mode)
    final_text = _visible_text(response) if final_text is None else final_text.strip()
    blocker = _completion_blocker(response, final_text)

    if mode is TaskMode.OPEN:
        if blocker:
            return None, blocker
        if not final_text:
            return None, "[System] complete_task requires a visible final answer."
        return _certificate(
            contract,
            ledger,
            reason="open_answer_completed",
            verify_status="not_required",
            plan_remaining=plan_remaining,
            final_text=final_text,
        ), None

    if mode is TaskMode.PLAN:
        if blocker:
            return None, blocker
        if not final_text:
            return None, "[System] complete_task requires a visible final answer."
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

    selected, ref_error = _select_evidence(_records(ledger), evidence_refs)
    if ref_error:
        return None, ref_error
    if any(_record_status(record) == "error" for record in selected):
        return None, "[System] Referenced tool evidence contains an error. Fix it before completing."

    successful = [record for record in selected if _record_status(record) == "success"]
    if len(successful) != len(selected):
        return None, "[System] Every complete_task evidence reference must be successful."
    if not successful:
        return None, (
            "[System] EXECUTING tasks require complete_task.evidence_refs pointing to "
            "successful read/write/execute/web/verify evidence from this task."
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
        return TaskMode.OPEN


def _select_evidence(
    relevant: list[Any],
    evidence_refs: Optional[list[int]],
) -> tuple[list[Any], Optional[str]]:
    """Resolve one-based references against this task's complete ledger."""

    if not evidence_refs:
        return [], None
    if any(not isinstance(ref, int) or isinstance(ref, bool) for ref in evidence_refs):
        return [], "[System] complete_task.evidence_refs must contain integer record numbers."
    if len(set(evidence_refs)) != len(evidence_refs):
        return [], "[System] complete_task.evidence_refs must not contain duplicates."
    if any(ref < 1 or ref > len(relevant) for ref in evidence_refs):
        return [], (
            "[System] complete_task.evidence_refs contains an unknown record number. "
            f"Valid evidence records are 1..{len(relevant)}."
        )
    selected = [relevant[ref - 1] for ref in evidence_refs]
    if any(_record_kind(record) not in _RELEVANT_EVIDENCE_KINDS for record in selected):
        return [], (
            "[System] complete_task.evidence_refs may only reference "
            "read/write/execute/web/verify records."
        )
    return selected, None


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
