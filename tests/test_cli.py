"""Tests for runners/cli.py — CLI config overrides."""

import argparse

import pytest

from zero_agent.core.exceptions import LLMError
from zero_agent.core.types import TerminalEvent, TerminalStatus
from zero_agent.runners.cli import (
    _build_parser,
    _format_llm_error,
    _load_config,
    _parse_reflect_args,
    _run_oneshot,
    _terminal_message,
)


def test_load_config_writes_max_turns_override(monkeypatch, tmp_path) -> None:
    """CLI --max-turns 必须写入 agent.config.max_turns."""
    monkeypatch.setenv("ZA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ZA_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("ZA_LLM_API_BASE", "https://api.openai.com/v1")
    monkeypatch.setenv("ZA_LLM_MODEL", "test-model")
    monkeypatch.setenv("ZA_MAX_TURNS", "80")
    monkeypatch.setenv("ZA_CONFIG_PATH", str(tmp_path / "missing-config.yaml"))

    config = _load_config(argparse.Namespace(
        config=None,
        model=None,
        workspace=str(tmp_path / "workspace"),
        verbose=False,
        quiet=False,
        max_turns=37,
    ))

    assert config.max_turns == 37

def test_load_config_keeps_yaml_max_turns_without_override(tmp_path) -> None:
    """未传 --max-turns 时保留 YAML 中的 max_turns。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
max_turns: 12
workspace_dir: {tmp_path / "workspace"}
llm_backends:
  default:
    provider: openai
    api_key: sk-test
    api_base: https://api.openai.com/v1
    model: gpt-test
""".lstrip(),
        encoding="utf-8",
    )

    config = _load_config(argparse.Namespace(
        config=str(config_path),
        model=None,
        workspace=None,
        verbose=False,
        quiet=False,
        max_turns=None,
    ))

    assert config.max_turns == 12


def test_parser_accepts_llm_no_and_reflect_args() -> None:
    args = _build_parser().parse_args([
        "--reflect", "zero_agent/reflect/goal_mode.py",
        "--llm-no", "1",
        "--reflect-arg", "goal_state=temp/goal.json",
    ])

    assert args.llm_no == 1
    assert args.reflect_arg == ["goal_state=temp/goal.json"]


def test_parse_reflect_args() -> None:
    parsed = _parse_reflect_args(["base_url=http://127.0.0.1:8000", "name=w1"])

    assert parsed == {"base_url": "http://127.0.0.1:8000", "name": "w1"}


def test_parse_reflect_args_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        _parse_reflect_args(["not-a-pair"])


def test_format_llm_error_removes_litellm_noise() -> None:
    err = LLMError(
        "LLM 调用失败 [default]: litellm.APIError: APIError: "
        "OpenAIException - Your request was blocked."
    )

    message = _format_llm_error(err)

    assert "Give Feedback" not in message
    assert "litellm.APIError" not in message
    assert "Your request was blocked." in message
    assert "服务端已拒绝该请求" in message


def test_parser_accepts_subagent_mode_flags() -> None:
    """parser 必须接受 --func, --task, --nobg, --history, --nolog, --no-user-tools."""
    args = _build_parser().parse_args([
        "--func", "prompt.txt",
        "--nobg",
        "--history", "hist.json",
        "--nolog",
        "--no-user-tools",
    ])
    assert args.func == "prompt.txt"
    assert args.nobg is True
    assert args.history == "hist.json"
    assert args.nolog is True
    assert args.no_user_tools is True


def test_parser_accepts_task_and_nobg() -> None:
    args = _build_parser().parse_args([
        "--task", "/tmp/mytask",
        "--nobg",
    ])
    assert args.task == "/tmp/mytask"
    assert args.nobg is True


def test_parser_defaults_nobg_false() -> None:
    args = _build_parser().parse_args(["--func", "p.txt"])
    assert args.nobg is False
    assert args.history is None
    assert args.nolog is False
    assert args.no_user_tools is False


