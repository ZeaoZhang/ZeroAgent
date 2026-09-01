"""Deterministic tests for bridge-owned WeChat QR onboarding."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from zero_agent.bots.wechat_app import WxBotClient
from zero_agent.frontends import channel_onboarding
from zero_agent.frontends.channel_onboarding import WechatQrManager


class FakeWechatClient:
    instances = []

    def __init__(self, statuses=None, token_file=None):
        self.statuses = list(statuses or [{"status": "pending"}])
        self.token_file = Path(token_file) if token_file else None
        self.qr_requests = 0
        self.saved = []
        self.__class__.instances.append(self)

    def request_qr(self):
        self.qr_requests += 1
        return "qr-1", "https://ilink.example/qr/qr-1"

    def get_qr_status(self, _qr_id):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def save_login_result(self, result):
        self.saved.append(dict(result))
        if self.token_file:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(json.dumps(result), encoding="utf-8")


class FailingSaveClient(FakeWechatClient):
    def save_login_result(self, _result):
        raise OSError("token replacement failed")


def test_wechat_client_saves_minimal_token_file_with_private_mode(tmp_path):
    token_file = tmp_path / "token.json"
    client = WxBotClient(token_file=token_file)

    client.save_login_result({
        "bot_token": "saved-token",
        "ilink_bot_id": "bot-1",
        "get_updates_buf": "buffer",
        "unexpected": "discarded",
    })

    assert json.loads(token_file.read_text(encoding="utf-8")) == {
        "bot_token": "saved-token",
        "ilink_bot_id": "bot-1",
        "updates_buf": "buffer",
    }
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_qr_manager_returns_data_url_and_never_returns_token(monkeypatch):
    manager = WechatQrManager(client_factory=FakeWechatClient)
    try:
        started = manager.start()
        status = manager.status(started["sessionId"])

        assert started["status"] == "pending"
        assert status["qrDataUrl"].startswith("data:image/png;base64,")
        assert "bot_token" not in json.dumps(started)
        assert "bot_token" not in json.dumps(status)
    finally:
        manager.close()


def test_confirmed_qr_saves_token_and_reports_configured(tmp_path):
    token_file = tmp_path / "token.json"
    client = FakeWechatClient(
        statuses=[{
            "status": "confirmed",
            "bot_token": "saved-token",
            "ilink_bot_id": "bot-1",
        }],
        token_file=token_file,
    )
    manager = WechatQrManager(client_factory=lambda: client, token_file=token_file)
    try:
        session = manager.start()
        manager.wait_for_worker(session["sessionId"])

        result = manager.status(session["sessionId"])
        assert result["status"] == "confirmed"
        assert json.loads(token_file.read_text(encoding="utf-8"))["ilink_bot_id"] == "bot-1"
        assert "saved-token" not in json.dumps(result)
    finally:
        manager.close()


def test_qr_manager_reports_expired_and_cancelled_sessions():
    expired = WechatQrManager(
        client_factory=lambda: FakeWechatClient(statuses=[{"status": "expired"}])
    )
    try:
        session = expired.start()
        expired.wait_for_worker(session["sessionId"])
        assert expired.status(session["sessionId"])["status"] == "expired"
    finally:
        expired.close()

    cancelled = WechatQrManager(client_factory=FakeWechatClient)
    try:
        session = cancelled.start()
        result = cancelled.cancel(session["sessionId"])
        assert result["status"] == "cancelled"
        assert cancelled.status(session["sessionId"])["status"] == "cancelled"
    finally:
        cancelled.close()


def test_qr_manager_reuses_one_pending_session():
    FakeWechatClient.instances.clear()
    manager = WechatQrManager(client_factory=FakeWechatClient)
    try:
        first = manager.start()
        second = manager.start()

        assert second["sessionId"] == first["sessionId"]
        assert len(FakeWechatClient.instances) == 1
        assert FakeWechatClient.instances[0].qr_requests == 1
    finally:
        manager.close()


def test_qr_manager_handles_missing_qrcode_dependency(monkeypatch):
    monkeypatch.setattr(channel_onboarding, "qrcode", None)
    manager = WechatQrManager(client_factory=FakeWechatClient)
    try:
        started = manager.start()
        assert started["status"] == "failed"
        assert "pip install -e '.[bots]'" in started["error"]
        assert "bot_token" not in json.dumps(started)
    finally:
        manager.close()


def test_qr_manager_reports_token_file_failure_without_credentials(tmp_path):
    manager = WechatQrManager(
        client_factory=lambda: FailingSaveClient(
            statuses=[{
                "status": "confirmed",
                "bot_token": "secret-token",
                "ilink_bot_id": "bot-1",
            }],
            token_file=tmp_path / "token.json",
        ),
        token_file=tmp_path / "token.json",
    )
    try:
        session = manager.start()
        manager.wait_for_worker(session["sessionId"])
        result = manager.status(session["sessionId"])
        assert result["status"] == "failed"
        assert "secret-token" not in json.dumps(result)
        assert "bot_token" not in json.dumps(result)
    finally:
        manager.close()


def test_qr_manager_rejects_unknown_sessions():
    manager = WechatQrManager(client_factory=FakeWechatClient)
    try:
        with pytest.raises(KeyError):
            manager.status("missing")
        with pytest.raises(KeyError):
            manager.cancel("missing")
    finally:
        manager.close()
