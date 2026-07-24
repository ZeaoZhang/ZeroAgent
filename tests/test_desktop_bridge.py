"""Tests for the desktop bridge ZeroAgent contract."""

from __future__ import annotations

import inspect
import asyncio
import json
import queue
from pathlib import Path

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.frontends import desktop_bridge


def test_desktop_bridge_source_uses_zeroagent_entrypoint() -> None:
    source = inspect.getsource(desktop_bridge)
    assert ("agent" + "main") not in source
    assert ("za" + "_adapter") not in source


def test_web_frontend_folds_tool_markers() -> None:
    root = Path(__file__).resolve().parents[1] / "zero_agent" / "frontends"
    app_source = (root / "desktop" / "static" / "app.js").read_text(encoding="utf-8")

    assert r"^TURN\s+\d+\s*:\s*TOOL:" in app_source
    assert "return { kind: 'TOOL_CALL'" in app_source
    assert "return kind !== 'agent_message_chunk';" in app_source
    assert "stripVisibleToolProtocol" in app_source


def test_status_payload_exposes_zeroagent_fields() -> None:
    manager = desktop_bridge.AgentManager()
    assert manager.workspace_dir
    assert manager.config_path


def test_agent_manager_uses_configured_workspace_and_sessions(monkeypatch, tmp_path) -> None:
    config = AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="sk-test",
                api_base="https://api.openai.com/v1",
                model="gpt-test",
            ),
        },
        workspace_dir=str(tmp_path / "workspace"),
        memory_dir=str(tmp_path / "memory"),
        sessions_dir=str(tmp_path / "workspace" / "sessions"),
    )
    monkeypatch.setattr(desktop_bridge, "load_default_config", lambda: config)

    manager = desktop_bridge.AgentManager()

    assert manager.workspace_dir == str(tmp_path / "workspace")
    assert manager.sessions_dir == str(tmp_path / "workspace" / "sessions")
    assert manager.config["workspace_dir"] == str(tmp_path / "workspace")
    assert "api_key" not in manager.config["llm_backends"]["default"]


def test_model_profiles_come_from_agent_runner(monkeypatch) -> None:
    expected = [{
        "index": 0,
        "llmNo": 0,
        "id": "default",
        "name": "default",
        "model": "model",
        "displayName": "default/model",
        "active": True,
    }]

    class DummyRunner:
        def __init__(self, _agent):
            pass

        def list_llm_profiles(self):
            return expected

    monkeypatch.setattr(desktop_bridge, "ZeroAgent", lambda *args, **kwargs: object())
    monkeypatch.setattr(desktop_bridge, "AgentRunner", DummyRunner)

    assert desktop_bridge.AgentManager().list_model_profiles() == expected


def test_create_app_exposes_desktop_http_contract() -> None:
    app = desktop_bridge.create_app()
    routes = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }

    assert ("GET", "/status") in routes
    assert ("GET", "/config") in routes
    assert ("POST", "/config") in routes
    assert ("GET", "/model-profiles") in routes
    assert ("GET", "/slash/commands") in routes
    assert ("POST", "/slash/resolve") in routes
    assert ("GET", "/history/sessions") in routes
    assert ("POST", "/history/resume") in routes
    assert ("GET", "/sessions") in routes
    assert ("POST", "/session/new") in routes
    assert ("GET", "/session/{sid}") in routes
    assert ("DELETE", "/session/{sid}") in routes
    assert ("POST", "/session/{sid}/prompt") in routes
    assert ("GET", "/session/{sid}/messages") in routes
    assert ("POST", "/session/{sid}/cancel") in routes
    assert ("POST", "/session/{sid}/model") in routes
    assert ("POST", "/session/{sid}/group") in routes
    assert ("POST", "/session/{sid}/agents/{aid}/cancel") in routes
    assert ("GET", "/ws") in routes



def _bridge_security(token: str = "secret", origin: str = "http://127.0.0.1:14168") -> desktop_bridge.BridgeSecurity:
    return desktop_bridge.BridgeSecurity(
        host="127.0.0.1",
        port=14168,
        token=token,
        token_explicit=True,
        allowed_origins=frozenset({origin}),
        allow_remote=False,
    )


async def _open_test_client(security: desktop_bridge.BridgeSecurity | None = None) -> TestClient:
    client = TestClient(TestServer(desktop_bridge.create_app(security=security or _bridge_security())))
    await client.start_server()
    return client