def test_run_func_mode_writes_out_file(tmp_path, monkeypatch) -> None:
    """_run_func_mode with a fake agent writes <stem>.out.txt ending [ROUND END]."""
    from zero_agent.runners.cli import _run_func_mode

    prompt_file = tmp_path / "my_prompt.txt"
    prompt_file.write_text("Hello, agent!", encoding="utf-8")

    class FakeAgent:
        def __init__(self):
            from types import SimpleNamespace
            self.config = SimpleNamespace(peer_hint=True)
            self.task_dir = "unset"

        def run(self, prompt, **kwargs):
            yield "chunk1 "
            yield "chunk2"
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
            )

        def abort(self):
            pass

    agent = FakeAgent()
    _run_func_mode(agent, str(prompt_file), None)

    out_path = tmp_path / "my_prompt.out.txt"
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert content.endswith("[ROUND END]\n")
    assert "chunk1 chunk2" in content


def test_run_task_mode_writes_output_txt_and_consumes_reply(tmp_path) -> None:
    """task mode writes output.txt, output1.txt on reply, and output.md for first round."""
    import os
    from zero_agent.runners.cli import _run_task_mode
    from zero_agent.utils.files import consume_file

    io_dir = tmp_path
    (io_dir / "input.txt").write_text("Task round 1", encoding="utf-8")

    call_count = [0]

    class FakeAgent:
        def __init__(self):
            from types import SimpleNamespace
            self.config = SimpleNamespace(peer_hint=True)
            self.task_dir = "unset"

        def run(self, prompt, **kwargs):
            call_count[0] += 1
            # After first round, write reply.txt for the next iteration
            if call_count[0] == 1:
                (io_dir / "reply.txt").write_text("Task round 2", encoding="utf-8")
            yield f"round{call_count[0]}_output "
            return TerminalEvent(
                status=TerminalStatus.COMPLETED,
                reason="completion_certificate",
            )

        def abort(self):
            pass

    agent = FakeAgent()

    # Monkeypatch _time.sleep so the test doesn't wait 2-second intervals
    import zero_agent.runners.cli as cli_mod
    orig_sleep = cli_mod._time.sleep
    cli_mod._time.sleep = lambda s: None

    try:
        _run_task_mode(agent, str(io_dir), None)
    except SystemExit as e:
        # Normal completion exits 0; ensure it's not an error exit
        assert e.code == 0
    finally:
        cli_mod._time.sleep = orig_sleep

    assert (io_dir / "output.txt").exists()
    assert (io_dir / "output.md").exists()
    assert (io_dir / "output1.txt").exists()

    first = (io_dir / "output.txt").read_text(encoding="utf-8")
    second = (io_dir / "output1.txt").read_text(encoding="utf-8")
    assert first.endswith("[ROUND END]\n")
    assert second.endswith("[ROUND END]\n")
    assert "round1_output" in first
    assert "round2_output" in second
    assert call_count[0] == 2


def test_run_task_mode_writes_evidence_json(tmp_path) -> None:
    """task mode writes current handler evidence for verifier handoff."""
    import json
    from types import SimpleNamespace
    from zero_agent.core.types import EvidenceLedger, EvidenceRecord, TaskContract, TaskMode
    from zero_agent.runners.cli import _run_task_mode

    (tmp_path / "input.txt").write_text("Task round 1", encoding="utf-8")
    ledger = EvidenceLedger(records=[
        EvidenceRecord(1, "code_run", "success", "execute", "ran check"),
    ])

    class FakeAgent:
        config = SimpleNamespace(peer_hint=True)
        task_dir = "unset"
        handler = SimpleNamespace(
            task_contract=TaskContract("task-1", "Task round 1", TaskMode.EXECUTING),
            evidence_ledger=ledger,
        )

        def run(self, _prompt):
            yield "round_output "
            return TerminalEvent(status=TerminalStatus.COMPLETED)

        def abort(self):
            pass

    import zero_agent.runners.cli as cli_mod
    orig_sleep = cli_mod._time.sleep
    cli_mod._time.sleep = lambda _seconds: None

    try:
        try:
            _run_task_mode(FakeAgent(), str(tmp_path), None)
        except SystemExit as exc:
            assert exc.code == 0
    finally:
        cli_mod._time.sleep = orig_sleep

    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload == {
        "task_id": "task-1",
        "records": [{
            "turn": 1,
            "tool_name": "code_run",
            "status": "success",
            "kind": "execute",
            "summary": "ran check",
            "data_ref": None,
        }],
    }


