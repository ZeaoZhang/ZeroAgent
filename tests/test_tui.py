"""Focused terminal-state mapping tests for the Textual frontend."""

import pytest

from zero_agent.core.types import TerminalEvent, TerminalStatus
from zero_agent.frontends.tui import _terminal_view


@pytest.mark.parametrize(
    ("status", "reason", "text", "expected"),
    [
        (TerminalStatus.COMPLETED, "completion_certificate", "done", ("Ready", "done", False)),
        (TerminalStatus.WAITING, "human_intervention", "question", ("Waiting for input", "question", False)),
        (TerminalStatus.CANCELLED, "user_cancelled", "", ("Cancelled", "user_cancelled", False)),
        (TerminalStatus.BUDGET_EXHAUSTED, "max_turns", "", ("Error", "Reached the turn/retry budget; task not completed (max_turns)", True)),
        (TerminalStatus.PROTOCOL_ERROR, "invalid_step_outcome", "", ("Error", "invalid_step_outcome", True)),
        (TerminalStatus.FAILED, "RuntimeError", "broken", ("Error", "broken", True)),
    ],
)
def test_terminal_view_distinguishes_terminal_states(status, reason, text, expected) -> None:
    event = TerminalEvent(status=status, reason=reason, text=text).to_dict()

    assert _terminal_view(event) == expected


def test_terminal_view_renders_waiting_question_and_candidates() -> None:
    event = TerminalEvent(
        status=TerminalStatus.WAITING,
        reason="human_intervention",
        data={
            "status": "INTERRUPT",
            "intent": "HUMAN_INTERVENTION",
            "data": {
                "question": "Proceed?",
                "candidates": ["Yes", "No"],
            },
        },
    ).to_dict()

    assert _terminal_view(event) == (
        "Waiting for input",
        "Proceed?\n\n- Yes\n- No",
        False,
    )
