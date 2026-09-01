"""Tests for channel definitions, link state, and desktop lifecycle helpers."""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

from zero_agent.bots import channel_control


def test_channel_definitions_are_stable():
    assert [item.id for item in channel_control.channel_definitions()] == [
        "wechat",
        "wecom",
        "dingtalk",
        "qq",
        "feishu",
        "telegram",
        "discord",
    ]
    assert channel_control.channel_definition("telegram").required_keys == (
        "tg_bot_token",
        "tg_allowed_users",
    )


def test_link_state_defaults_to_true_and_round_trips(monkeypatch, tmp_path):
    path = tmp_path / "channels.json"
    monkeypatch.setenv("ZA_CHANNEL_SETTINGS_PATH", str(path))

    assert channel_control.is_channel_linked("telegram") is True

    channel_control.set_channel_linked("telegram", False)

    assert channel_control.is_channel_linked("telegram") is False
    assert channel_control.get_channel_settings()["telegram"] == {"linked": False}
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "telegram": {"linked": False},
    }


def test_relative_link_state_path_is_project_relative(monkeypatch):
    monkeypatch.setenv("ZA_CHANNEL_SETTINGS_PATH", "nested/channel-settings.json")

    assert channel_control.channel_settings_path() == (
        channel_control._PROJECT_ROOT / "nested" / "channel-settings.json"
    )


def test_corrupt_link_state_fails_open(monkeypatch, tmp_path):
    path = tmp_path / "channels.json"
    path.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("ZA_CHANNEL_SETTINGS_PATH", str(path))

    assert channel_control.is_channel_linked("telegram") is True
    assert channel_control.get_channel_settings() == {}


def test_unknown_channel_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ZA_CHANNEL_SETTINGS_PATH", str(tmp_path / "channels.json"))

    with pytest.raises(KeyError):
        channel_control.set_channel_linked("unknown", False)


def test_required_configuration_is_checked_without_exposing_values():
    telegram = channel_control.channel_definition("telegram")
    assert channel_control.channel_is_configured(
        telegram,
        {"tg_bot_token": "token", "tg_allowed_users": ["1001"]},
    ) is True
    assert channel_control.channel_is_configured(
        telegram,
        {"tg_bot_token": "token", "tg_allowed_users": []},
    ) is False
    assert channel_control.channel_is_configured(telegram, {"tg_bot_token": ""}) is False
    assert channel_control.channel_is_configured(telegram, {}) is False

class _LiveProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


def test_channel_status_includes_configuration_and_link(monkeypatch):
    from zero_agent.frontends import desktop_commands

    monkeypatch.setattr(
        desktop_commands,
        "running_services",
        lambda use_cache=True: {"bots/telegram_app.py": 321},
    )
    monkeypatch.setattr(
        channel_control,
        "get_channel_settings",
        lambda: {"telegram": {"linked": False}},
    )
    monkeypatch.setattr(
        channel_control,
        "channel_is_configured",
        lambda definition, keys=None: True,
    )

    row = next(item for item in desktop_commands.channel_statuses() if item["id"] == "telegram")

    assert row["running"] is True
    assert row["pid"] == 321
    assert row["linked"] is False
    assert row["configured"] is True
    assert "tg_bot_token" in row["requiredKeys"]


def test_start_channel_rejects_missing_configuration(monkeypatch):
    from zero_agent.frontends import desktop_commands

    monkeypatch.setattr(
        channel_control,
        "channel_is_configured",
        lambda definition, keys=None: False,
    )

    ok, message = desktop_commands.start_channel("telegram")

    assert ok is False
    assert "未配置" in message