def test_terminal_message_renders_waiting_question_and_candidates() -> None:
    terminal = TerminalEvent(
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
    )

    assert _terminal_message(terminal) == "Proceed?\n\n- Yes\n- No"


def test_terminal_message_explains_budget_exhaustion() -> None:
    terminal = TerminalEvent(
        status=TerminalStatus.BUDGET_EXHAUSTED,
        reason="max_turns",
    )

    message = _terminal_message(terminal)
    assert "task not completed" in message
    assert "max_turns" in message


@pytest.mark.parametrize(
    "status",
    [
        TerminalStatus.FAILED,
        TerminalStatus.PROTOCOL_ERROR,
        TerminalStatus.BUDGET_EXHAUSTED,
    ],
)
def test_run_oneshot_exits_nonzero_for_error_terminals(status) -> None:
    class FakeAgent:
        def run(self, _prompt):
            if False:
                yield None
            return TerminalEvent(status=status, reason="terminal_reason")

        def abort(self):
            pass

    with pytest.raises(SystemExit) as exc_info:
        _run_oneshot(FakeAgent(), "task")

    assert exc_info.value.code == 1

def test_run_oneshot_converts_synchronous_exception() -> None:
    class FakeAgent:
        def run(self, _prompt):
            raise RuntimeError("broken")

        def abort(self):
            pass

    with pytest.raises(SystemExit) as exc_info:
        _run_oneshot(FakeAgent(), "task")

    assert exc_info.value.code == 1


@pytest.mark.parametrize(
    "status",
    [
        TerminalStatus.FAILED,
        TerminalStatus.PROTOCOL_ERROR,
        TerminalStatus.BUDGET_EXHAUSTED,
    ],
)
def test_run_func_mode_exits_nonzero_for_error_terminals(tmp_path, status) -> None:
    from types import SimpleNamespace
    from zero_agent.runners.cli import _run_func_mode

    prompt_file = tmp_path / "failure.txt"
    prompt_file.write_text("task", encoding="utf-8")

    class FakeAgent:
        config = SimpleNamespace(peer_hint=True)
        task_dir = "unset"

        def run(self, _prompt):
            yield "partial"
            return TerminalEvent(status=status, reason="terminal_reason")

        def abort(self):
            pass

    with pytest.raises(SystemExit) as exc_info:
        _run_func_mode(FakeAgent(), str(prompt_file))

    assert exc_info.value.code == 1
    output = (tmp_path / "failure.out.txt").read_text(encoding="utf-8")
    assert f"[{status.value}]" in output
    assert "terminal_reason" in output


@pytest.mark.parametrize(
    "status",
    [
        TerminalStatus.FAILED,
        TerminalStatus.PROTOCOL_ERROR,
        TerminalStatus.BUDGET_EXHAUSTED,
    ],
)
def test_run_task_mode_exits_nonzero_for_error_terminals(tmp_path, status) -> None:
    from types import SimpleNamespace
    from zero_agent.runners.cli import _run_task_mode

    (tmp_path / "input.txt").write_text("task", encoding="utf-8")

    class FakeAgent:
        config = SimpleNamespace(peer_hint=True)
        task_dir = "unset"

        def run(self, _prompt):
            yield "partial"
            return TerminalEvent(status=status, reason="terminal_reason")

        def abort(self):
            pass

    with pytest.raises(SystemExit) as exc_info:
        _run_task_mode(FakeAgent(), str(tmp_path))

    assert exc_info.value.code == 1
    output = (tmp_path / "output.txt").read_text(encoding="utf-8")
    assert f"[{status.value}]" in output
    assert "terminal_reason" in output
