"""Tests for the desktop bridge ZeroAgent contract."""

from __future__ import annotations

import inspect
import asyncio
import json
import queue
from pathlib import Path

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
    assert ("GET", "/ws") in routes


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

    assert "webbrowser.open(url)" in bridge_source
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
    class DummyContinue:
        @staticmethod
        def set_sessions_dir(_path):
            pass

        @staticmethod
        def list_sessions(exclude_pid=None):
            return [
                (str(tmp_path / f"session-{idx}.json"), 1000 + idx, f"preview {idx}", idx)
                for idx in range(12)
            ]

    manager = desktop_bridge.AgentManager()
    monkeypatch.setattr(manager, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(manager, "ensure_project_import_path", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "zero_agent.bots.shared.continue_cmd", DummyContinue)

    sessions = manager.list_resume_sessions()

    assert len(sessions) == 10
    assert sessions[0]["index"] == 1
    assert sessions[-1]["index"] == 10


def test_run_agent_turn_keeps_cumulative_partial_once() -> None:
    class DummyRunner:
        def __init__(self):
            self.prompts = []

        def put_task(self, prompt, images=None):
            self.prompts.append((prompt, images))
            out = queue.Queue()
            out.put({"next": "Hel"})
            out.put({"next": "Hello"})
            out.put({"done": "Hello"})
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


def test_run_agent_turn_preserves_incremental_runner_chunks() -> None:
    class IncrementalRunner:
        inc_out = True

        def put_task(self, prompt, images=None):
            out = queue.Queue()
            out.put({"next": "Hel"})
            out.put({"next": "lo"})
            out.put({"done": "Hello"})
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