def test_start_channel_spawns_the_declared_entrypoint(monkeypatch):
    from zero_agent.frontends import desktop_commands

    process = _LiveProcess()
    calls = []
    monkeypatch.setattr(
        channel_control,
        "channel_is_configured",
        lambda definition, keys=None: True,
    )
    monkeypatch.setattr(
        channel_control,
        "missing_configuration",
        lambda definition, keys=None: (),
    )
    monkeypatch.setattr(
        desktop_commands,
        "running_services",
        lambda use_cache=True: {},
    )
    monkeypatch.setattr(
        desktop_commands.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or process,
    )
    monkeypatch.setattr(desktop_commands.time, "sleep", lambda _seconds: None)

    ok, message = desktop_commands.start_channel("telegram")

    assert ok is True, message
    assert "telegram_app.py" in calls[0][0][-1]
    assert "Started" in message


def test_channel_process_scan_uses_exact_script_and_keeps_duplicates(monkeypatch):
    from zero_agent.frontends import desktop_commands

    class FakeProcess:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "name": "Python"}
            self._cmdline = cmdline

        def cmdline(self):
            return self._cmdline

    script = str((desktop_commands._ROOT / "bots" / "telegram_app.py").resolve())
    processes = [
        FakeProcess(101, [sys.executable, script]),
        FakeProcess(102, [sys.executable, script]),
        FakeProcess(103, [sys.executable, f"{script}.backup"]),
        FakeProcess(104, [sys.executable, "/tmp/unrelated.py", script]),
        FakeProcess(105, [sys.executable, "-m", "zero_agent.bots.telegram_app"]),
    ]
    fake_psutil = type(
        "FakePsutil",
        (),
        {"process_iter": staticmethod(lambda _attrs: processes)},
    )()
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert desktop_commands._channel_processes() == {
        "bots/telegram_app.py": [101, 102, 105],
    }


def test_stop_channel_terminates_all_verified_processes(monkeypatch):
    from zero_agent.frontends import desktop_commands

    class FakeProcess:
        def __init__(self, pid, cmdline):
            self.pid = pid
            self._cmdline = cmdline
            self._create_time = float(pid)
            self.terminated = False

        def cmdline(self):
            return self._cmdline

        def create_time(self):
            return self._create_time

        def children(self, recursive=False):
            return []

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    script = str((desktop_commands._ROOT / "bots" / "telegram_app.py").resolve())
    processes = {
        pid: FakeProcess(pid, [sys.executable, script])
        for pid in (201, 202)
    }

    class FakePsutil:
        class NoSuchProcess(Exception):
            pass

        @staticmethod
        def Process(pid):
            return processes[pid]

        @staticmethod
        def wait_procs(items, timeout):
            return items, []

    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)
    monkeypatch.setattr(
        desktop_commands,
        "_channel_processes",
        lambda: {"bots/telegram_app.py": [201, 202]},
    )

    ok, message = desktop_commands.stop_channel("telegram")

    assert ok is True
    assert "pids=201,202" in message
    assert all(process.terminated for process in processes.values())


@pytest.mark.asyncio
async def test_wecom_welcome_is_ignored_when_unlinked(monkeypatch):
    sdk = types.ModuleType("wecom_aibot_sdk")
    sdk.WSClient = type("WSClient", (), {})
    sdk.generate_req_id = lambda _prefix: "request-id"
    monkeypatch.setitem(sys.modules, "wecom_aibot_sdk", sdk)

    module = importlib.import_module("zero_agent.bots.wecom_app")
    monkeypatch.setattr(module, "WELCOME", "welcome")
    monkeypatch.setattr(module, "channel_is_linked", lambda _source: False)

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def reply_welcome(self, *args):
            self.calls.append(args)

    app = object.__new__(module.WeComApp)
    app.client = FakeClient()
    await app.on_enter_chat(object())

    assert app.client.calls == []


@pytest.mark.asyncio
async def test_mixin_ignores_commands_when_unlinked(monkeypatch):
    from zero_agent.bots import common

    class FakeBot(common.AgentBotMixin):
        source = "telegram"

        def __init__(self):
            super().__init__(runner=None, user_tasks={})
            self.sent = []

        async def send_text(self, _chat_id, content, **_ctx):
            self.sent.append(content)

    monkeypatch.setattr(common, "channel_is_linked", lambda _source: False)

    bot = FakeBot()
    await bot.handle_command("chat", "/help")

    assert bot.sent == []
