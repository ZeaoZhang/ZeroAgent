"""Tests for runners/cli.py — CLI config overrides."""

import argparse
import os
import threading
import warnings

import pytest

from zero_agent.core.exceptions import LLMError
from zero_agent.core.types import (
    CompletionCertificate,
    TaskContract,
    TaskMode,
    TerminalEvent,
    TerminalStatus,
)
from zero_agent.runners.cli import (
    _build_parser,
    _consume_prompt,
    _format_llm_error,
    _handle_plan_slash,
    _is_plan_command,
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

        def run(self, _prompt, **kwargs):
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
        def run(self, _prompt, **kwargs):
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
        def run(self, _prompt, **kwargs):
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

        def run(self, _prompt, **kwargs):
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

        def run(self, _prompt, **kwargs):
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


# ── /plan command handling ─────────────────────────────────────────────


class _RecordingAgent:
    """Fake agent recording run() calls and streaming a completed terminal."""

    def __init__(self, workspace_dir, mode=TaskMode.OPEN, plan_path=None,
                 user_request="", running=False, certificate=None, pending_contract=None):
        from types import SimpleNamespace

        self.config = SimpleNamespace(workspace_dir=str(workspace_dir))
        self._is_running_task = running
        self.handler = SimpleNamespace(
            task_contract=TaskContract("t1", user_request, mode, plan_path),
            completion_certificate=certificate,
        )
        self._pending_task_state = (
            SimpleNamespace(contract=pending_contract)
            if pending_contract is not None else None
        )
        self.calls = []

    def run(self, prompt, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        yield "chunk"
        return TerminalEvent(status=TerminalStatus.COMPLETED)

def _plan_certificate(plan_remaining=0, verify_status="pass"):
    """A ready completion certificate."""
    return CompletionCertificate(
        task_id="t1",
        status="completed",
        reason="plan_verified",
        evidence_count=0,
        verify_status=verify_status,
        plan_remaining=plan_remaining,
    )



def test_is_plan_command_recognizes_plan() -> None:
    assert _is_plan_command("/plan") is True
    assert _is_plan_command("/plan build a bridge") is True
    assert _is_plan_command("/PLAN execute") is True
    assert _is_plan_command("/help") is False
    assert _is_plan_command("/planning") is False
    assert _is_plan_command("just text") is False


def test_plan_empty_shows_usage(tmp_path, capsys) -> None:
    agent = _RecordingAgent(tmp_path)
    _handle_plan_slash("/plan", agent)
    assert "用法" in capsys.readouterr().out
    assert agent.calls == []


def test_plan_task_creates_workspace_and_passes_plan(tmp_path) -> None:
    agent = _RecordingAgent(tmp_path)
    _handle_plan_slash("/plan build a bridge", agent)
    assert len(agent.calls) == 1
    prompt, kwargs = agent.calls[0]
    assert prompt == "build a bridge"
    assert kwargs["initial_mode"] is TaskMode.PLAN
    plan_path = kwargs["plan_path"]
    assert os.path.isfile(plan_path)
    assert os.path.basename(plan_path) == "plan.md"


def test_plan_execute_passes_executing_and_path(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [x] one\n- [x] two\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path,
        mode=TaskMode.PLAN,
        plan_path=str(plan_file),
        user_request="do it",
        certificate=_plan_certificate(),
    )
    _handle_plan_slash("/plan execute", agent)
    assert len(agent.calls) == 1
    prompt, kwargs = agent.calls[0]
    assert prompt == "do it"
    assert kwargs["initial_mode"] is TaskMode.EXECUTING
    assert kwargs["plan_path"] == str(plan_file)

def test_plan_execute_resumes_pending_executing_without_ready_certificate(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [x] old step\n", encoding="utf-8")
    pending_contract = TaskContract("pending-1", "continue it", TaskMode.EXECUTING, str(plan_file))
    agent = _RecordingAgent(tmp_path, pending_contract=pending_contract)

    _handle_plan_slash("/plan execute", agent)

    assert len(agent.calls) == 1
    prompt, kwargs = agent.calls[0]
    assert prompt == "continue it"
    assert kwargs["initial_mode"] is TaskMode.EXECUTING
    assert kwargs["plan_path"] == str(plan_file)


def test_plan_execute_resumes_pending_waiting_plan_without_ready_certificate(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] answer user\n", encoding="utf-8")
    pending_contract = TaskContract("pending-1", "finish plan", TaskMode.PLAN, str(plan_file))
    agent = _RecordingAgent(tmp_path, pending_contract=pending_contract)

    _handle_plan_slash("/plan execute", agent)

    assert len(agent.calls) == 1
    prompt, kwargs = agent.calls[0]
    assert prompt == "finish plan"
    assert kwargs["initial_mode"] is TaskMode.PLAN
    assert kwargs["plan_path"] == str(plan_file)


def test_plan_execute_not_ready_does_not_run(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] pending\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path, mode=TaskMode.PLAN, plan_path=str(plan_file), user_request="do it"
    )
    _handle_plan_slash("/plan execute", agent)
    assert agent.calls == []


def test_plan_execute_no_plan_does_not_run(tmp_path, capsys) -> None:
    agent = _RecordingAgent(tmp_path)
    _handle_plan_slash("/plan execute", agent)
    assert agent.calls == []
    assert "没有就绪" in capsys.readouterr().out


def test_plan_rejected_while_running(tmp_path, capsys) -> None:
    agent = _RecordingAgent(tmp_path, running=True)
    _handle_plan_slash("/plan build a bridge", agent)
    assert agent.calls == []
    assert "正在运行" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []

def test_plan_rejected_while_pending_executing(tmp_path, capsys) -> None:
    pending_contract = TaskContract(
        "t1",
        "do it",
        TaskMode.EXECUTING,
        str(tmp_path / "plan.md"),
    )
    agent = _RecordingAgent(tmp_path, pending_contract=pending_contract)
    _handle_plan_slash("/plan build a bridge", agent)
    assert agent.calls == []
    assert "活动计划" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []

def test_plan_rejected_while_pending_waiting(tmp_path, capsys) -> None:
    pending_contract = TaskContract(
        "t1",
        "do it",
        TaskMode.PLAN,
        str(tmp_path / "plan.md"),
    )
    agent = _RecordingAgent(tmp_path, pending_contract=pending_contract)
    _handle_plan_slash("/plan build a bridge", agent)
    assert agent.calls == []
    assert "活动计划" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []



def test_plan_terminal_non_ready_contract_allows_recovery(tmp_path) -> None:
    agent = _RecordingAgent(
        tmp_path, mode=TaskMode.PLAN, plan_path=str(tmp_path / "old_plan.md")
    )
    _handle_plan_slash("/plan another task", agent)
    assert len(agent.calls) == 1
    prompt, kwargs = agent.calls[0]
    assert prompt == "another task"
    assert kwargs["initial_mode"] is TaskMode.PLAN
    assert os.path.basename(os.path.dirname(kwargs["plan_path"])) == "plan_another_task"


def test_plan_startup_exception_cleans_workspace(tmp_path) -> None:
    from types import SimpleNamespace

    class BrokenAgent:
        def __init__(self):
            self.config = SimpleNamespace(workspace_dir=str(tmp_path))
            self._is_running_task = False
            self.handler = SimpleNamespace(
                task_contract=TaskContract("t1", "", TaskMode.OPEN, None)
            )

        def run(self, prompt, **kwargs):
            raise RuntimeError("startup failure")

    _handle_plan_slash("/plan doomed task", BrokenAgent())
    assert list(tmp_path.iterdir()) == []


# ── TUI /plan special dispatch ─────────────────────────────────────────

def _make_fake_tui(agent):
    from unittest.mock import MagicMock
    from zero_agent.frontends.tui import ZeroAgentTui

    app = object.__new__(ZeroAgentTui)
    app.agent = agent
    app._append_message = MagicMock()
    app._run_prompt = MagicMock()
    app._plan_ready_notified = False
    app._run_reservation_lock = threading.Lock()
    app._run_reserved = False
    return app


def test_tui_plan_task_special_dispatch(tmp_path) -> None:
    agent = _RecordingAgent(tmp_path)
    app = _make_fake_tui(agent)
    assert app._try_dispatch_plan("/plan build it") is True
    app._run_prompt.assert_called_once()
    args, kwargs = app._run_prompt.call_args
    assert args[0] == "build it"
    assert kwargs["initial_mode"] is TaskMode.PLAN
    assert os.path.isfile(kwargs["plan_path"])


def test_tui_plan_execute_explicit(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [x] one\n- [x] two\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path,
        mode=TaskMode.PLAN,
        plan_path=str(plan_file),
        user_request="do it",
        certificate=_plan_certificate(),
    )
    app = _make_fake_tui(agent)
    assert app._try_dispatch_plan("/plan execute") is True
    app._run_prompt.assert_called_once()
    args, kwargs = app._run_prompt.call_args
    assert args[0] == "do it"
    assert kwargs["initial_mode"] is TaskMode.EXECUTING
    assert kwargs["plan_path"] == str(plan_file)

def test_tui_plan_execute_resumes_pending_executing_without_ready_certificate(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [x] old step\n", encoding="utf-8")
    pending_contract = TaskContract("pending-1", "continue it", TaskMode.EXECUTING, str(plan_file))
    agent = _RecordingAgent(tmp_path, pending_contract=pending_contract)
    app = _make_fake_tui(agent)

    assert app._try_dispatch_plan("/plan execute") is True

    app._run_prompt.assert_called_once()
    args, kwargs = app._run_prompt.call_args
    assert args[0] == "continue it"
    assert kwargs["initial_mode"] is TaskMode.EXECUTING
    assert kwargs["plan_path"] == str(plan_file)


def test_tui_plan_execute_resumes_pending_waiting_plan_without_ready_certificate(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] answer user\n", encoding="utf-8")
    pending_contract = TaskContract("pending-1", "finish plan", TaskMode.PLAN, str(plan_file))
    agent = _RecordingAgent(tmp_path, pending_contract=pending_contract)
    app = _make_fake_tui(agent)

    assert app._try_dispatch_plan("/plan execute") is True

    app._run_prompt.assert_called_once()
    args, kwargs = app._run_prompt.call_args
    assert args[0] == "finish plan"
    assert kwargs["initial_mode"] is TaskMode.PLAN
    assert kwargs["plan_path"] == str(plan_file)


def test_tui_plan_execute_not_ready_no_run(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] pending\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path, mode=TaskMode.PLAN, plan_path=str(plan_file), user_request="do it"
    )
    app = _make_fake_tui(agent)
    assert app._try_dispatch_plan("/plan execute") is True
    app._run_prompt.assert_not_called()


def test_tui_non_plan_slash_returns_false(tmp_path) -> None:
    agent = _RecordingAgent(tmp_path)
    app = _make_fake_tui(agent)
    assert app._try_dispatch_plan("/help") is False
    app._run_prompt.assert_not_called()


# ── TUI worker workspace cleanup ───────────────────────────────────────


def _make_worker_tui(agent):
    import queue as _queue
    import threading as _threading
    from zero_agent.frontends.tui import ZeroAgentTui

    app = object.__new__(ZeroAgentTui)
    app.agent = agent
    app._chunk_queue = _queue.Queue()
    app._abort_flag = _threading.Event()
    return app


def test_tui_worker_startup_exception_cleans_workspace(tmp_path) -> None:
    from types import SimpleNamespace

    class BrokenAgent:
        def __init__(self):
            self.config = SimpleNamespace(workspace_dir=str(tmp_path))
            self.handler = SimpleNamespace(
                task_contract=TaskContract("t1", "", TaskMode.OPEN, None)
            )

        def run(self, prompt, **kwargs):
            raise RuntimeError("startup failure")

    workspace = tmp_path / "plan_doomed"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    app = _make_worker_tui(BrokenAgent())
    app._worker_run(
        "doomed",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert not workspace.exists()


def test_tui_worker_success_keeps_workspace(tmp_path) -> None:
    workspace = tmp_path / "plan_ok"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)
    terminals = []

    def adopting_run(prompt, **kwargs):
        agent.calls.append((prompt, dict(kwargs)))
        agent.handler.task_contract = TaskContract(
            "t-adopted", prompt, TaskMode.PLAN, kwargs["plan_path"]
        )
        yield "chunk"
        return TerminalEvent(status=TerminalStatus.COMPLETED)

    agent.run = adopting_run
    app = _make_worker_tui(agent)
    app._worker_run(
        "ok",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert agent.calls == [(
        "ok", {"initial_mode": TaskMode.PLAN, "plan_path": str(plan_path)}
    )]
    while not app._chunk_queue.empty():
        item = app._chunk_queue.get_nowait()
        if isinstance(item, dict) and item.get("type") == "terminal":
            terminals.append(item)
    assert terminals[-1]["status"] == TerminalStatus.COMPLETED.value
    assert workspace.exists()
    assert plan_path.exists()

def test_tui_worker_adoption_without_yield_keeps_workspace(tmp_path) -> None:
    workspace = tmp_path / "plan_no_yield"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)

    def adopting_without_yield(prompt, **kwargs):
        agent.handler.task_contract = TaskContract(
            "t-adopted", prompt, TaskMode.PLAN, kwargs["plan_path"]
        )
        if False:
            yield "unreachable"
        return TerminalEvent(status=TerminalStatus.COMPLETED)

    agent.run = adopting_without_yield
    app = _make_worker_tui(agent)
    app._worker_run(
        "ok no yield",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )

    terminals = [
        item for item in list(app._chunk_queue.queue)
        if isinstance(item, dict) and item.get("type") == "terminal"
    ]
    assert terminals[-1]["status"] == TerminalStatus.COMPLETED.value
    assert workspace.exists()
    assert plan_path.exists()


def test_tui_worker_cancelled_before_adoption_cleans_workspace(tmp_path) -> None:
    workspace = tmp_path / "plan_cancelled"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)

    def cancelled_run(prompt, **kwargs):
        yield "chunk"
        return TerminalEvent(status=TerminalStatus.CANCELLED, reason="user_cancelled")

    agent.run = cancelled_run
    app = _make_worker_tui(agent)
    app._worker_run(
        "cancelled",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert not workspace.exists()


def test_tui_worker_waiting_before_adoption_cleans_workspace(tmp_path) -> None:
    workspace = tmp_path / "plan_waiting"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)

    def waiting_run(prompt, **kwargs):
        yield "chunk"
        return TerminalEvent(status=TerminalStatus.WAITING, reason="pending_continuation")

    agent.run = waiting_run
    app = _make_worker_tui(agent)
    app._worker_run(
        "waiting",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert not workspace.exists()


def test_tui_worker_adopted_plan_not_cleaned_on_failure(tmp_path) -> None:
    """A failure after the agent adopted the plan path must not be deleted."""
    workspace = tmp_path / "plan_adopted"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(
        tmp_path, mode=TaskMode.PLAN, plan_path=str(plan_path)
    )

    def failing_run(prompt, **kwargs):
        yield "chunk"
        raise RuntimeError("mid-run failure")

    agent.run = failing_run
    app = _make_worker_tui(agent)
    app._worker_run(
        "adopted",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert workspace.exists()

def test_tui_worker_stale_replaced_handler_does_not_delete_adopted_workspace(tmp_path) -> None:
    """Cleanup uses this run's observed adoption, not a later shared handler."""
    from types import SimpleNamespace

    workspace = tmp_path / "plan_stale_handler"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)

    def adopting_then_replacing_run(prompt, **kwargs):
        agent.handler.task_contract = TaskContract(
            "t-current", prompt, TaskMode.PLAN, kwargs["plan_path"]
        )
        yield "chunk"
        agent.handler = SimpleNamespace(
            task_contract=TaskContract(
                "t-next", "next task", TaskMode.OPEN, None
            )
        )
        return TerminalEvent(status=TerminalStatus.COMPLETED)

    agent.run = adopting_then_replacing_run
    app = _make_worker_tui(agent)
    app._worker_run(
        "adopted then stale",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert workspace.exists()


def test_tui_worker_replaced_handler_without_adoption_cleans_workspace(tmp_path) -> None:
    """A later handler owning a different run must not count as this run's adoption."""
    from types import SimpleNamespace

    workspace = tmp_path / "plan_unadopted_stale"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)

    def replaced_without_adoption_run(prompt, **kwargs):
        yield "chunk"
        agent.handler = SimpleNamespace(
            task_contract=TaskContract(
                "t-next", "next task", TaskMode.PLAN, kwargs["plan_path"]
            )
        )
        return TerminalEvent(status=TerminalStatus.COMPLETED)

    agent.run = replaced_without_adoption_run
    app = _make_worker_tui(agent)
    app._worker_run(
        "unadopted then stale",
        initial_mode=TaskMode.PLAN,
        plan_path=str(plan_path),
        workspace_directory=str(workspace),
    )
    assert not workspace.exists()

def test_tui_plan_dispatch_reserves_before_agent_run_advances(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    started = threading.Event()
    release = threading.Event()

    class BlockingAgent:
        def __init__(self):
            self.config = SimpleNamespace(workspace_dir=str(tmp_path))
            self._is_running_task = False
            self.handler = SimpleNamespace(
                task_contract=TaskContract("t1", "", TaskMode.OPEN, None)
            )
            self.calls = []

        def run(self, prompt, **kwargs):
            self.calls.append((prompt, dict(kwargs)))
            started.set()
            release.wait(timeout=5)
            yield "chunk"
            return TerminalEvent(status=TerminalStatus.COMPLETED)

    agent = BlockingAgent()
    app = _make_worker_tui(agent)
    app._append_message = MagicMock()
    app._streaming_text = ""
    app._streaming_markdown = None
    app._run_reservation_lock = threading.Lock()
    app._run_reserved = False

    assert app._try_dispatch_plan("/plan build safely") is True
    assert started.wait(timeout=2)
    workspaces_after_first = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(workspaces_after_first) == 1

    assert app._try_dispatch_plan("/plan build safely again") is True
    assert len(agent.calls) == 1
    assert [p for p in tmp_path.iterdir() if p.is_dir()] == workspaces_after_first
    assert any(
        call.args[:2] == ("system", "当前有任务正在运行，请先等待完成")
        and call.kwargs.get("error") is True
        for call in app._append_message.call_args_list
    )

    release.set()
    for _ in range(50):
        if not app._run_reserved:
            break
        threading.Event().wait(0.05)
    assert app._run_reserved is False


def test_tui_plan_reservation_released_after_startup_failure(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    class BrokenAgent:
        def __init__(self):
            self.config = SimpleNamespace(workspace_dir=str(tmp_path))
            self._is_running_task = False
            self.handler = SimpleNamespace(
                task_contract=TaskContract("t1", "", TaskMode.OPEN, None)
            )
            self.calls = 0

        def run(self, prompt, **kwargs):
            self.calls += 1
            raise RuntimeError("startup failure")

    agent = BrokenAgent()
    app = _make_worker_tui(agent)
    app._append_message = MagicMock()
    app._streaming_text = ""
    app._streaming_markdown = None
    app._run_reservation_lock = threading.Lock()
    app._run_reserved = False

    assert app._try_dispatch_plan("/plan first") is True
    for _ in range(50):
        if not app._run_reserved:
            break
        threading.Event().wait(0.05)
    assert app._run_reserved is False

    assert app._try_dispatch_plan("/plan second") is True
    for _ in range(50):
        if agent.calls >= 2 and not app._run_reserved:
            break
        threading.Event().wait(0.05)
    assert agent.calls == 2


def test_tui_cleanup_warning_preserves_terminal_on_failure(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "plan_cleanup_warning"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)
    app = _make_worker_tui(agent)
    app._run_reservation_lock = threading.Lock()
    app._run_reserved = True

    def broken_rmtree(path):
        raise OSError("permission denied")

    monkeypatch.setattr("zero_agent.frontends.tui.shutil.rmtree", broken_rmtree)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app._worker_run(
            "cleanup warning",
            initial_mode=TaskMode.PLAN,
            plan_path=str(plan_path),
            workspace_directory=str(workspace),
        )

    assert any("failed to clean unadopted plan workspace" in str(w.message) for w in caught)
    terminals = [
        item for item in list(app._chunk_queue.queue)
        if isinstance(item, dict) and item.get("type") == "terminal"
    ]
    assert terminals[-1]["status"] == TerminalStatus.COMPLETED.value
    assert app._run_reserved is False

def test_tui_cleanup_warning_as_error_preserves_terminal(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "plan_cleanup_warning_error"
    workspace.mkdir()
    plan_path = workspace / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")

    agent = _RecordingAgent(tmp_path)
    app = _make_worker_tui(agent)
    app._run_reservation_lock = threading.Lock()
    app._run_reserved = True

    def broken_rmtree(path):
        raise OSError("permission denied")

    monkeypatch.setattr("zero_agent.frontends.tui.shutil.rmtree", broken_rmtree)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        app._worker_run(
            "cleanup warning error",
            initial_mode=TaskMode.PLAN,
            plan_path=str(plan_path),
            workspace_directory=str(workspace),
        )

    terminals = [
        item for item in list(app._chunk_queue.queue)
        if isinstance(item, dict) and item.get("type") == "terminal"
    ]
    assert terminals[-1]["status"] == TerminalStatus.COMPLETED.value
    assert terminals[-1]["data"] == {
        "cleanup_warning": (
            f"failed to clean unadopted plan workspace {str(workspace)!r}: "
            "permission denied"
        )
    }
    assert app._run_reserved is False


# ── TUI ready-state notification ───────────────────────────────────────


def test_tui_ready_notification_shows_execute_hint(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [x] one\n- [x] two\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path,
        mode=TaskMode.PLAN,
        plan_path=str(plan_file),
        user_request="do it",
        certificate=_plan_certificate(),
    )
    app = _make_fake_tui(agent)
    app._maybe_notify_plan_ready()
    app._append_message.assert_called_once()
    role, text = app._append_message.call_args[0]
    assert role == "system"
    assert "/plan execute" in text


def test_tui_ready_notification_only_once(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [x] one\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path,
        mode=TaskMode.PLAN,
        plan_path=str(plan_file),
        user_request="do it",
        certificate=_plan_certificate(),
    )
    app = _make_fake_tui(agent)
    app._maybe_notify_plan_ready()
    app._maybe_notify_plan_ready()
    assert app._append_message.call_count == 1


def test_tui_not_ready_no_notification(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] pending\n", encoding="utf-8")
    agent = _RecordingAgent(
        tmp_path, mode=TaskMode.PLAN, plan_path=str(plan_file), user_request="do it"
    )
    app = _make_fake_tui(agent)
    app._maybe_notify_plan_ready()
    app._append_message.assert_not_called()


# ── TUI duplicate-user-message guard ───────────────────────────────────


def test_tui_plan_task_suppresses_duplicate_user_append(tmp_path) -> None:
    agent = _RecordingAgent(tmp_path)
    app = _make_fake_tui(agent)
    assert app._try_dispatch_plan("/plan build it") is True
    # Raw slash shown exactly once; the underlying prompt is suppressed.
    app._append_message.assert_called_once_with("user", "/plan build it")
    kwargs = app._run_prompt.call_args.kwargs
    assert kwargs["display_user"] is False
    assert os.path.isdir(kwargs["workspace_directory"])


def test_tui_run_prompt_display_user_flag(tmp_path) -> None:
    import queue as _queue
    import threading as _threading
    from unittest.mock import MagicMock
    from zero_agent.frontends.tui import ZeroAgentTui

    def make_app():
        app = object.__new__(ZeroAgentTui)
        app.agent = _RecordingAgent(tmp_path)
        app._append_message = MagicMock()
        app._streaming_text = ""
        app._streaming_markdown = None
        app._abort_flag = _threading.Event()
        app._run_reservation_lock = _threading.Lock()
        app._run_reserved = False
        app._worker_run = MagicMock()
        return app

    # Default: display_user=True appends the prompt.
    app = make_app()
    app._run_prompt("hello")
    app._append_message.assert_called_once_with("user", "hello")

    # display_user=False: no user message appended.
    app = make_app()
    app._run_prompt("hello", display_user=False)
    app._append_message.assert_not_called()
