"""Telegram allow-list and group-chat gate regressions."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def _telegram(pytest_importorskip=True):
    pytest.importorskip("telegram")
    return importlib.import_module("zero_agent.bots.telegram_app")


def _message(text: str = "hello"):
    sent: list[str] = []

    async def reply_text(body):
        sent.append(body)
        return None

    message = SimpleNamespace(text=text, reply_text=reply_text)
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=1001),
        effective_chat=SimpleNamespace(type="private"),
    )
    return update, sent


def _patch_runner(monkeypatch, module):
    put_calls: list[str] = []

    def put_task(prompt, **_kwargs):
        put_calls.append(prompt)
        return object()

    class FakeAsyncio:
        @staticmethod
        def create_task(coro):
            coro.close()
            return None

    monkeypatch.setattr(module.runner, "put_task", put_task)
    monkeypatch.setattr(module, "asyncio", FakeAsyncio())
    return put_calls


@pytest.fixture
def telegram_module(monkeypatch, tmp_path: Path):
    """Reload the adapter with a controlled bot config."""
    config = tmp_path / "bot-config.yaml"
    config.write_text(
        "bots:\n  tg_bot_token: token\n  tg_allowed_users: [1001]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZA_BOT_CONFIG_PATH", str(config))
    monkeypatch.delenv("TG_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    module = _telegram()
    importlib.reload(module)
    return module


def test_allow_list_is_normalized_to_strings(telegram_module) -> None:
    """YAML ints must still match int user ids from Telegram updates."""
    assert telegram_module.ALLOWED == {"1001"}
    assert telegram_module._unauthorized(1001) is False
    assert telegram_module._unauthorized(2002) is True


def test_whitelisted_user_reaches_the_runner(telegram_module, monkeypatch) -> None:
    put_calls = _patch_runner(monkeypatch, telegram_module)
    update, sent = _message("hi")

    asyncio.run(telegram_module.handle_msg(update, SimpleNamespace(user_data={})))

    assert put_calls == ["hi"]
    assert sent == []


def test_unknown_user_is_rejected(telegram_module, monkeypatch) -> None:
    put_calls = _patch_runner(monkeypatch, telegram_module)
    update, sent = _message("hi")
    update.effective_user.id = 2002

    asyncio.run(telegram_module.handle_msg(update, SimpleNamespace(user_data={})))

    assert put_calls == []
    assert sent == ["no"]


def test_group_chat_is_rejected_by_default(telegram_module, monkeypatch) -> None:
    put_calls = _patch_runner(monkeypatch, telegram_module)
    monkeypatch.delenv("ZA_ALLOW_GROUP_CHATS", raising=False)
    update, sent = _message("hi")
    update.effective_chat.type = "group"

    asyncio.run(telegram_module.handle_msg(update, SimpleNamespace(user_data={})))

    assert put_calls == []
    assert sent == []


def test_group_chat_runs_when_explicitly_allowed(telegram_module, monkeypatch) -> None:
    put_calls = _patch_runner(monkeypatch, telegram_module)
    monkeypatch.setenv("ZA_ALLOW_GROUP_CHATS", "1")
    update, _sent = _message("hi")
    update.effective_chat.type = "group"

    asyncio.run(telegram_module.handle_msg(update, SimpleNamespace(user_data={})))

    assert put_calls == ["hi"]


def test_explicit_star_publishes_the_bot(tmp_path, monkeypatch) -> None:
    config = tmp_path / "bot-config.yaml"
    config.write_text(
        "bots:\n  tg_bot_token: token\n  tg_allowed_users: ['*']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZA_BOT_CONFIG_PATH", str(config))
    monkeypatch.delenv("TG_ALLOWED_USERS", raising=False)
    module = _telegram()
    importlib.reload(module)
    put_calls = _patch_runner(monkeypatch, module)
    update, sent = _message("hi")
    update.effective_user.id = 999999

    asyncio.run(module.handle_msg(update, SimpleNamespace(user_data={})))

    assert module.ALLOWED == {"*"}
    assert put_calls == ["hi"]
    assert sent == []
