"""Bridge-owned WeChat QR login sessions."""

from __future__ import annotations

import base64
import io
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from zero_agent.bots.wechat_app import TOKEN_FILE, WxBotClient

try:
    import qrcode as _qrcode
except ImportError:
    _qrcode = None  # type: ignore[assignment]


qrcode = _qrcode


_TERMINAL_STATUSES = frozenset({"confirmed", "expired", "failed", "cancelled"})
_INSTALL_HINT = "pip install -e '.[bots]'"


class _QrSession:
    def __init__(self, session_id: str, client=None, qr_id: str = "", qr_data_url: str = ""):
        self.session_id = session_id
        self.client = client
        self.qr_id = qr_id
        self.qr_data_url = qr_data_url
        self.status = "pending"
        self.error = ""
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None


class WechatQrManager:
    """Own one short-lived QR login worker and redact login credentials."""

    def __init__(self, client_factory=WxBotClient, token_file=TOKEN_FILE):
        self._client_factory = client_factory
        self._token_file = Path(token_file)
        self._lock = threading.RLock()
        self._session: _QrSession | None = None

    def _new_client(self):
        if self._client_factory is WxBotClient:
            return self._client_factory(token_file=self._token_file)
        return self._client_factory()

    @staticmethod
    def _data_url(url: str) -> str:
        image = qrcode.make(url)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    @staticmethod
    def _error_text(prefix: str, error: BaseException) -> str:
        return f"{prefix} ({type(error).__name__})"

    def _public_snapshot(self, session: _QrSession) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "sessionId": session.session_id,
            "status": session.status,
        }
        if session.qr_data_url and session.status not in {"failed", "cancelled"}:
            snapshot["qrDataUrl"] = session.qr_data_url
        if session.error:
            snapshot["error"] = session.error
        if session.status == "confirmed":
            snapshot["configured"] = True
        return snapshot

    def _set_failed(self, session: _QrSession, error: str) -> None:
        with self._lock:
            if self._session is not session:
                return
            session.status = "failed"
            session.error = error
            session.stop_event.set()

    def start(self) -> dict:
        with self._lock:
            if (
                self._session
                and self._session.status in {"pending", "scanned"}
                and not self._session.stop_event.is_set()
            ):
                return self._public_snapshot(self._session)
            session = _QrSession(uuid.uuid4().hex)
            self._session = session

        if qrcode is None:
            self._set_failed(session, f"QR image support unavailable; install {_INSTALL_HINT}")
            return self._public_snapshot(session)

        try:
            client = self._new_client()
            qr_id, qr_url = client.request_qr()
            qr_id = str(qr_id or "").strip()
            qr_url = str(qr_url or "").strip()
            if not qr_id or not qr_url:
                raise ValueError("QR response is incomplete")
            qr_data_url = self._data_url(qr_url)
        except Exception as exc:
            self._set_failed(session, self._error_text("QR initialization failed", exc))
            return self._public_snapshot(session)

        with self._lock:
            if self._session is not session or session.stop_event.is_set():
                return self._public_snapshot(session)
            session.client = client
            session.qr_id = qr_id
            session.qr_data_url = qr_data_url
            session.worker = threading.Thread(
                target=self._poll,
                args=(session,),
                name="wechat-qr-worker",
                daemon=True,
            )
            session.worker.start()
            return self._public_snapshot(session)

    def _poll(self, session: _QrSession) -> None:
        while not session.stop_event.is_set():
            try:
                result = session.client.get_qr_status(session.qr_id)
                if not isinstance(result, Mapping):
                    raise ValueError("QR status response is invalid")
                raw_status = str(result.get("status") or "").strip().lower()
                status = {
                    "scaned": "scanned",
                    "scanned": "scanned",
                    "pending": "pending",
                    "confirmed": "confirmed",
                    "expired": "expired",
                }.get(raw_status)
                if status is None:
                    status = "pending"
                if status == "confirmed":
                    try:
                        session.client.save_login_result(result)
                    except Exception as exc:
                        self._set_failed(
                            session,
                            self._error_text("QR token save failed", exc),
                        )
                        return
                with self._lock:
                    if self._session is not session or session.stop_event.is_set():
                        return
                    session.status = status
                    if status in _TERMINAL_STATUSES:
                        session.stop_event.set()
                        return
            except Exception as exc:
                self._set_failed(session, self._error_text("QR status polling failed", exc))
                return
            if session.stop_event.wait(2.0):
                return

    def _get_session(self, session_id: str) -> _QrSession:
        with self._lock:
            session = self._session
            if session is None or session.session_id != session_id:
                raise KeyError(session_id)
            return session

    def status(self, session_id: str) -> dict:
        session = self._get_session(session_id)
        with self._lock:
            return self._public_snapshot(session)

    def cancel(self, session_id: str) -> dict:
        session = self._get_session(session_id)
        with self._lock:
            if session.status not in _TERMINAL_STATUSES:
                session.status = "cancelled"
                session.stop_event.set()
            return self._public_snapshot(session)

    def wait_for_worker(self, session_id: str, timeout: float = 2.0) -> None:
        session = self._get_session(session_id)
        worker = session.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)

    def close(self) -> None:
        with self._lock:
            session = self._session
            if session is not None:
                session.stop_event.set()
            worker = session.worker if session is not None else None
        if worker is not None and worker is not threading.current_thread():
            worker.join(2.0)


__all__ = ["WechatQrManager"]
