"""Shared control-plane data structures for the ZeroAgent loop."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal, Optional


class TerminalStatus(str, Enum):
    """Observable terminal states returned by every ZeroAgent run."""

    COMPLETED = "completed"
    WAITING = "waiting"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROTOCOL_ERROR = "protocol_error"



class TaskMode(str, Enum):
    """Observed task state; it never predicts intent from user text."""

    OPEN = "open"
    EXECUTING = "executing"
    PLAN = "plan"


@dataclass(frozen=True)
class TaskContract:
    """Stable per-task contract preserved across waiting continuations."""

    task_id: str
    user_request: str
    mode: TaskMode
    plan_path: Optional[str] = None


@dataclass(frozen=True)
class EvidenceRecord:
    """One observed tool-side effect or verification signal."""

    turn: int
    tool_name: str
    status: Literal["success", "error", "interrupt", "unknown"]
    kind: Literal["read", "write", "execute", "web", "user", "memory", "verify", "system"]
    summary: str
    data_ref: Optional[str] = None


@dataclass
class EvidenceLedger:
    """Append-only evidence records for the current task."""

    records: list[EvidenceRecord] = field(default_factory=list)


@dataclass
class PendingTaskState:
    """Paused task state restored when the user answers a waiting terminal."""

    contract: TaskContract
    ledger: EvidenceLedger
    plan_verify_status: str
    waiting_kind: Literal[
        "ask_user", "plan_partial_acceptance", "approval", "restart"
    ]
    waiting_data: dict[str, Any] = field(default_factory=dict)

    def waiting_prompt(self) -> tuple[str, list[str]]:
        """Return ``(question, candidates)`` for the paused task.

        ``waiting_data`` 的负载形状有两种历史写法（直接是问题字典，或包在
        ``{"data": {...}}`` 里），两个前端都要展示同一条问题，因此归一逻辑
        放在这里，避免各自解析导致文案漂移。

        Returns:
            问题文本与可选快捷选项（缺省空列表）.
        """

        data = self.waiting_data if isinstance(self.waiting_data, dict) else {}
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(payload, dict):
            payload = {}
        question = str(payload.get("question") or "Waiting for user input")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        return question, [str(candidate) for candidate in candidates]

@dataclass(frozen=True)
class CompletionCertificate:
    """Evidence-backed completion record populated by the completion evaluator."""

    task_id: str
    status: Literal["completed"]
    reason: str
    evidence_count: int
    verify_status: Literal["pass", "partial_accepted", "not_required"]
    plan_remaining: Optional[int] = None
    final_text: str = ""


@dataclass(frozen=True)
class TerminalEvent:
    """The sole terminal wire shape exposed by ZeroAgent."""

    type: Literal["terminal"] = "terminal"
    status: TerminalStatus = TerminalStatus.FAILED
    reason: str = ""
    text: str = ""
    data: Any = None
    turn: int = 0
    source: str = "agent"
    certificate: Optional[CompletionCertificate] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the event with stable literal field names."""

        return {
            "type": self.type,
            "status": self.status.value,
            "reason": self.reason,
            "text": self.text,
            "data": self.data,
            "turn": self.turn,
            "source": self.source,
            "certificate": asdict(self.certificate) if self.certificate else None,
        }


class StepAction(str, Enum):
    """Explicit action requested by a tool handler after one step."""

    CONTINUE = "continue"
    REQUEST_COMPLETION = "request_completion"
    WAIT_FOR_USER = "wait_for_user"
    FAIL = "fail"


@dataclass
class StepOutcome:
    """Tool result and explicit control action returned by ``dispatch``.

    ``next_prompt_suffix`` augments an automatic post-evidence anchor without
    capturing that anchor before the evidence record exists.
    """

    data: Any
    next_prompt: Optional[str] = None
    action: StepAction = StepAction.CONTINUE
    reason: str = ""
    terminal_status: Optional[TerminalStatus] = None
    next_prompt_suffix: Optional[str] = None


@dataclass
class TurnResult:
    """Aggregated result of one agent turn."""

    turn: int
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    terminal: Optional[TerminalEvent] = None
