"""WeChat Bot 前端 for ZeroAgent.

使用微信 iLink API (ilinkai.weixin.qq.com) — QR 码登录, AES 加密 CDN 上传/下载。
适配 ZeroAgent 的 AgentRunner 接口。

Usage:
    python -m zero_agent.bots.wechat_app
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import queue
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TEMP_DIR = os.path.join(_PROJECT_ROOT, "temp")

from zero_agent.core.agent import ZeroAgent
from zero_agent.runners.agent_runner import AgentRunner
from zero_agent.bots.channel_config import ChannelConfigError
from zero_agent.bots.common import load_keys
from zero_agent.bots.common import channel_is_linked
from zero_agent.bots.common import (
    PROMPT_CAPABILITY_FILE_DELIVERY,
    resolve_output_files,
    runner_workspace_dir,
)
from zero_agent.bots.common import terminal_notice
from zero_agent.bots.common import terminal_output_text, terminal_reply_text

_KEYS = {}

# 清除代理环境变量 (避免影响微信长轮询 SSL)
for _k in ("HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_k, None)

API = "https://ilinkai.weixin.qq.com"
TOKEN_FILE = Path.home() / ".wxbot" / "token.json"
TOKEN_FILE.parent.mkdir(exist_ok=True)
VER = "2.1.10"
MSG_USER, MSG_BOT, ITEM_TEXT, STATE_FINISH = 1, 2, 1, 2
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 10
UA = f"openclaw-weixin/{VER}"
ITEM_IMAGE, ITEM_FILE, ITEM_VIDEO = 2, 4, 5
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None  # type: ignore[assignment]

try:
    import qrcode
except ImportError:
    print("Warning: qrcode not installed. QR login images will not render as ASCII.")
    qrcode = None  # type: ignore

# —— Agent setup ——
za = None
runner = None


def _uin():
    return base64.b64encode(str(struct.unpack(">I", os.urandom(4))[0]).encode()).decode()


class AuthExpired(Exception):
    """Bot token expired or invalid (errcode=-14)."""
    pass


class WxBotClient:
    """微信 iLink Bot API 客户端 — QR 登录, 长轮询, 消息/媒体收发."""

    def __init__(self, token=None, token_file=None):
        self._tf = Path(token_file) if token_file else TOKEN_FILE
        self.token = token
        self.bot_id = None
        self._buf = ""
        if not self.token:
            self._load()

    def _load(self):
        if self._tf.exists():
            d = json.loads(self._tf.read_text("utf-8"))
            self.token = d.get("bot_token", "")
            self.bot_id = d.get("ilink_bot_id", "")
            self._buf = d.get("updates_buf", "")

    def _write_token_data(self, data: Mapping[str, str]) -> None:
        self._tf.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._tf.parent,
                prefix=f".{self._tf.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                os.chmod(temporary_name, 0o600)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._tf)
            try:
                os.chmod(self._tf, 0o600)
            except OSError:
                pass
        except Exception:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise

    def _save(self, **kw):
        data = {
            "bot_token": self.token or "",
            "ilink_bot_id": self.bot_id or "",
            "updates_buf": self._buf or "",
            **kw,
        }
        self._write_token_data(data)

    def _post(self, endpoint, body, timeout=15):
        """Call an iLink bot endpoint with the current bot token."""
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(data)),
            "X-WECHAT-UIN": _uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            "User-Agent": UA,
        }
        token = (self.token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.post(
            f"{API}/{endpoint}",
            data=data,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def request_qr(self) -> tuple[str, str]:
        response = requests.get(
            f"{API}/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
            headers={"User-Agent": UA},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return str(data["qrcode"]), str(data.get("qrcode_img_content", "") or "")

    def get_qr_status(self, qr_id: str) -> dict:
        response = requests.get(
            f"{API}/ilink/bot/get_qrcode_status",
            params={"qrcode": qr_id},
            headers={"User-Agent": UA},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def save_login_result(self, result: Mapping) -> None:
        if not isinstance(result, Mapping):
            raise ValueError("invalid QR login result")
        self.token = str(result.get("bot_token") or "")
        self.bot_id = str(result.get("ilink_bot_id") or "")
        updates_buf = result.get("updates_buf", result.get("get_updates_buf", self._buf))
        self._buf = str(updates_buf or "")
        self._write_token_data({
            "bot_token": self.token,
            "ilink_bot_id": self.bot_id,
            "updates_buf": self._buf,
        })

    def login_qr(self, poll_interval=2):
        qr_id, url = self.request_qr()
        print(f"[QR登录] ID: {qr_id}")
        if url and qrcode:
            img = self._tf.parent / "wx_qr.png"
            qrcode.make(url).save(str(img))
            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        last = ""
        while True:
            time.sleep(poll_interval)
            try:
                status = self.get_qr_status(qr_id)
            except requests.exceptions.ReadTimeout:
                continue
            state = status.get("status", "")
            if state != last:
                print(f"  状态: {state}")
                last = state
            if state == "confirmed":
                self.save_login_result(status)
                print(f"[QR登录] 成功! bot_id={self.bot_id}")
                return status
            if state == "expired":
                raise RuntimeError("二维码过期")

    def get_updates(self, timeout=30):
        try:
            resp = self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": self._buf or "", "base_info": {"channel_version": VER}},
                timeout=timeout + 5,
            )
        except requests.exceptions.ReadTimeout:
            return []
        if resp.get("errcode"):
            print(f'[getUpdates] err: {resp.get("errcode")} {resp.get("errmsg","")}')
            if resp["errcode"] == -14:
                self._buf = ""
                self.token = ""
                self.bot_id = ""
                self._save(bot_token="", ilink_bot_id="")
                raise AuthExpired(resp.get("errmsg", ""))
            return []
        nb = resp.get("get_updates_buf", "")
        if nb:
            self._buf = nb
            self._save()
        return resp.get("msgs") or []

    def send_text(self, to_user_id, text, context_token=""):
        msg = {
            "from_user_id": "", "to_user_id": to_user_id,
            "client_id": f"pyclient-{uuid.uuid4().hex[:16]}",
            "message_type": MSG_BOT, "message_state": STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        return self._post("ilink/bot/sendmessage", {
            "msg": msg, "base_info": {"channel_version": VER},
        })

    def send_typing(self, to_user_id, typing_ticket="", cancel=False):
        return self._post("ilink/bot/sendtyping", {
            "ilink_user_id": to_user_id, "typing_ticket": typing_ticket,
            "status": 2 if cancel else 1, "base_info": {"channel_version": VER},
        })

    def get_typing_ticket(self, to_user_id, context_token=""):
        payload = {"ilink_user_id": to_user_id}
        if context_token:
            payload["context_token"] = context_token
        return self._post("ilink/bot/getconfig", payload).get("typing_ticket", "")

    @staticmethod
    def extract_text(msg):
        return "\n".join(
            it["text_item"].get("text", "")
            for it in msg.get("item_list", [])
            if it.get("type") == ITEM_TEXT and it.get("text_item")
        )

    @staticmethod
    def is_user_msg(msg):
        return msg.get("message_type") == MSG_USER

    def _enc(self, raw, aes_key):
        pad = 16 - (len(raw) % 16)
        return AES.new(aes_key, AES.MODE_ECB).encrypt(raw + bytes([pad] * pad))

    def _upload(self, filekey, upload_param, raw, aes_key, timeout=120, upload_url=""):
        url = (
            upload_url.strip() if upload_url
            else f"{CDN_BASE}/upload?encrypted_query_param={quote(upload_param)}&filekey={filekey}"
        )
        data = self._enc(raw, aes_key)
        last_err = None
        for attempt in range(1, 4):
            try:
                r = requests.post(
                    url, data=data,
                    headers={"Content-Type": "application/octet-stream", "User-Agent": UA},
                    timeout=timeout,
                )
                if 400 <= r.status_code < 500:
                    msg = r.headers.get("x-error-message") or r.text[:300]
                    raise RuntimeError(f"CDN upload client error {r.status_code}: {msg}")
                if r.status_code != 200:
                    msg = r.headers.get("x-error-message") or f"status {r.status_code}"
                    raise RuntimeError(f"CDN upload server error: {msg}")
                eq = r.headers.get("x-encrypted-param", "")
                if not eq:
                    raise RuntimeError("CDN upload response missing x-encrypted-param header")
                return {
                    "encrypt_query_param": eq,
                    "aes_key": base64.b64encode(aes_key.hex().encode()).decode(),
                    "encrypt_type": 1,
                }
            except Exception as e:
                last_err = e
                if "client error" in str(e) or attempt >= 3:
                    break
                print(f"[WX] CDN upload retry {attempt}: {e}", file=sys.__stdout__)
        raise last_err

    def _send_media(self, to_user_id, file_path, media_type, item_type, item_key, context_token=""):
        fp = Path(file_path)
        raw = fp.read_bytes()
        filekey = uuid.uuid4().hex
        aes_key = os.urandom(16)
        ciphertext_size = ((len(raw) // 16) + 1) * 16
        thumb_raw = b""
        thumb_w = thumb_h = 0
        thumb_ciphertext_size = 0
        if item_key == "image_item":
            try:
                from io import BytesIO
                from PIL import Image
                im = Image.open(fp)
                im.thumbnail((240, 240))
                thumb_w, thumb_h = im.size
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                bio = BytesIO()
                im.save(bio, format="JPEG", quality=85)
                thumb_raw = bio.getvalue()
                thumb_ciphertext_size = ((len(thumb_raw) // 16) + 1) * 16
            except ImportError:
                pass
        body = {
            "filekey": filekey, "media_type": media_type, "to_user_id": to_user_id,
            "rawsize": len(raw), "rawfilemd5": hashlib.md5(raw).hexdigest(),
            "filesize": ciphertext_size,
            "no_need_thumb": item_key not in ("image_item", "video_item"),
            "aeskey": aes_key.hex(), "base_info": {"channel_version": VER},
        }
        if thumb_raw:
            body.update({
                "thumb_rawsize": len(thumb_raw),
                "thumb_rawfilemd5": hashlib.md5(thumb_raw).hexdigest(),
                "thumb_filesize": thumb_ciphertext_size,
            })
        resp = self._post("ilink/bot/getuploadurl", body)
        upload_param = resp.get("upload_param", "")
        upload_url = resp.get("upload_full_url", "")
        if not (upload_param or upload_url):
            raise RuntimeError(f"getuploadurl failed: {resp}")
        media = self._upload(filekey, upload_param, raw, aes_key=aes_key, upload_url=upload_url)
        item: dict = {"media": media}
        if item_key == "file_item":
            item.update({"file_name": fp.name, "len": str(len(raw))})
        elif item_key == "image_item":
            thumb_param = resp.get("thumb_upload_param", "")
            thumb_url = resp.get("thumb_upload_full_url", "")
            if thumb_param or thumb_url:
                thumb_media = self._upload(
                    filekey, thumb_param, thumb_raw, aes_key=aes_key, upload_url=thumb_url,
                )
                thumb_size = thumb_ciphertext_size
            else:
                thumb_media = media
                thumb_size = ciphertext_size
            item.update({
                "mid_size": ciphertext_size, "thumb_media": thumb_media,
                "thumb_size": thumb_size, "thumb_width": thumb_w, "thumb_height": thumb_h,
            })
        elif item_key == "video_item":
            item.update({"video_size": ciphertext_size})
        msg = {
            "from_user_id": "", "to_user_id": to_user_id,
            "client_id": f"pyclient-{uuid.uuid4().hex[:16]}",
            "message_type": MSG_BOT, "message_state": STATE_FINISH,
            "item_list": [{"type": item_type, item_key: item}],
        }
        if context_token:
            msg["context_token"] = context_token
        return self._post("ilink/bot/sendmessage", {"msg": msg, "base_info": {"channel_version": VER}})

    def send_file(self, to_user_id, file_path, context_token=""):
        return self._send_media(to_user_id, file_path, 3, ITEM_FILE, "file_item", context_token)

    def send_image(self, to_user_id, file_path, context_token=""):
        return self._send_media(to_user_id, file_path, 1, ITEM_IMAGE, "image_item", context_token)

    def send_video(self, to_user_id, file_path, context_token=""):
        return self._send_media(to_user_id, file_path, 2, ITEM_VIDEO, "video_item", context_token)

    def run_loop(self, on_message, poll_timeout=30):
        print(f"[Bot] 监听中... (bot_id={self.bot_id})")
        seen = set()
        while True:
            try:
                for msg in self.get_updates(poll_timeout):
                    mid = msg.get("message_id", 0)
                    if not self.is_user_msg(msg) or mid in seen:
                        continue
                    seen.add(mid)
                    if len(seen) > 5000:
                        seen = set(list(seen)[-2000:])
                    try:
                        on_message(self, msg)
                    except Exception as e:
                        print(f"[Bot] 回调异常: {e}")
            except KeyboardInterrupt:
                print("[Bot] 退出")
                break
            except AuthExpired:
                raise
            except Exception as e:
                print(f"[Bot] 异常: {e}, 5s重试")
                time.sleep(5)


# —— 媒体下载 ——
_MEDIA_KEYS = {"image_item": ".jpg", "video_item": ".mp4", "file_item": "", "voice_item": ".silk"}


def _dl_media(items):
    paths = []
    for item in items:
        for key, ext in _MEDIA_KEYS.items():
            sub = item.get(key)
            if not sub:
                continue
            eq = (sub.get("media") or {}).get("encrypt_query_param")
            if not eq:
                continue
            ak = (sub.get("media") or {}).get("aes_key", "") or sub.get("aeskey", "")
            if not ak:
                continue
            try:
                aes_key = (
                    bytes.fromhex(base64.b64decode(ak).decode())
                    if sub.get("media", {}).get("aes_key") else bytes.fromhex(ak)
                )
                ct = requests.get(
                    f"{CDN_BASE}/download?encrypted_query_param={quote(eq)}",
                    headers={"User-Agent": UA}, timeout=60,
                ).content
                pt = AES.new(aes_key, AES.MODE_ECB).decrypt(ct)
                pt = pt[:-pt[-1]]
                fname = sub.get("file_name") or f"{uuid.uuid4().hex[:8]}{ext or '.bin'}"
                p = os.path.join(_TEMP_DIR, fname)
                with open(p, "wb") as f:
                    f.write(pt)
                paths.append(p)
                print(f"[WX] media saved: {fname}", file=sys.__stdout__)
            except Exception as e:
                print(f"[WX] media dl err ({key}): {e}", file=sys.__stdout__)
            break
    return paths


# —— 文本清理 ——
_TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "tool_use")]
_TAG_PATS.append(r"<file_content>.*?</file_content>")


def _strip_md(t):
    """Filter markdown for WeChat rich-text rendering."""
    def _trunc_code(m):
        full = m.group()
        fence = re.match(r"`{3,}", full).group()
        rest = full[len(fence):-len(fence)]
        if "\n" not in rest:
            return full
        lang_line, _, body = rest.partition("\n")
        lines = body.split("\n")
        if len(lines) > 10:
            return f"{fence}{lang_line}\n" + "\n".join(lines[:10]) + "\n...\n" + fence
        return full
    t = re.sub(r"(`{3,})[\s\S]*?\1", _trunc_code, t)
    t = re.sub(r"!\[.*?\]\(.*?\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", t)
    t = re.sub(r"^#{5,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "• ", t, flags=re.M)
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _clean(t):
    t = re.sub(r"^\s*LLM Running \(Turn \d+\) \.{3}\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*🛠️\s*[A-Za-z_][A-Za-z0-9_]*\(.*$", "", t, flags=re.M)
    for p in _TAG_PATS:
        t = re.sub(p, "", t, flags=re.DOTALL)
    t = re.sub(r"</?summary>", "", t)
    return re.sub(r"\n{3,}", "\n\n", _strip_md(t)).strip()


# —— 消息处理 ——
_task_aborted: dict = {}


def on_message(bot: WxBotClient, msg):
    if not channel_is_linked("wechat"):
        return
    text = bot.extract_text(msg).strip()
    uid = msg.get("from_user_id", "")
    ctx = msg.get("context_token", "")
    media_paths = _dl_media(msg.get("item_list", []))
    if not text and not media_paths:
        return
    if media_paths:
        text = (text + "\n" if text else "") + "\n".join(
            f"[用户发送文件: {p}]" for p in media_paths
        )
    print(f"[WX] 收到: {text[:80]}", file=sys.__stdout__)

    if text in ("/stop", "/abort"):
        runner.abort()
        _task_aborted[uid] = True
        print(f"[WX] /stop set _task_aborted[{uid}]", file=sys.__stdout__)
        return
    if text.startswith("/llm"):
        args = text.split()
        if len(args) > 1:
            try:
                n = int(args[1])
                runner.next_llm(n)
                bot.send_text(uid, f"切换到 [{runner.llm_no}] {runner.get_llm_name()}", context_token=ctx)
            except (ValueError, IndexError):
                bot.send_text(uid, f"用法: /llm <0-{len(runner.list_llms()) - 1}>", context_token=ctx)
        else:
            lines = [
                f"{'→' if cur else '  '} [{i}] {name}"
                for i, name, cur in runner.list_llms()
            ]
            bot.send_text(uid, "LLMs:\n" + "\n".join(lines), context_token=ctx)
        return

    def _handle():
        dq = runner.put_task(
            text,
            source="wechat",
            prompt_capabilities=(PROMPT_CAPABILITY_FILE_DELIVERY,),
        )
        _typing_stop = threading.Event()

        def _keep_typing():
            ticket = bot.get_typing_ticket(uid, ctx)
            if not ticket:
                return
            while not _typing_stop.is_set():
                try:
                    bot.send_typing(uid, ticket)
                except Exception:
                    pass
                _typing_stop.wait(2.0)

        threading.Thread(target=_keep_typing, daemon=True).start()
        result = ""
        terminal = None

        def _wx_send(text):
            s = text.strip()
            t0 = time.time()
            try:
                bot.send_text(uid, s, context_token=ctx)
                print(f"[WX] send ok len={len(s)} dt={time.time() - t0:.1f}s", file=sys.__stdout__)
                return True
            except Exception as e:
                print(
                    f"[WX] send err len={len(s)} dt={time.time() - t0:.1f}s "
                    f"{type(e).__name__}: {e}", file=sys.__stdout__
                )
                return False

        try:
            while True:
                item = dq.get(timeout=300)
                if item.get("type") == "chunk":
                    continue
                if item.get("type") == "terminal":
                    terminal = item
                    result = terminal_output_text(item)
                    break
        except queue.Empty:
            terminal = {
                "type": "terminal",
                "status": "failed",
                "reason": "timeout",
                "text": "",
            }
        _typing_stop.set()

        status = terminal.get("status") if terminal else "failed"
        if status == "completed":
            final_text = _clean(terminal_reply_text(terminal or {}))
        else:
            final_text = terminal_notice(terminal or {})
        if final_text:
            _wx_send(final_text[-3000:])
        _task_aborted.pop(uid, None)
        files = resolve_output_files(
            result,
            workspace_dir=runner_workspace_dir(runner),
            fallback_dirs=(_TEMP_DIR, MEDIA_DIR),
        ) if status == "completed" else []
        input_media = {os.path.realpath(path) for path in media_paths}
        for fpath in files:
            if os.path.realpath(fpath) in input_media:
                continue
            try:
                ext = os.path.splitext(fpath)[1].lower()
                sender = (
                    bot.send_video if ext in {".mp4", ".mov", ".m4v", ".webm"}
                    else bot.send_image if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
                    else bot.send_file
                )
                sender(uid, fpath, context_token=ctx)
                print(f"[WX] sent media: {fpath}", file=sys.__stdout__)
            except Exception as e:
                print(f"[WX] send media err: {e}", file=sys.__stdout__)
                _wx_send(f"⚠️ 文件发送失败: {os.path.basename(fpath)}")

    threading.Thread(target=_handle, daemon=True).start()


# —— 主入口 ——
if __name__ == "__main__":
    za = ZeroAgent()
    runner = AgentRunner(za)
    runner.verbose = False
    _do_relogin = "--relogin" in sys.argv
    interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
    try:
        _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock.bind(("127.0.0.1", 19531))
    except OSError:
        print("[WeChat] Another instance running, exiting.")
        sys.exit(1)
    _logf = open(
        os.path.join(_PROJECT_ROOT, "temp", "wechatapp.log"),
        "a", encoding="utf-8", buffering=1,
    )
    sys.stdout = sys.stderr = _logf
    print(f"[NEW] Process starting {time.strftime('%m-%d %H:%M')}")
    try:
        _KEYS = load_keys()
    except ChannelConfigError:
        print("[Bot] channel configuration is unavailable; check the active config file.")
        sys.exit(1)
    bot = WxBotClient()
    if _do_relogin or not bot.token:
        if not interactive:
            print(
                "[Bot] no token or relogin requested in a non-interactive process; "
                "run `python -m zero_agent.bots.wechat_app --relogin` in a terminal."
            )
            sys.exit(1)
        sys.stdout = sys.stderr = sys.__stdout__
        try:
            bot.login_qr()
        finally:
            sys.stdout = sys.stderr = _logf
    print(f"WeChat Bot 已启动 (bot_id={bot.bot_id})", file=sys.__stdout__)
    try:
        bot.run_loop(on_message)
    except AuthExpired:
        print("[Bot] token expired, exit.", file=sys.__stdout__)
        sys.exit(2)