def test_load_bridge_security_generates_token_when_absent(monkeypatch) -> None:
    monkeypatch.delenv("ZA_DESKTOP_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("ZA_DESKTOP_BRIDGE_ALLOWED_ORIGINS", "https://desktop.example/")
    monkeypatch.delenv("ZA_DESKTOP_BRIDGE_ALLOW_REMOTE", raising=False)
    monkeypatch.setattr(desktop_bridge.secrets, "token_urlsafe", lambda size: f"generated-{size}")

    security = desktop_bridge.load_bridge_security("127.0.0.1", 14168)

    assert security.token == "generated-32"
    assert security.token_explicit is False
    assert "http://127.0.0.1:14168" in security.allowed_origins
    assert "http://localhost:14168" in security.allowed_origins
    assert "https://desktop.example" in security.allowed_origins


def test_load_bridge_security_remote_host_requires_allow_flag_and_explicit_token(monkeypatch) -> None:
    monkeypatch.delenv("ZA_DESKTOP_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("ZA_DESKTOP_BRIDGE_ALLOW_REMOTE", raising=False)

    with pytest.raises(ValueError):
        desktop_bridge.load_bridge_security("0.0.0.0", 14168)

    monkeypatch.setenv("ZA_DESKTOP_BRIDGE_ALLOW_REMOTE", "1")
    with pytest.raises(ValueError):
        desktop_bridge.load_bridge_security("0.0.0.0", 14168)

    monkeypatch.setenv("ZA_DESKTOP_BRIDGE_TOKEN", "explicit-token")
    security = desktop_bridge.load_bridge_security("0.0.0.0", 14168)

    assert security.allow_remote is True
    assert security.token == "explicit-token"
    assert security.token_explicit is True


def test_desktop_bridge_public_status_is_constrained_and_anonymous() -> None:
    async def run() -> None:
        client = await _open_test_client()
        try:
            async with client.get("/status") as resp:
                payload = await resp.json()
                assert resp.status == 200
                assert set(payload) == {"ok", "running", "ready", "authRequired"}
                assert payload == {"ok": True, "running": True, "ready": True, "authRequired": True}
            async with client.get("/") as resp:
                assert resp.status == 200
        finally:
            await client.close()

    asyncio.run(run())


def test_desktop_bridge_api_requires_token() -> None:
    async def run() -> None:
        client = await _open_test_client()
        try:
            async with client.get("/config") as resp:
                assert resp.status == 401
            async with client.get("/config", headers={"Authorization": "Bearer wrong"}) as resp:
                assert resp.status == 401
            async with client.get(
                "/config",
                headers={"Authorization": "Bearer wrong", "X-ZA-Desktop-Token": "secret"},
            ) as resp:
                assert resp.status == 200
        finally:
            await client.close()

    asyncio.run(run())


def test_desktop_bridge_origin_is_checked_before_token() -> None:
    async def run() -> None:
        client = await _open_test_client()
        try:
            async with client.get(
                "/config",
                headers={"Origin": "http://evil.invalid", "Authorization": "Bearer secret"},
            ) as resp:
                assert resp.status == 403
                assert "Access-Control-Allow-Origin" not in resp.headers
        finally:
            await client.close()

    asyncio.run(run())


def test_desktop_bridge_allows_token_and_exact_origin_cors() -> None:
    origin = "http://127.0.0.1:14168"

    async def run() -> None:
        client = await _open_test_client(_bridge_security(origin=origin))
        try:
            async with client.get(
                "/config",
                headers={"Origin": origin, "Authorization": "Bearer secret"},
            ) as resp:
                payload = await resp.json()
                assert resp.status == 200
                assert payload["workspaceDir"]
                assert resp.headers["Access-Control-Allow-Origin"] == origin
                assert resp.headers["Access-Control-Allow-Origin"] != "*"
            async with client.get(
                "/sessions",
                headers={"Origin": origin, "X-ZA-Desktop-Token": "secret"},
            ) as resp:
                payload = await resp.json()
                assert resp.status == 200
                assert "sessions" in payload
                assert resp.headers["Access-Control-Allow-Origin"] == origin
        finally:
            await client.close()

    asyncio.run(run())


def test_desktop_bridge_options_returns_allowed_cors() -> None:
    origin = "http://127.0.0.1:14168"

    async def run() -> None:
        client = await _open_test_client(_bridge_security(origin=origin))
        try:
            async with client.options(
                "/config",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
            ) as resp:
                assert resp.status == 204
                assert resp.headers["Access-Control-Allow-Origin"] == origin
                assert resp.headers["Access-Control-Allow-Origin"] != "*"
                assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]
                assert "X-ZA-Desktop-Token" in resp.headers["Access-Control-Allow-Headers"]
        finally:
            await client.close()

    asyncio.run(run())


def test_desktop_bridge_ws_requires_query_token_and_sends_bridge_ready() -> None:
    async def run() -> None:
        client = await _open_test_client()
        try:
            with pytest.raises(WSServerHandshakeError) as exc:
                await client.ws_connect("/ws")
            assert exc.value.status == 401

            ws = await client.ws_connect("/ws?token=secret")
            try:
                payload = await ws.receive_json()
                assert payload["type"] == "bridge-ready"
                assert payload["http"] is True
                assert payload["wsEventsOnly"] is True
            finally:
                await ws.close()
        finally:
            await client.close()

    asyncio.run(run())

def test_web_bridge_and_tauri_share_desktop_static_frontend() -> None:
    root = Path(__file__).resolve().parents[1]
    bridge_source = inspect.getsource(desktop_bridge.create_app)
    tauri_conf = json.loads(
        (root / "zero_agent" / "frontends" / "desktop" / "src-tauri" / "tauri.conf.json")
        .read_text(encoding="utf-8")
    )

    assert 'APP_DIR / "desktop" / "static"' in bridge_source
    assert tauri_conf["build"]["frontendDist"] == "../static"


def test_desktop_bridge_cli_opens_browser_but_tauri_disables_it() -> None:
    root = Path(__file__).resolve().parents[1]
    bridge_source = (root / "zero_agent" / "frontends" / "desktop_bridge.py").read_text(encoding="utf-8")
    tauri_source = (
        root / "zero_agent" / "frontends" / "desktop" / "src-tauri" / "src" / "lib.rs"
    ).read_text(encoding="utf-8")

    assert "webbrowser.open(browser_url)" in bridge_source
    assert "fn stop_owned_bridge()" in tauri_source
    assert "stop_owned_bridge();" in tauri_source
    assert "#token=" in bridge_source
    assert "ZA_DESKTOP_BRIDGE_NO_BROWSER" in bridge_source
    assert '.env("ZA_DESKTOP_BRIDGE_NO_BROWSER", "1")' in tauri_source


def test_desktop_bridge_resolves_zeroagent_mode_prompts() -> None:
    from zero_agent.frontends.desktop_commands import prompt_for

    prompt = prompt_for("/goal", "ship ZA desktop parity")

    assert prompt is not None
    assert "Goal 模式" in prompt
    assert "ship ZA desktop parity" in prompt


def test_desktop_bridge_resolves_init_prompt() -> None:
    from zero_agent.frontends.desktop_commands import PALETTE_ENTRIES, prompt_for

    init_entry = next(entry for entry in PALETTE_ENTRIES if entry[0] == "/init")
    prompt = prompt_for("/init", "browser only")

    assert "身份画像" in init_entry[2]
    assert prompt is not None
    assert "config.yaml" in prompt
    assert "memory/sops/web_setup_sop.md" in prompt
    assert "memory/sops/tmwebdriver_sop.md" in prompt
    assert "自由文本" in prompt
    assert "ask_user" in prompt
    assert "browser only" in prompt


def test_slash_palette_distinguishes_resume_and_continue() -> None:
    from zero_agent.frontends.desktop_commands import PALETTE_ENTRIES

    resume = next(entry for entry in PALETTE_ENTRIES if entry[0] == "/resume")
    response = asyncio.run(desktop_bridge.slash_commands_handler(None))
    payload = json.loads(response.text)
    commands = {entry["cmd"] for entry in payload["commands"]}

    assert "任意历史会话" in resume[2]
    assert "/resume" in commands
    assert "/scheduler" in commands
    assert "/continue" in commands
    assert next(entry for entry in payload["commands"] if entry["cmd"] == "/continue")["argHint"] == ""
    assert next(entry for entry in payload["commands"] if entry["cmd"] == "/continue")["description"] == "继续最近一个历史会话"


def test_resume_session_listing_defaults_to_ten(monkeypatch, tmp_path) -> None:
    configured_sessions_dir = tmp_path / "configured-sessions"
    seen_sessions_dirs = []

    class DummyContinue:
        @staticmethod
        def set_sessions_dir(path):
            seen_sessions_dirs.append(path)

        @staticmethod
        def list_sessions(exclude_pid=None):
            return [
                (str(tmp_path / f"session-{idx}.json"), 1000 + idx, f"preview {idx}", idx)
                for idx in range(12)
            ]

    manager = desktop_bridge.AgentManager()
    monkeypatch.setattr(manager, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(manager, "sessions_dir", str(configured_sessions_dir))
    monkeypatch.setattr(manager, "ensure_project_import_path", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "zero_agent.bots.shared.continue_cmd", DummyContinue)

    sessions = manager.list_resume_sessions()

    assert seen_sessions_dirs == [str(configured_sessions_dir)]
    assert len(sessions) == 10
    assert sessions[0]["index"] == 1
    assert sessions[-1]["index"] == 10


def _terminal(status: str, reason: str, text: str = "", data=None) -> dict:
    return {
        "type": "terminal",
        "status": status,
        "reason": reason,
        "text": text,
        "data": data,
        "turn": 1,
        "source": "agent",
        "certificate": None,
    }


def _run_terminal(status: str, reason: str, text: str = "", data=None):
    class TerminalRunner:
        def put_task(self, prompt, images=None):
            out = queue.Queue()
            out.put(_terminal(status, reason, text, data))
            return out

    manager = desktop_bridge.AgentManager()
    sess = manager.create_session(cwd=manager.workspace_dir)
    sess.agent = TerminalRunner()
    manager.add_message(sess, "user", "hello")
    sess.status = "running"
    sess.partial = {
        "id": sess.msg_seq + 1,
        "role": "assistant",
        "content": "partial",
        "partial": True,
    }
    manager.run_agent_turn(sess, "hello")
    return sess


def test_run_agent_turn_keeps_cumulative_partial_once() -> None:
    class DummyRunner:
        def __init__(self):
            self.prompts = []

        def put_task(self, prompt, images=None):
            self.prompts.append((prompt, images))
            out = queue.Queue()
            out.put({"type": "chunk", "text": "Hel", "source": "agent", "turn": 1})
            out.put({"type": "chunk", "text": "Hello", "source": "agent", "turn": 1})
            out.put(_terminal("completed", "completion_certificate", "Hello"))
            return out

    manager = desktop_bridge.AgentManager()
    sess = manager.create_session(cwd=manager.workspace_dir)
    runner = DummyRunner()
    sess.agent = runner
    manager.add_message(sess, "user", "hello")
    sess.status = "running"
    sess.partial = {
        "id": sess.msg_seq + 1,
        "role": "assistant",
        "content": "",
        "partial": True,
    }

    manager.run_agent_turn(sess, "hello")

    assert runner.prompts == [("hello", [])]
    assert sess.status == "idle"
    assert sess.partial is None
    assert sess.messages[-1]["role"] == "assistant"
    assert sess.messages[-1]["content"] == "Hello"
    assert "HelHello" not in sess.messages[-1]["content"]


def test_run_agent_turn_refreshes_token_usage() -> None:
    class FakeConfig:
        context_window = 12345

    class FakeClient:
        config = FakeConfig()
        _context_window = 12345
        system = "system prompt"
        history = [{"role": "user", "content": "hello"}]
        usage_stats = {
            "total_input_tokens": 111,
            "total_output_tokens": 22,
        }

    class FakeAgent:
        client = FakeClient()

    class TokenRunner:
        _agent = FakeAgent()

        def put_task(self, prompt, images=None):
            out = queue.Queue()
            out.put({"type": "chunk", "text": "Hello", "source": "agent", "turn": 1})
            out.put(_terminal("completed", "completion_certificate", "Hello"))
            return out

    manager = desktop_bridge.AgentManager()
    sess = manager.create_session(cwd=manager.workspace_dir)
    sess.agent = TokenRunner()
    manager.add_message(sess, "user", "hello")
    sess.status = "running"
    sess.partial = {
        "id": sess.msg_seq + 1,
        "role": "assistant",
        "content": "",
        "partial": True,
    }

    manager.run_agent_turn(sess, "hello")

    assert sess.token_usage["input"] == 111
    assert sess.token_usage["output"] == 22
    assert sess.token_usage["total"] > 0
    assert sess.token_usage["limit"] == 12345

def test_run_agent_turn_preserves_incremental_runner_chunks() -> None:
    class IncrementalRunner:
        inc_out = True

        def put_task(self, prompt, images=None):
            out = queue.Queue()
            out.put({"type": "chunk", "text": "Hel", "source": "agent", "turn": 1})
            out.put({"type": "chunk", "text": "lo", "source": "agent", "turn": 1})
            out.put(_terminal("completed", "completion_certificate", "Hello"))
            return out

    manager = desktop_bridge.AgentManager()
    sess = manager.create_session(cwd=manager.workspace_dir)
    sess.agent = IncrementalRunner()
    manager.add_message(sess, "user", "hello")
    sess.status = "running"
    sess.partial = {
        "id": sess.msg_seq + 1,
        "role": "assistant",
        "content": "",
        "partial": True,
    }

    manager.run_agent_turn(sess, "hello")

    assert sess.status == "idle"
    assert sess.partial is None
    assert sess.messages[-1]["content"] == "Hello"


def test_run_agent_turn_waiting_adds_input_required_message() -> None:
    data = {
        "status": "INTERRUPT",
        "intent": "HUMAN_INTERVENTION",
        "data": {"question": "Continue?", "candidates": ["yes", "no"]},
    }

    sess = _run_terminal("waiting", "human_intervention", data=data)

    assert sess.status == "waiting"
    assert sess.terminal_status == "waiting"
    assert sess.terminal_reason == "human_intervention"
    assert sess.partial is None
    message = sess.messages[-1]
    assert message["role"] == "system"
    assert message["content"] == "Continue?"
    assert message["kind"] == "input_required"
    assert message["candidates"] == ["yes", "no"]


def test_run_agent_turn_budget_exhausted_is_error_with_reason() -> None:
    sess = _run_terminal("budget_exhausted", "max_turns", "Task did not complete")

    assert sess.status == "error"
    assert sess.last_error == "max_turns"
    assert sess.terminal_status == "budget_exhausted"
    assert sess.terminal_reason == "max_turns"
    assert sess.messages[-1]["role"] == "error"


def test_run_agent_turn_failed_is_error_with_reason() -> None:
    sess = _run_terminal("failed", "RuntimeError", "Operation failed")

    assert sess.status == "error"
    assert sess.last_error == "RuntimeError"
    assert sess.terminal_status == "failed"
    assert sess.terminal_reason == "RuntimeError"


def test_run_agent_turn_protocol_error_is_error_with_reason() -> None:
    sess = _run_terminal("protocol_error", "invalid_step_outcome", "Protocol error")

    assert sess.status == "error"
    assert sess.last_error == "invalid_step_outcome"
    assert sess.terminal_status == "protocol_error"


def test_run_agent_turn_cancelled_clears_partial_without_assistant_completion() -> None:
    sess = _run_terminal("cancelled", "user_cancelled", "partial")

    assert sess.status == "cancelled"
    assert sess.partial is None
    assert sess.terminal_status == "cancelled"
    assert sess.terminal_reason == "user_cancelled"
    assert all(message["role"] != "assistant" for message in sess.messages)


def test_emit_session_state_includes_terminal_details(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(desktop_bridge.hub, "emit", emitted.append)
    sess = desktop_bridge.Session(
        id="session-1",
        status="error",
        terminal_status="budget_exhausted",
        terminal_reason="max_turns",
    )

    desktop_bridge.emit_session_state(sess, "error")

    assert emitted[-1]["terminalStatus"] == "budget_exhausted"
    assert emitted[-1]["reason"] == "max_turns"


def test_cancel_marks_session_cancelled_and_aborts_runner() -> None:
    class AbortableRunner:
        def __init__(self):
            self.aborted = False

        def abort(self):
            self.aborted = True

    manager = desktop_bridge.AgentManager()
    sess = manager.create_session(cwd=manager.workspace_dir)
    runner = AbortableRunner()
    sess.agent = runner
    sess.status = "running"
    sess.partial = {"id": 1, "role": "assistant", "content": "", "partial": True}

    result = manager.cancel(sess.id)

    assert result == {"ok": True, "sessionId": sess.id}
    assert runner.aborted is True
    assert sess.status == "cancelled"
    assert sess.partial is None
    assert sess.terminal_status == "cancelled"
    assert sess.terminal_reason == "user_cancelled"
